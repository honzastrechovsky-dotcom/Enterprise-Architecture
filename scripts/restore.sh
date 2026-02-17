#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform - Database Restore Script
#
# Restores a pg_dump custom-format backup created by backup.sh.
#
# Usage:
#   ./scripts/restore.sh <backup_file> [--create-db] [--no-confirm]
#
# Arguments:
#   backup_file   path to .dump file (custom pg_dump format)
#   --create-db   drop and recreate the target database before restoring
#   --no-confirm  skip interactive confirmation prompt (for CI/automation)
#
# Environment:
#   DATABASE_URL  postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE  (alternative to DATABASE_URL)
#
# Examples:
#   ./scripts/restore.sh backups/enterprise_agents_full_20240315T120000.dump
#   ./scripts/restore.sh backups/enterprise_agents_full_20240315T120000.dump --create-db
#   ./scripts/restore.sh backups/enterprise_agents_full_20240315T120000.dump --no-confirm
# =============================================================================

set -euo pipefail

# ---- colour helpers ----------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[restore]${NC} $*"; }
success() { echo -e "${GREEN}[restore]${NC} $*"; }
warn()    { echo -e "${YELLOW}[restore]${NC} $*" >&2; }
error()   { echo -e "${RED}[restore]${NC} $*" >&2; exit 1; }

# ---- arguments ---------------------------------------------------------------
BACKUP_FILE=""
CREATE_DB=false
NO_CONFIRM=false

for arg in "$@"; do
  case "$arg" in
    --create-db)   CREATE_DB=true ;;
    --no-confirm)  NO_CONFIRM=true ;;
    --help|-h)
      sed -n '3,30p' "$0"
      exit 0
      ;;
    -*)
      error "Unknown flag: $arg" ;;
    *)
      [ -z "$BACKUP_FILE" ] && BACKUP_FILE="$arg" || error "Unexpected argument: $arg" ;;
  esac
done

[ -z "$BACKUP_FILE" ] && error "No backup file specified. Usage: $0 <backup_file> [--create-db]"

# ---- resolve absolute path ---------------------------------------------------
BACKUP_FILE="$(cd "$(dirname "$BACKUP_FILE")" && pwd)/$(basename "$BACKUP_FILE")"

# ---- sanity-check backup file exists ----------------------------------------
[ -f "$BACKUP_FILE" ] || error "Backup file not found: $BACKUP_FILE"

# ---- verify backup integrity -------------------------------------------------
info "Verifying backup file integrity..."
if ! pg_restore --list "$BACKUP_FILE" > /dev/null 2>&1; then
  error "Backup file appears corrupt or is not a valid pg_dump custom-format file."
fi
success "Backup file integrity check passed."

# ---- print backup summary from table of contents ----------------------------
TOC_OBJECTS="$(pg_restore --list "$BACKUP_FILE" 2>/dev/null | wc -l | tr -d ' ')"
info "Backup contains $TOC_OBJECTS restore objects."

# ---- parse connection parameters -------------------------------------------
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
  info "Parsing connection from DATABASE_URL..."
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

# ---- check required tools ----------------------------------------------------
for tool in pg_restore psql alembic; do
  if ! command -v "$tool" &>/dev/null; then
    if [ "$tool" = "alembic" ]; then
      warn "alembic not found; will skip post-restore stamp step."
    else
      error "$tool not found. Install postgresql-client."
    fi
  fi
done

# ---- print restore plan and confirm -----------------------------------------
echo ""
echo -e "${YELLOW}=== RESTORE PLAN ===${NC}"
echo -e "  Source file : $BACKUP_FILE"
echo -e "  Target DB   : $PGDATABASE on $PGHOST:$PGPORT"
echo -e "  Create DB   : $CREATE_DB"
echo -e "  Objects     : $TOC_OBJECTS"
echo -e "${RED}WARNING: This will OVERWRITE data in the target database!${NC}"
echo ""

if [ "$NO_CONFIRM" = false ]; then
  read -r -p "Type 'yes' to proceed: " CONFIRM
  [ "$CONFIRM" = "yes" ] || { info "Restore cancelled."; exit 0; }
fi

# ---- optionally recreate database -------------------------------------------
if [ "$CREATE_DB" = true ]; then
  info "Dropping and recreating database '$PGDATABASE'..."
  # Connect to 'postgres' maintenance DB to drop/create the target
  psql \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-password \
    --dbname="postgres" \
    -v dbname="$PGDATABASE" \
    -v dbowner="$PGUSER" \
    <<'SQL'
SELECT pg_terminate_backend(pid)
FROM   pg_stat_activity
WHERE  datname = :'dbname'
  AND  pid <> pg_backend_pid();

DROP DATABASE IF EXISTS :"dbname";
CREATE DATABASE :"dbname"
  WITH OWNER = :"dbowner"
  ENCODING = 'UTF8'
  LC_COLLATE = 'C'
  LC_CTYPE   = 'C'
  TEMPLATE template0;
SQL
  success "Database '$PGDATABASE' recreated."
fi

# ---- run restore -------------------------------------------------------------
info "Restoring from $BACKUP_FILE into '$PGDATABASE'..."

START_TS="$(date +%s)"

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
  --verbose \
  "$BACKUP_FILE" 2>&1 || {
    warn "pg_restore exited with non-zero (may be harmless object-exists warnings)."
  }

END_TS="$(date +%s)"
DURATION=$(( END_TS - START_TS ))

success "Restore completed in ${DURATION}s."

# ---- post-restore: alembic stamp head ----------------------------------------
if command -v alembic &>/dev/null; then
  info "Running 'alembic stamp head' to align migration state..."
  alembic stamp head && success "Alembic migration state stamped to head." \
    || warn "alembic stamp head failed - you may need to run this manually."
else
  warn "alembic not found - run 'alembic stamp head' manually after confirming schema."
fi

# ---- verify row count spot-check --------------------------------------------
info "Running post-restore spot-check..."
psql \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --no-password \
  --dbname="$PGDATABASE" \
  --tuples-only \
  --command "
    SELECT table_name, n_live_tup AS row_estimate
    FROM   information_schema.tables t
    JOIN   pg_stat_user_tables s USING (table_name)
    WHERE  t.table_schema = 'public'
    ORDER  BY n_live_tup DESC
    LIMIT  10;
  " 2>/dev/null || warn "Could not run spot-check query."

success ""
success "Restore complete:"
success "  Database : $PGDATABASE"
success "  Host     : $PGHOST:$PGPORT"
success "  Duration : ${DURATION}s"
success ""
success "Next steps:"
success "  1. Verify application connectivity"
success "  2. Check alembic version: alembic current"
success "  3. Run integration tests if needed"
