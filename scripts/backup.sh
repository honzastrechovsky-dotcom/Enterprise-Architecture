#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform - Database Backup Script
#
# Performs pg_dump backups in custom format (supports point-in-time restore).
#
# Usage:
#   ./scripts/backup.sh [full|schema|data] [output_dir]
#
# Arguments:
#   mode        full (default) | schema | data
#   output_dir  directory for backup files (default: ./backups)
#
# Environment:
#   DATABASE_URL  postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE  (alternative to DATABASE_URL)
#
# Examples:
#   ./scripts/backup.sh
#   ./scripts/backup.sh full /var/backups/postgres
#   ./scripts/backup.sh schema ./backups
#   DATABASE_URL=postgresql://app:secret@db:5432/enterprise_agents ./scripts/backup.sh data
# =============================================================================

set -euo pipefail

# ---- colour helpers ----------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[backup]${NC}  $*"; }
success() { echo -e "${GREEN}[backup]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[backup]${NC}  $*" >&2; }
error()   { echo -e "${RED}[backup]${NC}  $*" >&2; exit 1; }

# ---- arguments ---------------------------------------------------------------
MODE="${1:-full}"
OUTPUT_DIR="${2:-./backups}"

case "$MODE" in
  full|schema|data) ;;
  *) error "Invalid mode '$MODE'. Use: full | schema | data" ;;
esac

# ---- resolve connection parameters from DATABASE_URL or env ------------------
_parse_database_url() {
  local url="$1"
  # Strip async driver prefix: postgresql+asyncpg:// -> postgresql://
  url="${url/postgresql+asyncpg:\/\//postgresql://}"
  url="${url/postgres+asyncpg:\/\//postgresql://}"

  # Extract components via Python (portable, avoids regex edge cases)
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
  info "Parsing connection from DATABASE_URL..."
  eval "$(_parse_database_url "$DATABASE_URL")"
fi

# Fall back to defaults if individual vars still not set
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-app}"
PGDATABASE="${PGDATABASE:-enterprise_agents}"
export PGHOST PGPORT PGUSER PGDATABASE
[ -n "${PGPASSWORD:-}" ] && export PGPASSWORD

# ---- verify pg_dump is available ---------------------------------------------
if ! command -v pg_dump &>/dev/null; then
  error "pg_dump not found. Install postgresql-client or run inside the db container."
fi

# ---- prepare output directory ------------------------------------------------
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# ---- build filename with timestamp -------------------------------------------
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
FILENAME="${PGDATABASE}_${MODE}_${TIMESTAMP}.dump"
BACKUP_PATH="${OUTPUT_DIR}/${FILENAME}"
META_PATH="${OUTPUT_DIR}/${PGDATABASE}_${MODE}_${TIMESTAMP}.meta.json"

# ---- build pg_dump flags for each mode --------------------------------------
PG_DUMP_FLAGS=(
  --format=custom          # custom format: compressed, supports selective restore
  --compress=9             # maximum zstd/gzip compression
  --no-password            # rely on PGPASSWORD env var
  --host="$PGHOST"
  --port="$PGPORT"
  --username="$PGUSER"
)

case "$MODE" in
  full)
    # Full schema + data - default, no extra flags needed
    ;;
  schema)
    PG_DUMP_FLAGS+=(--schema-only)
    ;;
  data)
    PG_DUMP_FLAGS+=(--data-only)
    ;;
esac

# ---- run backup --------------------------------------------------------------
info "Starting $MODE backup of '$PGDATABASE' on $PGHOST:$PGPORT..."
info "Output: $BACKUP_PATH"

START_TS="$(date +%s)"

pg_dump "${PG_DUMP_FLAGS[@]}" "$PGDATABASE" > "$BACKUP_PATH"

END_TS="$(date +%s)"
DURATION=$(( END_TS - START_TS ))

# ---- gather metadata ---------------------------------------------------------
BACKUP_SIZE="$(du -b "$BACKUP_PATH" 2>/dev/null | cut -f1 || stat -f%z "$BACKUP_PATH" 2>/dev/null || echo 0)"
BACKUP_SIZE_HUMAN="$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1 || echo 'unknown')"

# Table count (only meaningful for full or data backups; 0 for schema)
TABLE_COUNT=0
if [ "$MODE" != "schema" ]; then
  TABLE_COUNT="$(pg_restore --list "$BACKUP_PATH" 2>/dev/null \
    | grep -c "TABLE DATA" || true)"
fi

# ---- write metadata ----------------------------------------------------------
python3 - <<PYEOF
import json, os, datetime

meta = {
    "backup_file": "$BACKUP_PATH",
    "mode": "$MODE",
    "database": "$PGDATABASE",
    "host": "$PGHOST",
    "port": $PGPORT,
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "duration_seconds": $DURATION,
    "size_bytes": $BACKUP_SIZE,
    "size_human": "$BACKUP_SIZE_HUMAN",
    "table_count": $TABLE_COUNT,
    "pg_dump_version": os.popen("pg_dump --version").read().strip(),
}

with open("$META_PATH", "w") as f:
    json.dump(meta, f, indent=2)

print(json.dumps(meta, indent=2))
PYEOF

success "Backup complete:"
success "  File     : $BACKUP_PATH"
success "  Mode     : $MODE"
success "  Size     : $BACKUP_SIZE_HUMAN"
success "  Duration : ${DURATION}s"
success "  Tables   : $TABLE_COUNT"
success "  Metadata : $META_PATH"
