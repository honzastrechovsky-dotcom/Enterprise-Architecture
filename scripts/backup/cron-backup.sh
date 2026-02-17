#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform — Cron Backup Wrapper
#
# Designed to be invoked from cron or systemd timers.  Orchestrates:
#   1. pg_dump via backup.sh
#   2. Optional backup verification via verify.sh  (--verify flag)
#   3. Structured log output to LOG_DIR
#   4. Exit-code based alerting hook (customise the _alert function below)
#
# This script intentionally does NOT use `set -euo pipefail` at the top level
# so that a failed backup does not silently exit cron; instead all failures are
# caught explicitly and logged before emitting a non-zero exit code.
#
# Usage (manual):
#   ./scripts/backup/cron-backup.sh [--verify] [--mode full|schema|data]
#
# Cron example (runs at 02:00 every day, full backup with verification):
#   0 2 * * * /opt/enterprise-agent-platform/scripts/backup/cron-backup.sh \
#               --verify >> /var/log/pg-backup.log 2>&1
#
# systemd timer: see README.md
#
# Environment:
#   DATABASE_URL          postgresql[+asyncpg]://user:pass@host:port/dbname
#   BACKUP_DIR            output directory    (default: /var/backups/enterprise-agents)
#   BACKUP_KEEP           retention count     (default: 7)
#   S3_BUCKET             upload to S3/MinIO if set
#   ALERT_WEBHOOK_URL     POST JSON status to this URL on failure (optional)
#   LOG_DIR               directory for cron log files (default: /var/log/pg-backup)
#   VERIFY_ON_BACKUP      set to "true" to enable verify after each backup
# =============================================================================

# ---------------------------------------------------------------------------
# Resolve the directory this script lives in so sibling scripts can be found
# regardless of the working directory cron uses.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Defaults — all overridable via environment
# ---------------------------------------------------------------------------
BACKUP_DIR="${BACKUP_DIR:-/var/backups/enterprise-agents}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
LOG_DIR="${LOG_DIR:-/var/log/pg-backup}"
VERIFY_ON_BACKUP="${VERIFY_ON_BACKUP:-false}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BACKUP_MODE="full"
RUN_VERIFY=false

for arg in "$@"; do
  case "$arg" in
    --verify)        RUN_VERIFY=true ;;
    --mode=*)        BACKUP_MODE="${arg#--mode=}" ;;
    --mode)          shift; BACKUP_MODE="${1:-full}" ;;
    --help|-h)
      sed -n '3,45p' "$0"
      exit 0
      ;;
    *)
      echo "[cron-backup] Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

# VERIFY_ON_BACKUP env var can also trigger verification
[ "$VERIFY_ON_BACKUP" = "true" ] && RUN_VERIFY=true

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/backup-$(date +%Y%m%dT%H%M%S).log"
# Also write to a "latest" symlink for easy tailing
LATEST_LOG="${LOG_DIR}/latest.log"

# Redirect both stdout and stderr to the log file AND pass through to
# whatever stdout cron provides (typically mailed to the cron owner).
exec > >(tee -a "$LOG_FILE") 2>&1

# Update symlink to latest log (ignore errors — tmp filesystems may not allow it)
ln -sf "$LOG_FILE" "$LATEST_LOG" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Colour / logging helpers (duplicated so this script has no dependencies)
# ---------------------------------------------------------------------------
_ts()      { date '+%Y-%m-%dT%H:%M:%S'; }
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "$(_ts) ${CYAN}[cron-backup]${NC} $*"; }
success() { echo -e "$(_ts) ${GREEN}[cron-backup]${NC} $*"; }
warn()    { echo -e "$(_ts) ${YELLOW}[cron-backup]${NC} $*"; }
error()   { echo -e "$(_ts) ${RED}[cron-backup]${NC} $*"; }

# ---------------------------------------------------------------------------
# Alert hook
#   Called on failure.  Sends a JSON payload to ALERT_WEBHOOK_URL when set.
#   Replace or extend this function to integrate with PagerDuty, Slack, etc.
# ---------------------------------------------------------------------------
_alert() {
  local subject="$1"
  local body="$2"
  local status="${3:-failure}"   # "failure" | "success"

  error "ALERT: $subject"
  [ -n "$body" ] && error "  Detail: $body"

  if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
    # Build a minimal JSON payload — uses python3 for proper escaping
    PAYLOAD="$(python3 - "$subject" "$body" "$status" "$HOSTNAME" <<'PYEOF'
import json, sys, datetime

payload = {
    "service": "enterprise-agent-platform",
    "event":   "pg-backup",
    "status":  sys.argv[3],
    "host":    sys.argv[4],
    "message": sys.argv[1],
    "detail":  sys.argv[2],
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
}
print(json.dumps(payload))
PYEOF
)"
    if command -v curl > /dev/null 2>&1; then
      curl -s -X POST "$ALERT_WEBHOOK_URL" \
        -H 'Content-Type: application/json' \
        -d "$PAYLOAD" \
        --max-time 10 > /dev/null 2>&1 \
        && info "Alert webhook delivered." \
        || warn "Alert webhook delivery failed (URL: $ALERT_WEBHOOK_URL)."
    else
      warn "curl not available — alert webhook not sent."
    fi
  fi
}

