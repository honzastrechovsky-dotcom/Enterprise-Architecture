#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform — Backup Verification Script
#
# Restores a backup into a temporary database, validates expected tables and
# row counts, checks the Alembic migration version, then cleans up.
#
# This script is non-destructive to the production database.
# All work happens in a throwaway database named <target>_verify_<timestamp>.
#
# Exit codes:
#   0  all checks passed
#   1  one or more checks failed
#   2  configuration / dependency / setup error
#
# Usage:
#   ./scripts/backup/verify.sh [OPTIONS] [BACKUP_FILE]
#
# Arguments:
#   BACKUP_FILE   Path to a .dump file.  If omitted, picks the most recent
#                 full backup in --backup-dir.
#
# Options:
#   --backup-dir  directory to search for backups   (default: ./backups)
#   --no-cleanup  leave the temp database after verification (for debugging)
#   --jobs N      parallel restore workers          (default: 4)
#   --help        show this message
#
# Environment — Database connection (one of two forms):
#   DATABASE_URL          postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
#
# Examples:
#   # Verify the latest backup automatically
#   ./scripts/backup/verify.sh
#
#   # Verify a specific file and leave the temp DB for inspection
#   ./scripts/backup/verify.sh backups/enterprise_agents_full_20240315T120000.dump \
#     --no-cleanup
#
#   # Called from cron-backup.sh immediately after a backup
#   DATABASE_URL=postgresql+asyncpg://app:secret@db:5432/enterprise_agents \
#     ./scripts/backup/verify.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour / logging helpers
# ---------------------------------------------------------------------------
_ts()      { date '+%Y-%m-%dT%H:%M:%S'; }
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "$(_ts) ${CYAN}[verify]${NC}  $*"; }
success() { echo -e "$(_ts) ${GREEN}[verify]${NC}  $*"; }
warn()    { echo -e "$(_ts) ${YELLOW}[verify]${NC}  $*" >&2; }
error()   { echo -e "$(_ts) ${RED}[verify]${NC}  FAIL — $*" >&2; CHECKS_FAILED=$(( CHECKS_FAILED + 1 )); }
die()     { echo -e "$(_ts) ${RED}[verify]${NC}  $*" >&2; exit 2; }

CHECKS_FAILED=0
TEMP_DB=""   # set once created so the EXIT trap can drop it

