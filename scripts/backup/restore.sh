#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform — PostgreSQL Restore Script
#
# Restores a pg_dump custom-format backup (.dump) created by backup.sh.
# Performs pre-restore integrity checks and post-restore table count + Alembic
# migration-version verification.
#
# Exit codes:
#   0  success
#   1  restore failed
#   2  configuration / dependency / safety error
#
# Usage:
#   ./scripts/backup/restore.sh [OPTIONS] [BACKUP_FILE]
#
# Arguments:
#   BACKUP_FILE   Path to a .dump file.  If omitted, the script selects the
#                 most recent full backup found in --backup-dir.
#
# Options:
#   --backup-dir  directory to search for backups   (default: ./backups)
#   --create-db   drop and recreate the target database before restoring
#   --no-confirm  skip interactive confirmation     (use in CI / cron)
#   --jobs N      parallel restore workers          (default: 4)
#   --help        show this message
#
# Environment — Database connection (one of two forms):
#   DATABASE_URL          postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
#
# Examples:
#   # Restore the latest backup automatically
#   ./scripts/backup/restore.sh --no-confirm
#
#   # Restore a specific file with full database recreation
#   ./scripts/backup/restore.sh backups/enterprise_agents_full_20240315T120000.dump \
#     --create-db
#
#   # Non-interactive restore for CI pipelines
#   DATABASE_URL=postgresql+asyncpg://app:secret@db:5432/enterprise_agents \
#     ./scripts/backup/restore.sh backups/enterprise_agents_full_*.dump --no-confirm
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

info()    { echo -e "$(_ts) ${CYAN}[restore]${NC} $*"; }
success() { echo -e "$(_ts) ${GREEN}[restore]${NC} $*"; }
warn()    { echo -e "$(_ts) ${YELLOW}[restore]${NC} $*" >&2; }
error()   { echo -e "$(_ts) ${RED}[restore]${NC} $*" >&2; exit 1; }
die()     { echo -e "$(_ts) ${RED}[restore]${NC} $*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BACKUP_FILE=""
BACKUP_DIR="./backups"
CREATE_DB=false
NO_CONFIRM=false
PARALLEL_JOBS=4

while [ $# -gt 0 ]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="${2:?--backup-dir requires an argument}"
      shift 2
      ;;
    --create-db)
      CREATE_DB=true
      shift
      ;;
    --no-confirm)
      NO_CONFIRM=true
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

# Validate --jobs
case "$PARALLEL_JOBS" in
  ''|*[!0-9]*) die "--jobs must be a positive integer (got: '$PARALLEL_JOBS')" ;;
esac

# ---------------------------------------------------------------------------
# Resolve the backup file
#   If not supplied, find the most recently modified full backup in BACKUP_DIR
# ---------------------------------------------------------------------------
if [ -z "$BACKUP_FILE" ]; then
  info "No backup file specified; searching for latest full backup in $BACKUP_DIR..."
  BACKUP_DIR_ABS="$(cd "$BACKUP_DIR" 2>/dev/null && pwd)" \
    || die "Backup directory not found: $BACKUP_DIR"

  # ls -t lists newest first; we pick the first match
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

# Resolve to an absolute path (avoids issues when script is called from
# different working directories)
BACKUP_FILE="$(cd "$(dirname "$BACKUP_FILE")" && pwd)/$(basename "$BACKUP_FILE")"

# ---------------------------------------------------------------------------
# Pre-restore checks
# ---------------------------------------------------------------------------
[ -f "$BACKUP_FILE" ] \
  || die "Backup file not found: $BACKUP_FILE"

[ -s "$BACKUP_FILE" ] \
  || die "Backup file is empty: $BACKUP_FILE"

info "Verifying backup file integrity with pg_restore --list ..."
if ! pg_restore --list "$BACKUP_FILE" > /dev/null 2>&1; then
  die "Backup file is corrupt or not a valid pg_dump custom-format file: $BACKUP_FILE"
fi
success "Integrity check passed."

# Count objects in the table of contents for the summary display
TOC_LINE_COUNT="$(pg_restore --list "$BACKUP_FILE" 2>/dev/null | wc -l | tr -d ' ')"
TABLE_DATA_COUNT="$(pg_restore --list "$BACKUP_FILE" 2>/dev/null | grep -c 'TABLE DATA' || true)"

# Load metadata sidecar if present
META_FILE="${BACKUP_FILE%.dump}.meta.json"
META_BACKUP_TS="(no metadata file)"
META_ALEMBIC_REV="(no metadata file)"
if [ -f "$META_FILE" ]; then
  META_BACKUP_TS="$(python3 -c "
import json
with open('$META_FILE') as f:
    d = json.load(f)
print(d.get('timestamp_utc', 'unknown'))
" 2>/dev/null || echo 'parse error')"
  META_ALEMBIC_REV="$(python3 -c "
import json
with open('$META_FILE') as f:
    d = json.load(f)
print(d.get('alembic_revision', 'unknown'))
" 2>/dev/null || echo 'parse error')"
fi

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
# Print restore plan and require confirmation
# ---------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}===== RESTORE PLAN =====================================================${NC}"
echo -e "  Backup file   : $BACKUP_FILE"
echo -e "  Backup time   : $META_BACKUP_TS"
echo -e "  Alembic rev   : $META_ALEMBIC_REV"
echo -e "  TOC objects   : $TOC_LINE_COUNT  (table-data: $TABLE_DATA_COUNT)"
echo -e "  Target DB     : ${PGDATABASE} @ ${PGHOST}:${PGPORT}"
echo -e "  Recreate DB   : $CREATE_DB"
echo -e "  Parallel jobs : $PARALLEL_JOBS"
echo -e "${RED}  WARNING: This will OVERWRITE data in '$PGDATABASE'!${NC}"
echo -e "${YELLOW}========================================================================${NC}"
echo ""

