#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform — PostgreSQL Backup Script
#
# Performs a pg_dump in custom format (.dump) with gzip-compressed metadata,
# optional S3/MinIO upload, and a configurable retention policy.
#
# Exit codes:
#   0  success
#   1  backup failed
#   2  configuration / dependency error
#
# Usage:
#   ./scripts/backup/backup.sh [OPTIONS]
#
# Options:
#   --mode        full | schema | data        (default: full)
#   --output-dir  path for local backup files (default: ./backups)
#   --keep        number of local backups to retain (default: 7, 0 = unlimited)
#   --no-s3       skip S3/MinIO upload even if S3_BUCKET is set
#   --label       optional label appended to filename (e.g. "pre-migration")
#   --help        show this message
#
# Environment — Database connection (one of two forms):
#   DATABASE_URL          postgresql[+asyncpg]://user:pass@host:port/dbname
#   PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE  (individual vars)
#
# Environment — S3/MinIO upload (all optional):
#   S3_BUCKET             bucket name           (e.g. my-backups)
#   S3_PREFIX             key prefix            (default: postgres/)
#   S3_ENDPOINT_URL       MinIO / custom endpoint (leave unset for AWS S3)
#   AWS_ACCESS_KEY_ID     AWS / MinIO access key
#   AWS_SECRET_ACCESS_KEY AWS / MinIO secret key
#   AWS_DEFAULT_REGION    region                (default: us-east-1)
#
# Environment — Retention:
#   BACKUP_KEEP           overrides --keep flag
#
# Examples:
#   # Basic full backup with defaults
#   ./scripts/backup/backup.sh
#
#   # Schema-only backup into a specific directory, keep last 14
#   ./scripts/backup/backup.sh --mode schema --output-dir /var/backups/pg --keep 14
#
#   # Full backup with automatic S3 upload
#   S3_BUCKET=acme-db-backups ./scripts/backup/backup.sh
#
#   # Via DATABASE_URL (e.g. from .env)
#   DATABASE_URL=postgresql+asyncpg://app:secret@db:5432/enterprise_agents \
#     ./scripts/backup/backup.sh --label pre-migration
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

info()    { echo -e "$(_ts) ${CYAN}[backup]${NC}  $*"; }
success() { echo -e "$(_ts) ${GREEN}[backup]${NC}  $*"; }
warn()    { echo -e "$(_ts) ${YELLOW}[backup]${NC}  $*" >&2; }
error()   { echo -e "$(_ts) ${RED}[backup]${NC}  $*" >&2; exit 1; }
die()     { echo -e "$(_ts) ${RED}[backup]${NC}  $*" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE="full"
OUTPUT_DIR="./backups"
KEEP="${BACKUP_KEEP:-7}"
SKIP_S3=false
LABEL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      MODE="${2:?--mode requires an argument}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?--output-dir requires an argument}"
      shift 2
      ;;
    --keep)
      KEEP="${2:?--keep requires an argument}"
      shift 2
      ;;
    --no-s3)
      SKIP_S3=true
      shift
      ;;
    --label)
      LABEL="${2:?--label requires an argument}"
      shift 2
      ;;
    --help|-h)
      sed -n '3,50p' "$0"
      exit 0
      ;;
    *)
      die "Unknown argument: $1  (use --help for usage)"
      ;;
  esac
done

case "$MODE" in
  full|schema|data) ;;
  *) die "Invalid --mode '$MODE'. Must be one of: full | schema | data" ;;
esac

# Validate --keep is a non-negative integer
case "$KEEP" in
  ''|*[!0-9]*) die "--keep must be a non-negative integer (got: '$KEEP')" ;;
esac

# ---------------------------------------------------------------------------
# Parse DATABASE_URL into PG* variables (handles asyncpg driver prefix)
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
if ! command -v pg_dump > /dev/null 2>&1; then
  die "pg_dump not found. Install postgresql-client-16 or run inside the db container."
fi

# ---------------------------------------------------------------------------
# Prepare output directory
# ---------------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"
# Resolve to an absolute path so log messages are unambiguous
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# ---------------------------------------------------------------------------
# Build backup filename
#   Format: <db>_<mode>[_<label>]_<timestamp>.dump
# ---------------------------------------------------------------------------
TIMESTAMP="$(date +%Y%m%dT%H%M%S)"
if [ -n "$LABEL" ]; then
  FILENAME="${PGDATABASE}_${MODE}_${LABEL}_${TIMESTAMP}.dump"
  META_FILENAME="${PGDATABASE}_${MODE}_${LABEL}_${TIMESTAMP}.meta.json"
