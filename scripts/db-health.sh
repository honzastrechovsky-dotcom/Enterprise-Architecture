#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform - Database Health Check Script
#
# Checks database health and emits a JSON report suitable for Prometheus,
# Datadog, or any monitoring system that consumes JSON.
#
# Exit codes:
#   0  healthy   - all checks passed
#   1  warning   - non-critical issues (high bloat, slow queries, etc.)
#   2  critical  - connectivity failure or severe issues
#
# Usage:
#   ./scripts/db-health.sh
#   ./scripts/db-health.sh | jq .
#
# Environment:
#   DATABASE_URL  postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE  (alternative to DATABASE_URL)
#
#   REPLICATION_LAG_WARN_BYTES   default: 104857600  (100 MB)
#   REPLICATION_LAG_CRIT_BYTES   default: 524288000  (500 MB)
#   LONG_QUERY_WARN_SECONDS      default: 30
#   LONG_QUERY_CRIT_SECONDS      default: 300
#   BLOAT_WARN_RATIO             default: 0.3  (30% dead tuples)
#   BLOAT_CRIT_RATIO             default: 0.5  (50% dead tuples)
# =============================================================================

set -euo pipefail

# ---- connection defaults & thresholds ----------------------------------------
REPLICATION_LAG_WARN_BYTES="${REPLICATION_LAG_WARN_BYTES:-104857600}"
REPLICATION_LAG_CRIT_BYTES="${REPLICATION_LAG_CRIT_BYTES:-524288000}"
LONG_QUERY_WARN_SECONDS="${LONG_QUERY_WARN_SECONDS:-30}"
LONG_QUERY_CRIT_SECONDS="${LONG_QUERY_CRIT_SECONDS:-300}"
BLOAT_WARN_RATIO="${BLOAT_WARN_RATIO:-0.3}"
BLOAT_CRIT_RATIO="${BLOAT_CRIT_RATIO:-0.5}"

# ---- parse DATABASE_URL ------------------------------------------------------
_parse_database_url() {
  local url="$1"
  url="${url/postgresql+asyncpg:\/\//postgresql://}"
  url="${url/postgres+asyncpg:\/\//postgresql://}"
  python3 - "$url" <<'PYEOF'
import sys, urllib.parse
url = sys.argv[1]
p = urllib.parse.urlparse(url)
print(f"PGHOST={p.hostname or 'localhost'}")
print(f"PGPORT={p.port or 5432}")
print(f"PGUSER={p.username or 'app'}")
print(f"PGPASSWORD={urllib.parse.unquote(p.password or '')}")
print(f"PGDATABASE={p.path.lstrip('/') or 'enterprise_agents'}")
PYEOF
}

if [ -n "${DATABASE_URL:-}" ]; then
  if command -v python3 &>/dev/null; then
    PGHOST=$(python3 -c "from urllib.parse import urlparse; u=urlparse('$DATABASE_URL'); print(u.hostname or 'localhost')")
    PGPORT=$(python3 -c "from urllib.parse import urlparse; u=urlparse('$DATABASE_URL'); print(u.port or 5432)")
    PGUSER=$(python3 -c "from urllib.parse import urlparse; u=urlparse('$DATABASE_URL'); print(u.username or 'postgres')")
    PGPASSWORD=$(python3 -c "from urllib.parse import urlparse; u=urlparse('$DATABASE_URL'); print(u.password or '')")
    PGDATABASE=$(python3 -c "from urllib.parse import urlparse; u=urlparse('$DATABASE_URL'); print(u.path.lstrip('/') or 'postgres')")
  else
    echo "ERROR: python3 required for safe URL parsing" >&2
    exit 1
  fi
fi

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-app}"
PGDATABASE="${PGDATABASE:-enterprise_agents}"
export PGHOST PGPORT PGUSER PGDATABASE
[ -n "${PGPASSWORD:-}" ] && export PGPASSWORD

# ---- psql helper: run SQL and return raw output (or empty on error) ----------
_psql() {
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="$PGDATABASE" \
    --tuples-only \
    --no-align \
    --command "$1" 2>/dev/null || echo ""
}

# Track overall severity: 0=healthy 1=warning 2=critical
OVERALL_STATUS=0
_raise() {
  local level="$1"  # 1=warning 2=critical
  [ "$level" -gt "$OVERALL_STATUS" ] && OVERALL_STATUS="$level"
}

# ---- 1. connectivity check ---------------------------------------------------
CONNECTION_OK=false
CONNECTION_ERROR=""
START_MS="$(date +%s%3N)"
if psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="$PGDATABASE" \
    --tuples-only \
    --command "SELECT 1" > /dev/null 2>&1; then
  CONNECTION_OK=true