if [ "$NO_CONFIRM" = false ]; then
  printf "Type 'yes' to proceed, anything else to abort: "
  read -r CONFIRM
  if [ "$CONFIRM" != "yes" ]; then
    info "Restore cancelled by user."
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Optionally drop and recreate the database
#   This provides a clean slate for a full replace (no leftover objects).
#   Connects to the 'postgres' maintenance database to avoid being inside
#   the target database while dropping it.
# ---------------------------------------------------------------------------
if [ "$CREATE_DB" = true ]; then
  info "Terminating existing connections to '$PGDATABASE'..."
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="postgres" \
    --command "
      SELECT pg_terminate_backend(pid)
      FROM   pg_stat_activity
      WHERE  datname = '${PGDATABASE}'
        AND  pid <> pg_backend_pid();
    " > /dev/null 2>&1 || true

  info "Dropping database '$PGDATABASE'..."
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="postgres" \
    --command "DROP DATABASE IF EXISTS \"${PGDATABASE}\";" \
    || die "Failed to drop database '$PGDATABASE'."

  info "Creating database '$PGDATABASE'..."
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="postgres" \
    --command "
      CREATE DATABASE \"${PGDATABASE}\"
        WITH OWNER = \"${PGUSER}\"
        ENCODING = 'UTF8'
        LC_COLLATE = 'C'
        LC_CTYPE   = 'C'
        TEMPLATE template0;
    " || die "Failed to create database '$PGDATABASE'."

  # Re-enable pgvector extension (required for vector_chunks table)
  info "Enabling pgvector extension in '$PGDATABASE'..."
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="$PGDATABASE" \
    --command "CREATE EXTENSION IF NOT EXISTS vector;" \
    || warn "Could not create pgvector extension — may already exist in restored data."

  success "Database '$PGDATABASE' recreated."
fi

# ---------------------------------------------------------------------------
# Run the restore
# ---------------------------------------------------------------------------
info "Restoring '$BACKUP_FILE' into '${PGDATABASE}' on ${PGHOST}:${PGPORT}..."

START_EPOCH="$(date +%s)"

# --clean + --if-exists: drop existing objects before recreating them
#   (safe to run against a non-empty database; harmless against fresh one)
# --no-owner + --no-privileges: the restoring user becomes owner
# --jobs: parallel restore for faster throughput on large databases
pg_restore \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$PGDATABASE" \
  --format=custom \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --jobs="$PARALLEL_JOBS" \
  "$BACKUP_FILE" 2>&1 \
  | while IFS= read -r line; do
      # Suppress routine "does not exist, skipping" noise in verbose output
      echo "$(_ts) [pg_restore] $line"
    done \
  || warn "pg_restore exited non-zero — common when objects already exist. Proceeding."

END_EPOCH="$(date +%s)"
DURATION=$(( END_EPOCH - START_EPOCH ))

success "pg_restore finished in ${DURATION}s."

# ---------------------------------------------------------------------------
# Post-restore verification — table row counts
# ---------------------------------------------------------------------------
info "Running post-restore table row count spot-check..."

psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$PGDATABASE" \
  --tuples-only \
  --command "
    SELECT
      table_name,
      n_live_tup AS approx_row_count
    FROM information_schema.tables t
    JOIN pg_stat_user_tables s ON s.relname = t.table_name
    WHERE t.table_schema = 'public'
    ORDER BY n_live_tup DESC;
  " 2>/dev/null \
  | while IFS='|' read -r tbl rows; do
      tbl="$(echo "$tbl" | xargs)"
      rows="$(echo "$rows" | xargs)"
      [ -n "$tbl" ] && info "  $tbl : $rows rows (approx)"
    done \
  || warn "Could not retrieve table row counts."

# ---------------------------------------------------------------------------
# Post-restore verification — Alembic migration version
# ---------------------------------------------------------------------------
info "Checking alembic_version table in restored database..."

DB_ALEMBIC_REV="$(psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$PGDATABASE" \
  --tuples-only \
  --no-align \
  --command "SELECT version_num FROM alembic_version LIMIT 1;" \
  2>/dev/null | tr -d ' \n' || echo 'unknown')"

info "Restored Alembic revision: $DB_ALEMBIC_REV"

if [ "$META_ALEMBIC_REV" != "(no metadata file)" ] \
   && [ "$DB_ALEMBIC_REV" != "unknown" ] \
   && [ "$DB_ALEMBIC_REV" != "$META_ALEMBIC_REV" ]; then
  warn "Alembic revision mismatch!"
  warn "  Backup metadata says : $META_ALEMBIC_REV"
  warn "  Restored DB has      : $DB_ALEMBIC_REV"
  warn "  You may need to run: alembic upgrade head"
else
  success "Alembic revision verified: $DB_ALEMBIC_REV"
fi

# Offer to stamp alembic if alembic CLI is present
if command -v alembic > /dev/null 2>&1; then
  info "Running 'alembic stamp head' to ensure migration state is aligned..."
  alembic stamp head 2>/dev/null \
    && success "Alembic state stamped to head." \
    || warn "alembic stamp head failed — run manually if needed."
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
success ""
success "Restore completed:"
success "  Backup file  : $BACKUP_FILE"
success "  Database     : $PGDATABASE @ $PGHOST:$PGPORT"
success "  Duration     : ${DURATION}s"
success "  Alembic rev  : $DB_ALEMBIC_REV"
success ""
success "Recommended next steps:"
success "  1. alembic current               — confirm migration state"
success "  2. alembic upgrade head          — if revision is behind"
success "  3. Run integration tests         — pytest tests/ -m integration"
success "  4. Restart the API service       — docker compose restart api"
success ""

exit 0