else
  FILENAME="${PGDATABASE}_${MODE}_${TIMESTAMP}.dump"
  META_FILENAME="${PGDATABASE}_${MODE}_${TIMESTAMP}.meta.json"
fi
BACKUP_PATH="${OUTPUT_DIR}/${FILENAME}"
META_PATH="${OUTPUT_DIR}/${META_FILENAME}"

# ---------------------------------------------------------------------------
# Build pg_dump argument list
#   Custom format: supports selective restore, parallel jobs, and is
#   internally compressed — no extra gzip step required.
# ---------------------------------------------------------------------------
PG_DUMP_ARGS=(
  --format=custom
  --compress=9
  --no-password
  --host="$PGHOST"
  --port="$PGPORT"
  --username="$PGUSER"
  --verbose
)

case "$MODE" in
  schema) PG_DUMP_ARGS+=(--schema-only) ;;
  data)   PG_DUMP_ARGS+=(--data-only) ;;
  full)   ;; # default: schema + data
esac

# ---------------------------------------------------------------------------
# Run the backup
# ---------------------------------------------------------------------------
info "Starting $MODE backup of '$PGDATABASE' on $PGHOST:$PGPORT..."
info "Output file: $BACKUP_PATH"

START_EPOCH="$(date +%s)"

if ! pg_dump "${PG_DUMP_ARGS[@]}" "$PGDATABASE" > "$BACKUP_PATH" 2>&1; then
  # Remove the (potentially partial) dump file on failure
  rm -f "$BACKUP_PATH"
  error "pg_dump failed — backup NOT created."
fi

END_EPOCH="$(date +%s)"
DURATION=$(( END_EPOCH - START_EPOCH ))

# ---------------------------------------------------------------------------
# Verify the dump is non-empty and parseable
# ---------------------------------------------------------------------------
if [ ! -s "$BACKUP_PATH" ]; then
  rm -f "$BACKUP_PATH"
  error "Backup file is empty — something went wrong with pg_dump."
fi

if ! pg_restore --list "$BACKUP_PATH" > /dev/null 2>&1; then
  warn "pg_restore --list failed; backup may be incomplete."
fi

# ---------------------------------------------------------------------------
# Collect metadata
# ---------------------------------------------------------------------------
# File size (bytes) — portable across Linux and macOS
BACKUP_SIZE_BYTES="$(wc -c < "$BACKUP_PATH" | tr -d ' ')"
BACKUP_SIZE_HUMAN="$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1 || echo 'unknown')"

TABLE_DATA_COUNT=0
if [ "$MODE" != "schema" ]; then
  TABLE_DATA_COUNT="$(pg_restore --list "$BACKUP_PATH" 2>/dev/null \
    | grep -c 'TABLE DATA' || true)"
fi

PG_DUMP_VERSION="$(pg_dump --version 2>/dev/null | head -1 || echo 'unknown')"

# Alembic revision (if alembic is available)
ALEMBIC_REVISION="unknown"
if command -v alembic > /dev/null 2>&1; then
  ALEMBIC_REVISION="$(alembic current 2>/dev/null | grep -E '^\w{12}' | awk '{print $1}' || echo 'unknown')"
fi

# Write JSON metadata alongside the dump file
python3 - <<PYEOF
import json, datetime

meta = {
    "backup_file": "$BACKUP_PATH",
    "mode": "$MODE",
    "label": "$LABEL",
    "database": "$PGDATABASE",
    "host": "$PGHOST",
    "port": int("$PGPORT"),
    "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
    "duration_seconds": $DURATION,
    "size_bytes": $BACKUP_SIZE_BYTES,
    "size_human": "$BACKUP_SIZE_HUMAN",
    "table_data_objects": $TABLE_DATA_COUNT,
    "pg_dump_version": "$PG_DUMP_VERSION",
    "alembic_revision": "$ALEMBIC_REVISION",
}

with open("$META_PATH", "w") as f:
    json.dump(meta, f, indent=2)