else
  CONNECTION_ERROR="Cannot connect to $PGHOST:$PGPORT/$PGDATABASE"
  _raise 2
fi
END_MS="$(date +%s%3N)"
CONNECT_LATENCY_MS=$(( END_MS - START_MS ))

# If connection fails, emit minimal JSON immediately and exit 2
if [ "$CONNECTION_OK" = false ]; then
  python3 - <<PYEOF
import json, datetime
report = {
    "status": "critical",
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "database": "$PGDATABASE",
    "host": "$PGHOST",
    "port": $PGPORT,
    "checks": {
        "connection": {
            "status": "critical",
            "error": "$CONNECTION_ERROR"
        }
    }
}
print(json.dumps(report, indent=2))
PYEOF
  exit 2
fi

# ---- 2. server version -------------------------------------------------------
PG_VERSION="$(_psql "SELECT version()" | head -1 | xargs)"

# ---- 3. replication lag (bytes behind primary; empty on primary/standalone) --
REPLICATION_LAG_JSON="[]"
REPLICATION_LAG_STATUS="healthy"
RAW_LAG="$(_psql "
  SELECT
    client_addr::text,
    state,
    COALESCE(
      pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn),
      0
    ) AS send_lag_bytes,
    COALESCE(
      pg_wal_lsn_diff(sent_lsn, replay_lsn),
      0
    ) AS replay_lag_bytes
  FROM pg_stat_replication;
")"

if [ -n "$RAW_LAG" ]; then
  REPLICATION_LAG_JSON="$(echo "$RAW_LAG" | python3 - "$REPLICATION_LAG_WARN_BYTES" "$REPLICATION_LAG_CRIT_BYTES" <<'PYEOF'
import sys, json

warn_threshold = int(sys.argv[1])
crit_threshold = int(sys.argv[2])

lines = [l.strip() for l in sys.stdin if l.strip()]
replicas = []
worst = "healthy"

for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        continue
    client_addr, state, send_lag, replay_lag = parts[0], parts[1], int(parts[2]), int(parts[3])
    total_lag = send_lag + replay_lag
    if total_lag >= crit_threshold:
        status = "critical"
        worst = "critical"
    elif total_lag >= warn_threshold:
        status = "warning"
        if worst != "critical":
            worst = "warning"
    else:
        status = "healthy"
    replicas.append({
        "client": client_addr,
        "state": state,
        "send_lag_bytes": send_lag,
        "replay_lag_bytes": replay_lag,
        "total_lag_bytes": total_lag,
        "status": status,
    })

result = {"replicas": replicas, "status": worst}
print(json.dumps(result))
PYEOF
)"
  REPLICATION_LAG_STATUS="$(echo "$REPLICATION_LAG_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','healthy'))")"
  case "$REPLICATION_LAG_STATUS" in
    warning)  _raise 1 ;;
    critical) _raise 2 ;;
  esac
else
  REPLICATION_LAG_JSON='{"replicas":[],"status":"healthy","note":"standalone or primary with no replicas"}'
fi

# ---- 4. table sizes (top 20 by total size) -----------------------------------
TABLE_SIZES_JSON="$(_psql "
  SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
    pg_total_relation_size(schemaname||'.'||tablename)                 AS total_bytes,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename))       AS table_size,
    pg_size_pretty(
      pg_total_relation_size(schemaname||'.'||tablename)
      - pg_relation_size(schemaname||'.'||tablename)
    )                                                                  AS index_size,
    (SELECT reltuples::bigint
     FROM pg_class
     WHERE oid = (schemaname||'.'||tablename)::regclass)              AS est_row_count
  FROM pg_tables
  WHERE schemaname = 'public'
  ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
  LIMIT 20;
" | python3 - <<'PYEOF'
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
tables = []
for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 7:
        continue
    tables.append({
        "schema": parts[0],
        "table": parts[1],
        "total_size": parts[2],
        "total_bytes": int(parts[3]) if parts[3].lstrip("-").isdigit() else 0,
        "table_size": parts[4],
        "index_size": parts[5],
        "est_row_count": int(float(parts[6])) if parts[6].replace(".", "", 1).lstrip("-").isdigit() else 0,
    })
print(json.dumps(tables))
PYEOF
)"

# ---- 5. index usage stats (low usage = candidate for removal) ----------------
INDEX_USAGE_JSON="$(_psql "
  SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
    pg_relation_size(indexrelid)                  AS index_bytes
  FROM pg_stat_user_indexes
  WHERE schemaname = 'public'
  ORDER BY idx_scan ASC, pg_relation_size(indexrelid) DESC
  LIMIT 20;
