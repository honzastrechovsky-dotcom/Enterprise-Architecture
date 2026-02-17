# PostgreSQL Backup & Restore — Enterprise Agent Platform

This directory contains production-grade backup and restore scripts for the
PostgreSQL 16 + pgvector database used by the Enterprise Agent Platform.

## Contents

| Script | Purpose |
|---|---|
| `backup.sh` | Full `pg_dump` with compression, retention, and optional S3/MinIO upload |
| `restore.sh` | `pg_restore` with integrity check, safety confirmation, and post-restore verification |
| `verify.sh` | Restore to a temp database, check all 13 tables, check Alembic revision |
| `cron-backup.sh` | Cron/systemd wrapper: runs backup + optional verify, logs to file, fires alert webhook |

---

## Prerequisites

- PostgreSQL client tools (`pg_dump`, `pg_restore`, `psql`) version 16
  On Debian/Ubuntu: `apt-get install postgresql-client-16`
- Python 3.x (used for `DATABASE_URL` parsing and JSON metadata — already present in the project's venv)
- `aws` CLI (optional, only needed for S3/MinIO upload)

---

## Quick Start

All scripts read the database connection from `DATABASE_URL` or from individual
`PG*` environment variables.  The simplest way is to export `DATABASE_URL`
before calling any script.

```bash
export DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents"

# --- Full backup to ./backups/ ---
./scripts/backup/backup.sh

# --- Restore latest backup (interactive confirmation) ---
./scripts/backup/restore.sh

# --- Verify the latest backup (restore to temp DB, runs table/migration checks) ---
./scripts/backup/verify.sh
```

### Docker Compose

When running inside the Docker Compose stack, pass the database service name:

```bash
# Run backup inside the db container (pg_dump guaranteed to be the right version)
docker compose exec db pg_dump \
  --format=custom --compress=9 \
  --username=app enterprise_agents \
  > backups/enterprise_agents_full_$(date +%Y%m%dT%H%M%S).dump
```

Or call the script from the host if `localhost:5432` is mapped:

```bash
PGHOST=localhost PGPORT=5432 PGUSER=app PGPASSWORD=app_password \
  PGDATABASE=enterprise_agents \
  ./scripts/backup/backup.sh
```

---

## backup.sh

```
Usage: ./scripts/backup/backup.sh [OPTIONS]

Options:
  --mode        full | schema | data   (default: full)
  --output-dir  local backup directory  (default: ./backups)
  --keep        retain last N backups   (default: 7, 0 = unlimited)
  --no-s3       skip S3 upload
  --label       suffix appended to filename  (e.g. pre-migration)
```

Produces two files per run:

- `<db>_<mode>[_<label>]_<timestamp>.dump` — pg_dump custom format (internally compressed)
- `<db>_<mode>[_<label>]_<timestamp>.meta.json` — JSON metadata (size, duration, Alembic revision)

### Backup modes

| Mode | What it dumps |
|---|---|
| `full` | Schema + all data (default) |
| `schema` | DDL only — no row data |
| `data` | Row data only — no DDL |

### Selective restore from a full backup

Because `backup.sh` uses `pg_dump --format=custom`, you can restore individual
tables without restoring the whole database:

```bash
# List all objects in the dump
pg_restore --list backups/enterprise_agents_full_20240315T120000.dump

# Restore only the 'users' and 'tenants' tables
pg_restore \
  --host=localhost --port=5432 --username=app \
  --dbname=enterprise_agents \
  --table=users --table=tenants \
  backups/enterprise_agents_full_20240315T120000.dump
```

---

## restore.sh

```
Usage: ./scripts/backup/restore.sh [OPTIONS] [BACKUP_FILE]

Options:
  --backup-dir  directory to search for the latest backup  (default: ./backups)
  --create-db   drop and recreate the target database before restoring
  --no-confirm  skip interactive confirmation (for CI / automation)
  --jobs N      parallel restore workers  (default: 4)
```

If `BACKUP_FILE` is omitted, the script automatically selects the most recently
modified `*_full_*.dump` file in `--backup-dir`.

Post-restore actions performed automatically:

1. Table row count spot-check (printed to stdout)
2. Alembic revision check against the backup metadata file
3. `alembic stamp head` (if `alembic` CLI is on PATH)

---

## verify.sh

```
Usage: ./scripts/backup/verify.sh [OPTIONS] [BACKUP_FILE]

Options:
  --backup-dir  directory to search for backups  (default: ./backups)
  --no-cleanup  leave temp database after verification (for debugging)
  --jobs N      parallel restore workers  (default: 4)
```

Runs 5 checks:

| # | Check |
|---|---|
| 1 | `pg_restore --list` — file is a valid custom-format dump |
| 2 | Creates a temporary database `<db>_verify_<timestamp>` |
| 3 | Restores the dump into the temp database successfully |
| 4 | All 13 expected application tables are present |
| 5 | `alembic_version` matches the backup metadata |

The temporary database is always dropped on exit (unless `--no-cleanup`).

Exit codes: `0` = all checks passed, `1` = one or more checks failed.

---

## cron-backup.sh

Wraps `backup.sh` (and optionally `verify.sh`) for scheduled execution.
Writes timestamped logs to `LOG_DIR` and fires an optional alert webhook on
failure.

```
Usage: ./scripts/backup/cron-backup.sh [--verify] [--mode full|schema|data]

Environment:
  BACKUP_DIR           output directory    (default: /var/backups/enterprise-agents)
  BACKUP_KEEP          retention count     (default: 7)
  LOG_DIR              log directory       (default: /var/log/pg-backup)
  VERIFY_ON_BACKUP     "true" to enable verify   (default: false)
  ALERT_WEBHOOK_URL    POST JSON status on failure (optional)
```

---

## Cron Setup

### crontab entry (daily at 02:00)

```crontab
# Ensure PATH includes pg_dump and python3
PATH=/usr/lib/postgresql/16/bin:/usr/local/bin:/usr/bin:/bin

# Daily full backup at 02:00 with verification and 14-day retention
0 2 * * * \
  DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
  BACKUP_DIR=/var/backups/enterprise-agents \
  BACKUP_KEEP=14 \
  VERIFY_ON_BACKUP=true \
  /opt/enterprise-agent-platform/scripts/backup/cron-backup.sh \
  >> /var/log/pg-backup/cron.log 2>&1

# Weekly schema-only backup on Sunday at 03:00
0 3 * * 0 \
  DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
  BACKUP_DIR=/var/backups/enterprise-agents \
  /opt/enterprise-agent-platform/scripts/backup/cron-backup.sh \
  --mode schema \
  >> /var/log/pg-backup/cron.log 2>&1
```

### systemd timer (alternative to cron)

Create `/etc/systemd/system/pg-backup.service`:

```ini
[Unit]
Description=Enterprise Agent Platform — PostgreSQL Backup
After=network.target

[Service]
Type=oneshot
User=postgres
EnvironmentFile=/etc/enterprise-agents/backup.env
ExecStart=/opt/enterprise-agent-platform/scripts/backup/cron-backup.sh --verify
StandardOutput=append:/var/log/pg-backup/backup.log
StandardError=append:/var/log/pg-backup/backup.log
```

Create `/etc/systemd/system/pg-backup.timer`:

```ini
[Unit]
Description=Daily PostgreSQL backup timer

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
systemctl daemon-reload
systemctl enable --now pg-backup.timer
systemctl list-timers pg-backup.timer
```

---

## S3 / MinIO Upload

Set these environment variables (or put them in a `.env` file sourced before
calling the script):

```bash
# AWS S3
export S3_BUCKET="my-company-db-backups"
export S3_PREFIX="enterprise-agents/postgres/"
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-1"

# MinIO (self-hosted S3-compatible)
export S3_BUCKET="enterprise-agents-backups"
export S3_PREFIX="postgres/"
export S3_ENDPOINT_URL="http://minio.internal:9000"
export AWS_ACCESS_KEY_ID="minioadmin"
export AWS_SECRET_ACCESS_KEY="minioadmin"
```

The dump file is uploaded with storage class `STANDARD_IA` (infrequent access)
to minimise S3 storage costs for backups that are rarely read.

---

## Alert Webhook

Set `ALERT_WEBHOOK_URL` to receive a JSON POST on backup or verification failure.
The payload format:

```json
{
  "service": "enterprise-agent-platform",
  "event":   "pg-backup",
  "status":  "failure",
  "host":    "db-host-01",
  "message": "PostgreSQL backup failed on db-host-01",
  "detail":  "Mode: full | Duration: 12s | Log: /var/log/pg-backup/backup-20240315T020000.log",
  "timestamp": "2024-03-15T02:00:12Z"
}
```

Compatible with Slack incoming webhooks, PagerDuty Events API v2, and any
generic HTTP alert receiver.

---

## Database Details

| Property | Value |
|---|---|
| PostgreSQL version | 16 |
| Extensions | pgvector |
| Docker service name | `db` |
| Default database | `enterprise_agents` |
| Default user | `app` |
| Default port | `5432` |
| Alembic migrations | 13 (001 through 013) |

### Application Tables (13 total)

| Table | Description |
|---|---|
| `users` | Platform user accounts |
| `tenants` | Multi-tenant organisations |
| `conversations` | Chat / agent conversation sessions |
| `messages` | Individual messages within conversations |
| `documents` | Uploaded documents for RAG |
| `vector_chunks` | pgvector embeddings for RAG retrieval |
| `audit_logs` | Immutable compliance audit trail |
| `agent_registry` | Registered agent definitions |
| `feedback` | User feedback on agent responses |
| `token_budgets` | Per-tenant/user token budget limits |
| `token_usage_records` | Historical token consumption |
| `routing_decisions` | Model routing history |
| `execution_plans` | HITL agent execution plan records |

---

## Troubleshooting

**`pg_dump: error: connection to server on socket failed`**
The database is not reachable. Check `PGHOST`, `PGPORT`, and whether the
Docker container is running: `docker compose ps db`.

**`pg_restore --list` fails on the backup file**
The file is corrupt or was written with `pg_dump --format=plain` (SQL text).
Custom-format backups should always end in `.dump` and begin with the `PGDMP`
magic bytes.  Verify: `file backups/enterprise_agents_full_*.dump`.

**Alembic revision mismatch after restore**
Run `alembic upgrade head` from the project root (with the correct
`DATABASE_URL` set) to bring the schema to the latest migration.

**`CREATE EXTENSION vector` fails in temp DB during verify.sh**
The `pgvector/pgvector:pg16` image bundles the extension.  If using a plain
`postgres:16` image, install it first:
`apt-get install postgresql-16-pgvector`.

**Restore exits non-zero but data looks correct**
`pg_restore` exits 1 for warnings such as "role does not exist" or
"extension already exists".  These are usually harmless — use `--no-owner`
and `--no-privileges` (already set in `restore.sh`) to suppress most of them.