# ---------------------------------------------------------------------------
# Overall result tracking
# ---------------------------------------------------------------------------
OVERALL_EXIT=0

# ---------------------------------------------------------------------------
# Step 1 — Run backup.sh
# ---------------------------------------------------------------------------
info "====================================================================="
info "Starting scheduled $BACKUP_MODE backup"
info "  Script dir  : $SCRIPT_DIR"
info "  Backup dir  : $BACKUP_DIR"
info "  Retention   : keep last $BACKUP_KEEP backups"
info "  Verify      : $RUN_VERIFY"
info "  Log file    : $LOG_FILE"
info "====================================================================="

BACKUP_SCRIPT="${SCRIPT_DIR}/backup.sh"
[ -x "$BACKUP_SCRIPT" ] || chmod +x "$BACKUP_SCRIPT"

BACKUP_START="$(date +%s)"
BACKUP_FILE=""

if BACKUP_OUTPUT="$("$BACKUP_SCRIPT" \
    --mode "$BACKUP_MODE" \
    --output-dir "$BACKUP_DIR" \
    --keep "$BACKUP_KEEP" \
    2>&1)"; then
  BACKUP_END="$(date +%s)"
  echo "$BACKUP_OUTPUT"
  success "Backup completed in $(( BACKUP_END - BACKUP_START ))s."

  # Extract the backup file path from the output line "  File      : <path>"
  BACKUP_FILE="$(echo "$BACKUP_OUTPUT" \
    | grep -E '^\S.*File\s+:' \
    | awk -F': ' '{print $2}' \
    | head -1 \
    | xargs)"

  [ -n "$BACKUP_FILE" ] \
    && info "Backup file: $BACKUP_FILE" \
    || warn "Could not parse backup file path from output."
else
  BACKUP_END="$(date +%s)"
  echo "$BACKUP_OUTPUT"
  error "Backup FAILED after $(( BACKUP_END - BACKUP_START ))s."
  _alert \
    "PostgreSQL backup failed on $(hostname)" \
    "Mode: $BACKUP_MODE | Duration: $(( BACKUP_END - BACKUP_START ))s | Log: $LOG_FILE" \
    "failure"
  OVERALL_EXIT=1
fi

# ---------------------------------------------------------------------------
# Step 2 — Optional backup verification
# ---------------------------------------------------------------------------
if [ "$RUN_VERIFY" = true ] && [ "$OVERALL_EXIT" -eq 0 ]; then
  info "====================================================================="
  info "Running backup verification..."
  info "====================================================================="

  VERIFY_SCRIPT="${SCRIPT_DIR}/verify.sh"
  [ -x "$VERIFY_SCRIPT" ] || chmod +x "$VERIFY_SCRIPT"

  VERIFY_ARGS=("--backup-dir" "$BACKUP_DIR")
  [ -n "$BACKUP_FILE" ] && VERIFY_ARGS=("$BACKUP_FILE")

  VERIFY_START="$(date +%s)"
  if "$VERIFY_SCRIPT" "${VERIFY_ARGS[@]}" 2>&1; then
    VERIFY_END="$(date +%s)"
    success "Verification PASSED in $(( VERIFY_END - VERIFY_START ))s."
  else
    VERIFY_END="$(date +%s)"
    error "Verification FAILED after $(( VERIFY_END - VERIFY_START ))s."
    _alert \
      "PostgreSQL backup verification failed on $(hostname)" \
      "Backup file: ${BACKUP_FILE:-unknown} | Log: $LOG_FILE" \
      "failure"
    OVERALL_EXIT=1
  fi
elif [ "$RUN_VERIFY" = true ] && [ "$OVERALL_EXIT" -ne 0 ]; then
  warn "Skipping verification because backup step failed."
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
info "====================================================================="
if [ "$OVERALL_EXIT" -eq 0 ]; then
  success "Cron backup job COMPLETED SUCCESSFULLY."
  success "  Log : $LOG_FILE"
  # Optional success notification (uncomment and adapt as needed)
  # _alert "PostgreSQL backup succeeded on $(hostname)" "" "success"
else
  error "Cron backup job COMPLETED WITH ERRORS — see log: $LOG_FILE"
fi
info "====================================================================="

exit "$OVERALL_EXIT"