" | python3 - <<'PYEOF'
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
indexes = []
for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 6:
        continue
    idx_scan = int(parts[3]) if parts[3].isdigit() else 0
    indexes.append({
        "schema": parts[0],
        "table": parts[1],
        "index": parts[2],
        "scans": idx_scan,
        "size": parts[4],
        "size_bytes": int(parts[5]) if parts[5].isdigit() else 0,
        "note": "low_usage" if idx_scan < 10 else "ok",
    })
print(json.dumps(indexes))
PYEOF
)"

# ---- 6. bloated tables (dead tuple ratio) ------------------------------------
BLOAT_JSON="$(_psql "
  SELECT
    relname                                             AS tablename,
    n_live_tup,
    n_dead_tup,
    CASE WHEN (n_live_tup + n_dead_tup) > 0
         THEN round(n_dead_tup::numeric / (n_live_tup + n_dead_tup), 4)
         ELSE 0
    END                                                AS dead_ratio,
    pg_size_pretty(pg_relation_size(relid))             AS table_size,
    last_vacuum::text,
    last_autovacuum::text
  FROM pg_stat_user_tables
  WHERE schemaname = 'public'
  ORDER BY dead_ratio DESC
  LIMIT 20;
" | python3 - "$BLOAT_WARN_RATIO" "$BLOAT_CRIT_RATIO" <<'PYEOF'
import sys, json

warn_ratio = float(sys.argv[1])
crit_ratio = float(sys.argv[2])

lines = [l.strip() for l in sys.stdin if l.strip()]
tables = []
worst = "healthy"

for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 7:
        continue
    dead_ratio = float(parts[3]) if parts[3] else 0.0
    if dead_ratio >= crit_ratio:
        status = "critical"
        worst = "critical"
    elif dead_ratio >= warn_ratio:
        status = "warning"
        if worst != "critical":
            worst = "warning"
    else:
        status = "healthy"
    tables.append({
        "table": parts[0],
        "live_tuples": int(parts[1]) if parts[1].isdigit() else 0,
        "dead_tuples": int(parts[2]) if parts[2].isdigit() else 0,
        "dead_ratio": dead_ratio,
        "size": parts[4],
        "last_vacuum": parts[5] or None,
        "last_autovacuum": parts[6] or None,
        "status": status,
    })

print(json.dumps({"tables": tables, "status": worst}))
PYEOF
)"
BLOAT_STATUS="$(echo "$BLOAT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','healthy'))")"
case "$BLOAT_STATUS" in
  warning)  _raise 1 ;;
  critical) _raise 2 ;;
esac

# ---- 7. long-running queries -------------------------------------------------
LONG_QUERIES_JSON="$(_psql "
  SELECT
    pid,
    state,
    EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_seconds,
    left(query, 120)                                AS query_snippet,
    wait_event_type,
    wait_event
  FROM pg_stat_activity
  WHERE state <> 'idle'
    AND query_start < now() - interval '$LONG_QUERY_WARN_SECONDS seconds'
    AND query NOT ILIKE '%pg_stat_activity%'
  ORDER BY duration_seconds DESC
  LIMIT 10;
" | python3 - "$LONG_QUERY_WARN_SECONDS" "$LONG_QUERY_CRIT_SECONDS" <<'PYEOF'
import sys, json

warn_sec = int(sys.argv[1])
crit_sec = int(sys.argv[2])

lines = [l.strip() for l in sys.stdin if l.strip()]
queries = []
worst = "healthy"

for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 6:
        continue
    dur = int(parts[2]) if parts[2].lstrip("-").isdigit() else 0
    if dur >= crit_sec:
        status = "critical"
        worst = "critical"
    elif dur >= warn_sec:
        status = "warning"
        if worst != "critical":
            worst = "warning"
    else:
        status = "healthy"
    queries.append({
        "pid": int(parts[0]) if parts[0].isdigit() else None,
        "state": parts[1],
        "duration_seconds": dur,
        "query_snippet": parts[3],
        "wait_event_type": parts[4] or None,
        "wait_event": parts[5] or None,
        "status": status,
    })

print(json.dumps({"queries": queries, "status": worst}))
PYEOF
)"
LONG_QUERY_STATUS="$(echo "$LONG_QUERIES_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','healthy'))")"
case "$LONG_QUERY_STATUS" in
  warning)  _raise 1 ;;
  critical) _raise 2 ;;
esac

# ---- 8. lock contention (blocked queries) ------------------------------------
LOCK_JSON="$(_psql "
  SELECT
    blocked.pid                        AS blocked_pid,
    blocked.query                      AS blocked_query,
    blocking.pid                       AS blocking_pid,
    blocking.query                     AS blocking_query,
    EXTRACT(EPOCH FROM (now() - blocked.query_start))::int AS blocked_duration_seconds
  FROM pg_stat_activity blocked
  JOIN pg_stat_activity blocking
    ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
  WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
  LIMIT 10;