print(json.dumps(meta, indent=2))
PYEOF

# ---------------------------------------------------------------------------
# Optional S3/MinIO upload
# ---------------------------------------------------------------------------
S3_BUCKET="${S3_BUCKET:-}"
if [ -n "$S3_BUCKET" ] && [ "$SKIP_S3" = false ]; then
  if ! command -v aws > /dev/null 2>&1; then
    warn "aws CLI not found — skipping S3 upload. Install awscli to enable."
  else
    S3_PREFIX="${S3_PREFIX:-postgres/}"
    # Normalise: ensure prefix ends with /
    S3_PREFIX="${S3_PREFIX%/}/"

    S3_DUMP_KEY="${S3_PREFIX}${FILENAME}"
    S3_META_KEY="${S3_PREFIX}${META_FILENAME}"

    # Build optional endpoint override for MinIO / non-AWS S3
    S3_ENDPOINT_ARGS=()
    if [ -n "${S3_ENDPOINT_URL:-}" ]; then
      S3_ENDPOINT_ARGS+=(--endpoint-url "$S3_ENDPOINT_URL")
    fi

    export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

    info "Uploading dump to s3://${S3_BUCKET}/${S3_DUMP_KEY} ..."
    if aws s3 cp "${S3_ENDPOINT_ARGS[@]}" \
        "$BACKUP_PATH" "s3://${S3_BUCKET}/${S3_DUMP_KEY}" \
        --storage-class STANDARD_IA; then
      success "Dump uploaded to S3."
    else
      warn "S3 upload failed — local backup is intact."
    fi

    info "Uploading metadata to s3://${S3_BUCKET}/${S3_META_KEY} ..."
    aws s3 cp "${S3_ENDPOINT_ARGS[@]}" \
      "$META_PATH" "s3://${S3_BUCKET}/${S3_META_KEY}" || true
  fi
fi

# ---------------------------------------------------------------------------
# Retention policy — remove old local backups exceeding KEEP count
# ---------------------------------------------------------------------------
if [ "$KEEP" -gt 0 ]; then
  info "Applying retention policy: keep last $KEEP backups (mode=$MODE)..."

  # List dump files for this database+mode, sorted by modification time (oldest first)
  # We use ls -t and reverse so the oldest files are at the top of the delete candidates
  PATTERN="${OUTPUT_DIR}/${PGDATABASE}_${MODE}_*.dump"

  # Count total matching files (glob must expand; guard against no-match)
  TOTAL_DUMPS=0
  for f in $PATTERN; do
    [ -f "$f" ] && TOTAL_DUMPS=$(( TOTAL_DUMPS + 1 ))
  done

  if [ "$TOTAL_DUMPS" -gt "$KEEP" ]; then
    DELETE_COUNT=$(( TOTAL_DUMPS - KEEP ))
    info "Found $TOTAL_DUMPS backups; removing $DELETE_COUNT oldest..."

    # ls -t lists newest first; tail gives us the oldest ones to delete
    # shellcheck disable=SC2012
    ls -t $PATTERN 2>/dev/null | tail -n "$DELETE_COUNT" | while IFS= read -r old_dump; do
      info "  Removing old backup: $old_dump"
      rm -f "$old_dump"
      # Remove associated metadata file if it exists
      old_meta="${old_dump%.dump}.meta.json"
      [ -f "$old_meta" ] && rm -f "$old_meta" && info "  Removed metadata: $old_meta"
    done
  else
    info "Retention OK: $TOTAL_DUMPS backups present (limit: $KEEP)."
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
success ""
success "Backup completed successfully:"
success "  File      : $BACKUP_PATH"
success "  Mode      : $MODE"
success "  Size      : $BACKUP_SIZE_HUMAN  ($BACKUP_SIZE_BYTES bytes)"
success "  Duration  : ${DURATION}s"
success "  Tables    : $TABLE_DATA_COUNT table-data objects"
success "  Alembic   : $ALEMBIC_REVISION"
success "  Metadata  : $META_PATH"
if [ -n "$S3_BUCKET" ] && [ "$SKIP_S3" = false ]; then
  success "  S3 bucket : s3://${S3_BUCKET}/${S3_PREFIX:-postgres/}${FILENAME}"
fi
success ""

exit 0