# ---------------------------------------------------------------------------
# Cleanup trap — always drop the temporary database
# ---------------------------------------------------------------------------
_cleanup() {
  local exit_code=$?
  if [ -n "$TEMP_DB" ] && [ "$NO_CLEANUP" = false ]; then
    info "Dropping temporary database '$TEMP_DB'..."
    psql \
      --host="$PGHOST" \
      --port="$PGPORT" \
      --username="$PGUSER" \
      --no-password \
      --dbname="postgres" \
      --command "
        SELECT pg_terminate_backend(pid)
        FROM   pg_stat_activity
        WHERE  datname = '${TEMP_DB}'
          AND  pid <> pg_backend_pid();
        DROP DATABASE IF EXISTS \"${TEMP_DB}\";
      " > /dev/null 2>&1 \
      && info "Temporary database '$TEMP_DB' dropped." \
      || warn "Could not drop '$TEMP_DB' — remove it manually: DROP DATABASE \"${TEMP_DB}\";"
  elif [ -n "$TEMP_DB" ] && [ "$NO_CLEANUP" = true ]; then
    warn "Temp database left for inspection: $TEMP_DB"
    warn "  Remove with: DROP DATABASE \"${TEMP_DB}\";"
  fi
  exit $exit_code
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BACKUP_FILE=""
BACKUP_DIR="./backups"
NO_CLEANUP=false
PARALLEL_JOBS=4

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="${2:?--backup-dir requires an argument}"
      shift 2
      ;;
    --no-cleanup)
      NO_CLEANUP=true
      shift
      ;;
    --jobs)
      PARALLEL_JOBS="${2:?--jobs requires an argument}"
      shift 2
      ;;
    --help|-h)
      sed -n '3,55p' "$0"
      exit 0
      ;;
    -*)
      die "Unknown flag: $1  (use --help for usage)"
      ;;
    *)
      [ -z "$BACKUP_FILE" ] && BACKUP_FILE="$1" || die "Unexpected argument: $1"
      shift
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve backup file (auto-select latest if not given)
# ---------------------------------------------------------------------------
if [ -z "$BACKUP_FILE" ]; then
  BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" 2>/dev/null && pwd)" \
    || die "Backup directory not found: $BACKUP_DIR"

  LATEST=""
  for candidate in $(ls -t "${BACKUP_DIR_ABS}"/*_full_*.dump 2>/dev/null || true); do
    LATEST="$candidate"
    break
  done

  [ -n "$LATEST" ] \
    || die "No full backup files (*_full_*.dump) found in $BACKUP_DIR_ABS"

  BACKUP_FILE="$LATEST"
  info "Auto-selected: $BACKUP_FILE"
fi

BACKUP_FILE="$(cd "$(dirname "$BACKUP_FILE")" && pwd)/$(basename "$BACKUP_FILE")"

# ---------------------------------------------------------------------------
# Parse DATABASE_URL into PG* variables
# ---------------------------------------------------------------------------
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
  info "Parsing connection parameters from DATABASE_URL..."
  eval "$(_parse_database_url "$DATABASE_URL")"
fi

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-app}"
PGDATABASE="${PGDATABASE:-enterprise_agents}"
export PGHOST PGPORT PGUSER PGDATABASE
[ -n "${PGPASSWORD:-}" ] && export PGPASSWORD

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
for tool in pg_restore psql; do
  command -v "$tool" > /dev/null 2>&1 \
    || die "$tool not found. Install postgresql-client-16."
done

# ---------------------------------------------------------------------------
# Pre-restore file checks
# ---------------------------------------------------------------------------
info "===== Backup Verification ================================================="
info "File : $BACKUP_FILE"

[ -f "$BACKUP_FILE" ]  || die "Backup file not found: $BACKUP_FILE"
[ -s "$BACKUP_FILE" ]  || die "Backup file is empty: $BACKUP_FILE"

info "CHECK 1/5: File integrity (pg_restore --list)..."
if pg_restore --list "$BACKUP_FILE" > /dev/null 2>&1; then
  success "CHECK 1/5 PASSED: Backup file is a valid custom-format dump."
else
  die "Backup file is corrupt or not a valid pg_dump custom-format file."
fi

TABLE_DATA_COUNT="$(pg_restore --list "$BACKUP_FILE" 2>/dev/null | grep -c 'TABLE DATA' || true)"
info "  Table-data objects in backup: $TABLE_DATA_COUNT"

# Read metadata sidecar
META_FILE="${BACKUP_FILE%.dump}.meta.json"
EXPECTED_ALEMBIC_REV="unknown"
if [ -f "$META_FILE" ]; then
  EXPECTED_ALEMBIC_REV="$(python3 -c "
import json
with open('$META_FILE') as f:
    d = json.load(f)
print(d.get('alembic_revision', 'unknown'))
" 2>/dev/null || echo 'unknown')"
  info "  Metadata file found: alembic_revision=$EXPECTED_ALEMBIC_REV"
else
  warn "  No metadata sidecar found (${META_FILE}) — skipping revision cross-check."
fi

# ---------------------------------------------------------------------------
# Create temporary database
# ---------------------------------------------------------------------------
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
TEMP_DB="${PGDATABASE}_verify_${TIMESTAMP}"

info "CHECK 2/5: Creating temporary database '$TEMP_DB'..."

psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="postgres" \
  --command "
    CREATE DATABASE \"${TEMP_DB}\"
      WITH OWNER = \"${PGUSER}\"
      ENCODING = 'UTF8'
      LC_COLLATE = 'C'
      LC_CTYPE   = 'C'
      TEMPLATE template0;
  " \
  || die "Failed to create temporary database '$TEMP_DB'."

# Enable pgvector — required to restore vector column type
psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$TEMP_DB" \
  --command "CREATE EXTENSION IF NOT EXISTS vector;" \
  > /dev/null 2>&1 \
  || warn "Could not create pgvector extension in temp DB — restore may fail for vector columns."

success "CHECK 2/5 PASSED: Temporary database '$TEMP_DB' created."

# ---------------------------------------------------------------------------
# Restore into temporary database
# ---------------------------------------------------------------------------
info "CHECK 3/5: Restoring backup into '$TEMP_DB'..."

START_EPOCH="$(date +%s)"

pg_restore \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$TEMP_DB" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --jobs="$PARALLEL_JOBS" \
  "$BACKUP_FILE" 2>&1 \
  | while IFS= read -r line; do
      echo "$(_ts) [pg_restore] $line"
    done \
  || warn "pg_restore exited non-zero — checking if data arrived despite warnings..."

END_EPOCH="$(date +%s)"
RESTORE_DURATION=$(( END_EPOCH - START_EPOCH ))

# Verify at least one table exists
RESTORED_TABLE_COUNT="$(psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$TEMP_DB" \
  --tuples-only \
  --no-align \
  --command "
    SELECT count(*)
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
  " 2>/dev/null | tr -d ' \n' || echo '0')"

if [ "$RESTORED_TABLE_COUNT" -gt 0 ]; then
  success "CHECK 3/5 PASSED: Restored $RESTORED_TABLE_COUNT tables in ${RESTORE_DURATION}s."
else
  error "Restore produced 0 tables in the temporary database."
fi

# ---------------------------------------------------------------------------
# Table presence check
#   Verify all 13 known application tables are present.
# ---------------------------------------------------------------------------
info "CHECK 4/5: Verifying expected tables are present..."

EXPECTED_TABLES=(
  users
  tenants
  conversations
  messages
  documents
  vector_chunks
  audit_logs
  agent_registry
  feedback
  token_budgets
  token_usage_records
  routing_decisions
  execution_plans
)

MISSING_TABLES=()
for table in "${EXPECTED_TABLES[@]}"; do
  EXISTS="$(psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="$TEMP_DB" \
    --tuples-only \
    --no-align \
    --command "
      SELECT count(*)
      FROM information_schema.tables
      WHERE table_schema = 'public'
        AND table_name   = '${table}';
    " 2>/dev/null | tr -d ' \n' || echo '0')"

  if [ "$EXISTS" -gt 0 ]; then
    info "  [OK] $table"
  else
    warn "  [MISSING] $table"
    MISSING_TABLES+=("$table")
  fi
done

if [ ${#MISSING_TABLES[@]} -eq 0 ]; then
  success "CHECK 4/5 PASSED: All ${#EXPECTED_TABLES[@]} expected tables present."
else
  error "${#MISSING_TABLES[@]} table(s) missing: ${MISSING_TABLES[*]}"
fi

# ---------------------------------------------------------------------------
# Alembic migration version check
# ---------------------------------------------------------------------------
info "CHECK 5/5: Verifying Alembic migration version..."

RESTORED_REV="$(psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$TEMP_DB" \
  --tuples-only \
  --no-align \
  --command "SELECT version_num FROM alembic_version LIMIT 1;" \
  2>/dev/null | tr -d ' \n' || echo 'unknown')"

info "  Restored revision : $RESTORED_REV"
info "  Expected revision : $EXPECTED_ALEMBIC_REV"

if [ "$RESTORED_REV" = "unknown" ]; then
  error "Could not read alembic_version from restored database."
elif [ "$EXPECTED_ALEMBIC_REV" != "unknown" ] \
     && [ "$RESTORED_REV" != "$EXPECTED_ALEMBIC_REV" ]; then
  error "Alembic revision mismatch: got '$RESTORED_REV', expected '$EXPECTED_ALEMBIC_REV'."
else
  success "CHECK 5/5 PASSED: Alembic revision = $RESTORED_REV."
fi

# ---------------------------------------------------------------------------
# Print row count summary from temporary database
# ---------------------------------------------------------------------------
info ""
info "Row count summary (approximate, from pg_stat):"
psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$TEMP_DB" \
  --tuples-only \
  --command "
    SELECT
      relname                AS table_name,
      n_live_tup             AS approx_rows
    FROM pg_stat_user_tables
    ORDER BY n_live_tup DESC;
  " 2>/dev/null \
  | while IFS='|' read -r tbl rows; do
      tbl="$(echo "$tbl" | xargs)"
      rows="$(echo "$rows" | xargs)"
      [ -n "$tbl" ] && info "  $tbl : $rows"
    done \
  || warn "Could not retrieve row counts."

# ---------------------------------------------------------------------------
# Final verdict
# ---------------------------------------------------------------------------
echo ""
if [ "$CHECKS_FAILED" -eq 0 ]; then
  success "===== VERIFICATION RESULT: ALL CHECKS PASSED ============================="
  success "  Backup file : $BACKUP_FILE"
  success "  Restore DB  : $TEMP_DB (will be dropped)"
  success "  Duration    : ${RESTORE_DURATION}s"
  exit 0
else
  echo -e "$(_ts) ${RED}[verify]${NC}  ===== VERIFICATION RESULT: $CHECKS_FAILED CHECK(S) FAILED ====================="
  echo -e "$(_ts) ${RED}[verify]${NC}  Backup file : $BACKUP_FILE"
  echo -e "$(_ts) ${RED}[verify]${NC}  Review warnings above to diagnose issues."
  exit 1
fi