" | python3 - <<'PYEOF'
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
locks = []
for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5:
        continue
    locks.append({
        "blocked_pid": int(parts[0]) if parts[0].isdigit() else None,
        "blocked_query": parts[1][:120],
        "blocking_pid": int(parts[2]) if parts[2].isdigit() else None,
        "blocking_query": parts[3][:120],
        "blocked_duration_seconds": int(parts[4]) if parts[4].lstrip("-").isdigit() else 0,
    })
status = "warning" if locks else "healthy"
print(json.dumps({"locks": locks, "count": len(locks), "status": status}))
PYEOF
)"
LOCK_STATUS="$(echo "$LOCK_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','healthy'))")"
[ "$LOCK_STATUS" = "warning" ] && _raise 1

# ---- 9. database statistics --------------------------------------------------
DB_STATS_JSON="$(_psql "
  SELECT
    numbackends              AS active_connections,
    xact_commit              AS transactions_committed,
    xact_rollback            AS transactions_rolled_back,
    blks_read                AS blocks_read,
    blks_hit                 AS blocks_hit,
    CASE WHEN (blks_read + blks_hit) > 0
         THEN round(blks_hit::numeric / (blks_read + blks_hit) * 100, 2)
         ELSE 100
    END                      AS cache_hit_ratio,
    deadlocks,
    temp_files,
    pg_size_pretty(temp_bytes) AS temp_size
  FROM pg_stat_database
  WHERE datname = '$PGDATABASE';
" | python3 - <<'PYEOF'
import sys, json
lines = [l.strip() for l in sys.stdin if l.strip()]
for line in lines:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 9:
        continue
    print(json.dumps({
        "active_connections": int(parts[0]) if parts[0].isdigit() else 0,
        "transactions_committed": int(parts[1]) if parts[1].isdigit() else 0,
        "transactions_rolled_back": int(parts[2]) if parts[2].isdigit() else 0,
        "blocks_read": int(parts[3]) if parts[3].isdigit() else 0,
        "blocks_hit": int(parts[4]) if parts[4].isdigit() else 0,
        "cache_hit_ratio": float(parts[5]) if parts[5].replace(".", "").isdigit() else 0.0,
        "deadlocks": int(parts[6]) if parts[6].isdigit() else 0,
        "temp_files": int(parts[7]) if parts[7].isdigit() else 0,
        "temp_size": parts[8],
    }))
    break
PYEOF
)"

# ---- assemble final JSON report ----------------------------------------------
STATUS_LABEL="healthy"
case "$OVERALL_STATUS" in
  1) STATUS_LABEL="warning" ;;
  2) STATUS_LABEL="critical" ;;
esac

python3 - <<PYEOF
import json, datetime

replication = json.loads('''$REPLICATION_LAG_JSON''')
tables = json.loads('''${TABLE_SIZES_JSON:-[]}''')
indexes = json.loads('''${INDEX_USAGE_JSON:-[]}''')
bloat = json.loads('''$BLOAT_JSON''')
long_queries = json.loads('''$LONG_QUERIES_JSON''')
locks = json.loads('''$LOCK_JSON''')
db_stats_raw = '''${DB_STATS_JSON:-{}}'''.strip()
db_stats = json.loads(db_stats_raw) if db_stats_raw else {}

report = {
    "status": "$STATUS_LABEL",
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "database": "$PGDATABASE",
    "host": "$PGHOST",
    "port": $PGPORT,
    "connect_latency_ms": $CONNECT_LATENCY_MS,
    "pg_version": "$PG_VERSION",
    "checks": {
        "connection": {
            "status": "healthy",
            "latency_ms": $CONNECT_LATENCY_MS,
        },
        "replication": replication,
        "long_running_queries": long_queries,
        "lock_contention": locks,
        "table_bloat": bloat,
    },
    "metrics": {
        "table_sizes": tables,
        "index_usage": indexes,
        "database_stats": db_stats,
    },
    "thresholds": {
        "replication_lag_warn_bytes": $REPLICATION_LAG_WARN_BYTES,
        "replication_lag_crit_bytes": $REPLICATION_LAG_CRIT_BYTES,
        "long_query_warn_seconds": $LONG_QUERY_WARN_SECONDS,
        "long_query_crit_seconds": $LONG_QUERY_CRIT_SECONDS,
        "bloat_warn_ratio": $BLOAT_WARN_RATIO,
        "bloat_crit_ratio": $BLOAT_CRIT_RATIO,
    },
}

print(json.dumps(report, indent=2))
PYEOF

exit "$OVERALL_STATUS"
