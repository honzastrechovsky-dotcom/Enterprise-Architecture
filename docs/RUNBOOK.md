# Enterprise Agent Platform — Deployment Runbook

**Platform:** Enterprise Agent Platform v1.0.0
**Customer:** Enterprise Client Infrastructure Team
**Stack:** Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL 16 + pgvector / Redis 7 / React 19 / Helm
**Last updated:** 2026-02-17

---

## Pre-Deployment Checklist

Complete every item below before starting or updating a production deployment.
Items marked with a lock are security-critical and must not be skipped.

### Secrets & Certificates

- [ ] Rotate all secrets: generate new `SECRET_KEY`, `LITELLM_MASTER_KEY`, and `DB_PASSWORD` — never reuse values from staging or a previous deployment
- [ ] Generate or renew TLS certificates; set `TLS_CERT_PATH` and `TLS_KEY_PATH` in the environment (or Kubernetes Secret)
- [ ] Verify `DEV_JWT_SECRET` is set to a strong random value (not the default) — this value must differ from the OIDC secret

### Network & Database

- [ ] Confirm PostgreSQL is reachable from the API pods and the `pgvector` extension is installed: `SELECT * FROM pg_extension WHERE extname = 'vector';`
- [ ] Confirm Redis is reachable and authenticated (if `requirepass` is set)
- [ ] Verify network policies allow API → PostgreSQL, API → Redis, API → LiteLLM proxy, LiteLLM → vLLM, LiteLLM → Ollama

### OIDC / Authentication

- [ ] Test OIDC connectivity: either set `JWKS_LOCAL_PATH=/path/to/jwks.json` for air-gapped mode or verify the IdP discovery URL is reachable at `${OIDC_ISSUER_URL}/.well-known/openid-configuration`
- [ ] Confirm `OIDC_AUDIENCE` matches the `aud` claim issued by your IdP

### LiteLLM / Model Endpoints

- [ ] Set `LITELLM_CONFIG_PATH=litellm_config.prod.yaml` for on-premise deployments (switches from cloud APIs to vLLM/Ollama)
- [ ] Verify vLLM is serving all three model tiers: curl `http://vllm:8000/v1/models` and confirm `qwen2.5:7b`, `qwen2.5:32b`, `qwen2.5:72b` appear
- [ ] Verify Ollama embedding endpoint: `curl http://ollama:11434/api/tags | grep nomic-embed-text`

### Database Migrations

- [ ] Run migrations before starting the API: `alembic upgrade head`
- [ ] Confirm all expected tables exist: `SELECT tablename FROM pg_tables WHERE schemaname = 'public';`

### First Deployment Only

- [ ] Run seed script to create initial tenant and admin user: `python scripts/seed.py`
- [ ] Record the printed JWT tokens and store them in the password manager — they are only shown once in plaintext

### Final Verification

- [ ] Verify the full stack passes health checks: `docker compose -f docker-compose.yml up --wait` (Docker Compose v2.20+ waits for `healthcheck` to pass)
- [ ] Hit the API health endpoint: `curl http://localhost:8000/health` — expect `{"status": "ok"}`
- [ ] Run the smoke test suite: `python -m pytest tests/integration/test_health.py -q`

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Variables Reference](#2-environment-variables-reference)
3. [Docker Compose Deployment](#3-docker-compose-deployment)
4. [Kubernetes / Helm Deployment](#4-kubernetes--helm-deployment)
5. [Database Setup and Migrations](#5-database-setup-and-migrations)
6. [First-time Configuration](#6-first-time-configuration)
7. [Health Check Verification](#7-health-check-verification)
8. [Common Troubleshooting](#8-common-troubleshooting)
9. [Scaling Guide](#9-scaling-guide)
10. [Rollback Procedure](#10-rollback-procedure)

---

## 1. Prerequisites

### 1.1 Hardware Requirements

#### Minimum (Development / Staging)

| Component | CPU | RAM | Storage |
|-----------|-----|-----|---------|
| API nodes | 2 cores | 2 GB | 20 GB |
| PostgreSQL | 2 cores | 4 GB | 50 GB SSD |
| Redis | 1 core | 1 GB | 10 GB |
| Ollama (local LLM, optional) | 8 cores | 32 GB | 100 GB (model storage) |

#### Recommended (Production)

| Component | CPU | RAM | Storage |
|-----------|-----|-----|---------|
| API nodes (x2+) | 4 cores each | 4 GB each | 20 GB each |
| PostgreSQL primary | 8 cores | 16 GB | 500 GB SSD |
| Redis master | 2 cores | 4 GB | 20 GB SSD |
| vLLM inference (Qwen 72B) | 16 cores + 2x A100 GPU | 64 GB | 200 GB |
| LiteLLM proxy | 2 cores | 2 GB | 10 GB |

Note: vLLM GPU inference requires NVIDIA A100 or H100 GPUs with CUDA 12.x drivers installed on the host.

### 1.2 Software Requirements

| Software | Minimum Version | Notes |
|----------|----------------|-------|
| Docker | 24.0 | `docker --version` |
| Docker Compose | 2.20 | `docker compose version` |
| Kubernetes | 1.28 | For Helm deployment |
| Helm | 3.13 | `helm version` |
| kubectl | 1.28 | Matches cluster version |
| Python | 3.12 | For running migration scripts locally |
| NVIDIA Container Toolkit | Latest | Required for GPU workloads |

### 1.3 Access Requirements

Before deployment, ensure you have:

- Docker registry credentials (for pulling the `enterprise-agent-platform` image)
- Kubernetes cluster admin access (`kubectl auth can-i '*' '*' --all-namespaces`)
- Write access to the target namespace
- OIDC / Keycloak admin access (to register the OIDC client)
- Database superuser credentials (to run initial extension setup)
- Cloud LLM API keys if using OpenAI / Anthropic (optional — local Ollama/vLLM is self-contained)

### 1.4 Network Requirements

The following ports must be reachable between components:

| Service | Port | Protocol | Notes |
|---------|------|----------|-------|
| FastAPI | 8000 | TCP | API and WebSocket |
| PostgreSQL | 5432 | TCP | Internal only |
| Redis | 6379 | TCP | Internal only |
| LiteLLM proxy | 4000 | TCP | Internal only |
| Ollama | 11434 | TCP | Internal only (dev/test) |
| vLLM | 8080 | TCP | Internal only (production) |
| Frontend (dev) | 5173 | TCP | Development Vite server |

---

## 2. Environment Variables Reference

All variables are loaded by `src/config.py` via pydantic-settings. They can be set as OS environment variables or in a `.env` file in the project root. In Kubernetes they are projected from a ConfigMap (non-sensitive) or a Secret (sensitive values).

Variables marked **REQUIRED in prod** have no safe default for production and will cause the application to refuse to start if left at the default value.

### 2.1 Application

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `ENVIRONMENT` | `dev` | Yes — set to `prod` | One of `dev`, `prod`, `test`. Controls debug mode, CORS policy, and Swagger UI availability. |
| `SECRET_KEY` | `dev-secret-key-not-for-production` | Yes | HMAC secret for internal token signing. Generate with `openssl rand -hex 32`. |
| `DEBUG` | `false` (auto-set to `true` when `ENVIRONMENT=dev`) | No | Enables verbose error responses. Never enable in production. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Yes | Public HTTPS URL of the API (e.g. `https://agents.tecoc.com`). Used for SAML SP metadata generation. |

### 2.2 Database

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents` | Yes | Async SQLAlchemy connection string. Must use the `asyncpg` driver. |
| `DB_ECHO_SQL` | `false` | No | Set to `true` to log all SQL queries. High verbosity — dev only. |

### 2.3 Redis

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Yes | Redis connection URL for rate limiting, caching, and session storage. Supports `redis://` and `rediss://` (TLS). |

### 2.4 Authentication (OIDC / JWT)

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `OIDC_ISSUER_URL` | `http://localhost:8080/realms/dev` | Yes | OIDC issuer URL. The platform fetches `{OIDC_ISSUER_URL}/.well-known/openid-configuration` to discover JWKS. |
| `OIDC_CLIENT_ID` | `enterprise-agents` | Yes | Expected `azp` (authorized party) claim in JWT tokens. |
| `OIDC_AUDIENCE` | `enterprise-agents-api` | Yes | Expected `aud` claim in JWT tokens. |
| `DEV_JWT_SECRET` | `dev-only-jwt-secret-not-for-production` | Never use in prod | Symmetric HMAC secret for JWT validation in dev/test mode only. Must differ from default if set in prod — but OIDC JWKS is always used in production. |

### 2.5 CORS

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | Yes | JSON-encoded list of allowed CORS origins. Set to your production frontend URL(s). |

### 2.6 LiteLLM Proxy

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `LITELLM_BASE_URL` | `http://localhost:4000` | Yes | Internal URL of the LiteLLM proxy service. |
| `LITELLM_API_KEY` | `sk-dev-key` | Yes | Master API key for the LiteLLM proxy. Must match the proxy's `LITELLM_MASTER_KEY`. |
| `LITELLM_DEFAULT_MODEL` | `openai/gpt-4o-mini` | Yes | Default model for agent tasks. Use `ollama/qwen2.5:32b` for on-premise deployments. |
| `LITELLM_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Yes | Embedding model for RAG document indexing. Dimensions must match `EMBEDDING_DIMENSIONS`. |

### 2.7 Model Routing and Token Economy

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `MODEL_ROUTING_ENABLED` | `true` | No | Enable intelligent three-tier model routing based on task complexity. |
| `MODEL_LIGHT` | `ollama/qwen2.5:7b` | Yes (if routing enabled) | Light tier — intent classification, PII detection. |
| `MODEL_STANDARD` | `ollama/qwen2.5:32b` | Yes (if routing enabled) | Standard tier — most agent reasoning tasks. |
| `MODEL_HEAVY` | `vllm/qwen2.5:72b` | Yes (if routing enabled) | Heavy tier — complex multi-step reasoning, security review. |
| `TOKEN_BUDGET_DAILY` | `1000000` | No | Default daily token budget per tenant. |
| `TOKEN_BUDGET_MONTHLY` | `20000000` | No | Default monthly token budget per tenant. |

### 2.8 RAG / Document Processing

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `CHUNK_SIZE_TOKENS` | `512` | No | Token count per document chunk (range: 64–2048). |
| `CHUNK_OVERLAP_TOKENS` | `50` | No | Token overlap between consecutive chunks (range: 0–256). |
| `VECTOR_TOP_K` | `5` | No | Number of vector search results returned per RAG query (range: 1–20). |
| `EMBEDDING_DIMENSIONS` | `1536` | Yes | Must exactly match the output dimensionality of the configured embedding model. `text-embedding-3-small` = 1536. `nomic-embed-text` = 768. |

### 2.9 Infrastructure / Observability

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `BACKGROUND_WORKER_CONCURRENCY` | `4` | No | Number of concurrent background task coroutines (range: 1–32). Increase for high document ingestion throughput. |
| `RATE_LIMIT_PER_MINUTE` | `60` | No | Maximum API requests per authenticated user per minute. |
| `ENABLE_TELEMETRY` | `false` | No | Enable OpenTelemetry distributed tracing. Requires a running OTLP collector. |
| `OTLP_ENDPOINT` | `http://localhost:4317` | If telemetry enabled | OTLP gRPC collector endpoint. |

### 2.10 Notifications

| Variable | Default | Required in prod | Description |
|----------|---------|-----------------|-------------|
| `SMTP_HOST` | `null` | No | SMTP server hostname. Leave unset to disable email notifications. |
| `SMTP_PORT` | `587` | No | SMTP port. 587 = STARTTLS, 465 = SSL/TLS, 25 = plain. |
| `SMTP_USER` | `null` | If SMTP enabled | SMTP login username. |
| `SMTP_PASSWORD` | `null` | If SMTP enabled | SMTP login password. |
| `SMTP_FROM` | `noreply@enterprise-agents.local` | No | From address on outgoing notifications. |
| `SMTP_USE_TLS` | `false` | No | Use implicit TLS (port 465). When `false`, STARTTLS is negotiated if available. |
| `WEBHOOK_URL` | `null` | No | Generic outbound webhook URL (Slack, Teams, etc.). Leave unset to disable. |

---

## 3. Docker Compose Deployment

Docker Compose is the recommended method for development and staging environments.

### 3.1 Choose Your Compose File

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Full production-like stack including Ollama GPU inference |
| `docker-compose.dev.yml` | Development stack with hot-reload, `.env.dev` file support |

For a local developer setup, use `docker-compose.dev.yml`. For staging or integration testing, use `docker-compose.yml`.

### 3.2 Step-by-step: Development Stack

**Step 1: Clone the repository and navigate to the project root.**

```bash
git clone <repo-url> enterprise-agent-platform-v3
cd enterprise-agent-platform-v3
```

**Step 2: Create the dev environment file.**

```bash
cp .env.example .env.dev   # if an example exists, otherwise create manually
```

Minimum required content for `.env.dev`:

```dotenv
ENVIRONMENT=dev
SECRET_KEY=dev-secret-key-not-for-production
DATABASE_URL=postgresql+asyncpg://app:app_password@db:5432/enterprise_agents
REDIS_URL=redis://redis:6379/0
LITELLM_BASE_URL=http://litellm:4000
LITELLM_API_KEY=sk-dev-key
OIDC_ISSUER_URL=http://localhost:8080/realms/dev

# Optional: cloud LLM keys (leave blank for local Ollama only)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
```

**Step 3: Start the full dev stack.**

```bash
make dev
# Equivalent to: docker compose -f docker-compose.dev.yml up -d --build
```

Services started:
- `api` — FastAPI on port 8000 (hot-reload enabled)
- `db` — PostgreSQL 16 + pgvector on port 5432
- `redis` — Redis 7 on port 6379
- `frontend` — React/Vite dev server on port 5173
- `litellm` — LiteLLM proxy on port 4000

**Step 4: Wait for health checks to pass.**

```bash
make dev-status
# docker compose -f docker-compose.dev.yml ps
```

All services should show `healthy` or `running` within 60 seconds.

**Step 5: Run database migrations.**

```bash
make migrate
# Equivalent to: alembic upgrade head
```

This runs all 13 migration scripts in `alembic/versions/`.

**Step 6: (Optional) Seed sample data.**

```bash
make seed
# Equivalent to: python scripts/seed.py
```

This creates sample tenants, users, conversations, and documents.

**Step 7: Pull Ollama models (if using local LLM inference).**

```bash
docker compose -f docker-compose.dev.yml exec ollama ollama pull qwen2.5:7b
docker compose -f docker-compose.dev.yml exec ollama ollama pull qwen2.5:32b
```

Note: qwen2.5:7b is ~5 GB. qwen2.5:32b is ~20 GB. Ensure sufficient disk space before pulling.

**Step 8: Verify the deployment.**

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

Both endpoints should return HTTP 200.

### 3.3 Step-by-step: Production-like Stack (docker-compose.yml)

**Step 1: Set required environment variables.**

Export the production secrets in your shell or create a `.env` file (not committed to git):

```bash
export SECRET_KEY=$(openssl rand -hex 32)
export LITELLM_API_KEY=$(openssl rand -hex 24)
export DATABASE_URL="postgresql+asyncpg://app:<strong_password>@db:5432/enterprise_agents"
export REDIS_URL="redis://redis:6379/0"
export ENVIRONMENT=prod
export OIDC_ISSUER_URL="https://keycloak.example.com/realms/production"
export CORS_ALLOWED_ORIGINS='["https://agents.example.com"]'
export PUBLIC_BASE_URL="https://agents.example.com"
```

**Step 2: Start services.**

```bash
docker compose up -d
```

**Step 3: Run migrations.**

```bash
docker compose exec api alembic upgrade head
```

**Step 4: Verify health.**

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

### 3.4 Useful Operational Commands

```bash
# Tail all logs
make dev-logs

# Tail API logs only
make dev-logs-api

# Open shell in API container
make dev-shell

# Stop without removing data
make dev-stop

# Full reset (destroys all data volumes)
make dev-reset

# Open psql shell
make db-shell

# Check database health (outputs JSON)
make db-health

# Run vacuum/analyze maintenance
make db-maintenance
```

---

## 4. Kubernetes / Helm Deployment

### 4.1 Prerequisites Check

```bash
# Verify cluster access
kubectl cluster-info
kubectl auth can-i create deployment --namespace enterprise-agents

# Verify Helm
helm version

# Add Bitnami chart repository (required for PostgreSQL and Redis dependencies)
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

### 4.2 Build and Push the Application Image

```bash
# From the repository root
docker build -t <registry>/enterprise-agent-platform:1.0.0 .

# Push to your registry
docker push <registry>/enterprise-agent-platform:1.0.0
```

### 4.3 Create the Target Namespace

```bash
kubectl create namespace enterprise-agents
```

### 4.4 Create Kubernetes Secrets

The Helm chart reads secrets from a Kubernetes Secret object. Create it before installing the chart.

```bash
# Generate strong secrets
SECRET_KEY=$(openssl rand -hex 32)
LITELLM_API_KEY=$(openssl rand -hex 24)
DB_PASSWORD=$(openssl rand -hex 16)

# Create the Kubernetes secret
kubectl create secret generic enterprise-agent-platform \
  --namespace enterprise-agents \
  --from-literal=secret-key="${SECRET_KEY}" \
  --from-literal=litellm-api-key="${LITELLM_API_KEY}" \
  --from-literal=database-url="postgresql+asyncpg://app:${DB_PASSWORD}@enterprise-agent-platform-postgresql:5432/enterprise_agents"
```

For production, use a secrets management solution such as HashiCorp Vault with the external-secrets operator, or Sealed Secrets.

### 4.5 Update Helm Values for Your Environment

Create a `values-override.yaml` file. Do not commit secrets to this file — reference the Kubernetes secret created above.

```yaml
image:
  repository: <registry>/enterprise-agent-platform
  tag: "1.0.0"
  pullPolicy: Always

config:
  environment: prod
  oidcIssuerUrl: "https://keycloak.example.com/realms/production"
  oidcClientId: "enterprise-agents"
  oidcAudience: "enterprise-agents-api"

ingress:
  enabled: true
  className: nginx
  host: agents.example.com
  tls:
    enabled: true
    secretName: enterprise-agent-platform-tls

postgresql:
  auth:
    password: "<strong_db_password>"   # override from vault in CI

redis:
  auth:
    password: "<strong_redis_password>"
```

### 4.6 Install Helm Dependencies

```bash
cd deploy/helm/enterprise-agent-platform
helm dependency update
```

### 4.7 Perform a Dry-run Validation

```bash
helm upgrade --install enterprise-agent-platform \
  deploy/helm/enterprise-agent-platform \
  --namespace enterprise-agents \
  --values deploy/helm/enterprise-agent-platform/values.yaml \
  --values values-override.yaml \
  --dry-run --debug 2>&1 | head -100
```

Review the rendered output and verify all environment variables are correctly populated.

### 4.8 Install / Upgrade the Chart

```bash
helm upgrade --install enterprise-agent-platform \
  deploy/helm/enterprise-agent-platform \
  --namespace enterprise-agents \
  --values deploy/helm/enterprise-agent-platform/values.yaml \
  --values values-override.yaml \
  --wait \
  --timeout 10m
```

The `--wait` flag blocks until all deployments reach a ready state.

### 4.9 Run Database Migrations (Post-install)

After Helm install, run migrations from within the cluster:

```bash
kubectl run migration-job \
  --image=<registry>/enterprise-agent-platform:1.0.0 \
  --namespace=enterprise-agents \
  --restart=Never \
  --env="DATABASE_URL=$(kubectl get secret enterprise-agent-platform -n enterprise-agents -o jsonpath='{.data.database-url}' | base64 -d)" \
  --command -- alembic upgrade head

# Wait for completion
kubectl wait --for=condition=complete pod/migration-job \
  --namespace=enterprise-agents \
  --timeout=120s

# View migration output
kubectl logs migration-job -n enterprise-agents

# Clean up
kubectl delete pod migration-job -n enterprise-agents
```

### 4.10 Verify the Deployment

```bash
# Check pod status
kubectl get pods -n enterprise-agents

# Check services
kubectl get services -n enterprise-agents

# Check HPA status
kubectl get hpa -n enterprise-agents

# Check ingress
kubectl get ingress -n enterprise-agents

# View API pod logs
kubectl logs -l app=enterprise-agent-platform-api -n enterprise-agents --tail=100
```

### 4.11 Multi-region Deployment

For multi-region deployments, apply the `values-multiregion.yaml` overlay:

```bash
helm upgrade --install enterprise-agent-platform \
  deploy/helm/enterprise-agent-platform \
  --namespace enterprise-agents \
  --values deploy/helm/enterprise-agent-platform/values.yaml \
  --values values-override.yaml \
  --values deploy/helm/enterprise-agent-platform/values-multiregion.yaml \
  --set region.name="${REGION_NAME}" \
  --wait
```

Regions supported: `us-east-1`, `eu-west-1`, `ap-southeast-1`. The routing strategy defaults to `residency_first` for GDPR compliance.

---

## 5. Database Setup and Migrations

### 5.1 Required PostgreSQL Extensions

The application requires three PostgreSQL extensions. The `scripts/init-db.sql` file is mounted as an init script in Docker Compose and is also run via the Helm `initdb.scripts` block:

```sql
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector (RAG embeddings)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; -- UUID generation helpers
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- Trigram text search
```

If deploying against an existing PostgreSQL cluster (not the bundled Bitnami chart), run this script manually with a superuser:

```bash
psql -h <host> -U postgres -d enterprise_agents -f scripts/init-db.sql
```

### 5.2 Migration History

Migrations are managed by Alembic. The following migration scripts exist at `alembic/versions/`:

| Revision | Description |
|----------|-------------|
| 001 | Add conversation memories |
| 002 | Add agent memory |
| 003 | Add conversation fields |
| 004 | Add agent traces |
| 005 | Add feedback system |
| 006 | Add plugins |
| 007 | Analytics tables |
| 008 | Ingestion pipeline tables |
| 009 | API keys |
| 010 | Agent memory (extended) |
| 011 | Compliance runs |
| 012 | Budget and metrics tables |
| 013 | Execution plans table |

### 5.3 Running Migrations

**Apply all pending migrations:**

```bash
alembic upgrade head
```

**Apply migrations up to a specific revision:**

```bash
alembic upgrade 007_analytics
```

**Check current migration state:**

```bash
alembic current
```

**View migration history:**

```bash
alembic history --verbose
```

### 5.4 Creating New Migrations

```bash
# Auto-generate from model changes
make migrate-create message="add_new_feature_table"

# Review the generated file in alembic/versions/ before applying
alembic upgrade head
```

Always review auto-generated migrations before running in production. Alembic cannot detect all schema changes (e.g. column type changes with pgvector may require manual intervention).

### 5.5 Database Backup and Restore

```bash
# Full backup (default)
make db-backup

# Schema-only backup
make db-backup BACKUP_MODE=schema BACKUP_DIR=/mnt/backups

# Data-only backup
make db-backup BACKUP_MODE=data BACKUP_DIR=/mnt/backups

# Restore from a backup file
make db-restore BACKUP_FILE=/mnt/backups/enterprise_agents_full_20260217_120000.dump

# Restore and create the database first (fresh install)
make db-restore BACKUP_FILE=/path/to/dump CREATE_DB=1
```

Backup files are created by `scripts/backup.sh` using `pg_dump` in custom format. Restore uses `pg_restore`.

---

## 6. First-time Configuration

### 6.1 OIDC / Keycloak Setup

The platform validates JWT tokens issued by your OIDC provider. If you are using Keycloak:

**Step 1: Log in to the Keycloak admin console.**

**Step 2: Create or select a realm (e.g. `production`).**

**Step 3: Create a new Client.**

- Client ID: `enterprise-agents`
- Client Protocol: `openid-connect`
- Access Type: `confidential`
- Valid Redirect URIs: `https://agents.example.com/*`
- Web Origins: `https://agents.example.com`

**Step 4: Under the client's "Credentials" tab, note the client secret.**

**Step 5: Create an audience mapper.**

- Name: `enterprise-agents-api-audience`
- Mapper Type: `Audience`
- Included Audience: `enterprise-agents-api`

**Step 6: Set the corresponding environment variables in the platform:**

```dotenv
OIDC_ISSUER_URL=https://keycloak.example.com/realms/production
OIDC_CLIENT_ID=enterprise-agents
OIDC_AUDIENCE=enterprise-agents-api
```

**Step 7: Verify OIDC discovery is reachable from the API pod:**

```bash
curl https://keycloak.example.com/realms/production/.well-known/openid-configuration
```

The response must include a `jwks_uri` field.

#### Dev Mode (No Keycloak Required)

In `ENVIRONMENT=dev`, the platform accepts symmetric HMAC JWTs signed with `DEV_JWT_SECRET`. This is for local development only and is never used in production.

### 6.2 LiteLLM Configuration

LiteLLM acts as a unified proxy in front of all LLM providers.

**For cloud LLMs (OpenAI / Anthropic):**

Set the provider API keys in the LiteLLM service environment:

```yaml
# docker-compose.yml or Kubernetes secret
OPENAI_API_KEY: sk-...
ANTHROPIC_API_KEY: sk-ant-...
```

The model list in `litellm_config.yaml` defines which models are exposed:

```yaml
model_list:
  - model_name: openai/gpt-4o-mini
    litellm_params:
      model: gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
```

**For local Ollama models:**

Add Ollama models to `litellm_config.yaml`:

```yaml
  - model_name: ollama/qwen2.5:7b
    litellm_params:
      model: ollama/qwen2.5:7b
      api_base: http://ollama:11434
```

**For vLLM (production):**

```yaml
  - model_name: vllm/qwen2.5:72b
    litellm_params:
      model: openai/qwen2.5:72b
      api_base: http://vllm-service:8080/v1
      api_key: not-required
```

**Verify LiteLLM is working:**

```bash
curl http://localhost:4000/health
curl -s http://localhost:4000/v1/models | python3 -m json.tool
```

### 6.3 Ollama Model Setup

After Ollama starts, pull the required models. These are large downloads — run this step only once, as models persist in the `ollama-models` volume.

```bash
# Pull light-tier model (~5 GB)
docker compose exec ollama ollama pull qwen2.5:7b

# Pull standard-tier model (~20 GB)
docker compose exec ollama ollama pull qwen2.5:32b

# Verify models are available
docker compose exec ollama ollama list
```

For Kubernetes, use an init container or a one-off Job:

```bash
kubectl exec -it deployment/ollama -n enterprise-agents -- ollama pull qwen2.5:7b
```

### 6.4 Frontend Configuration

The React frontend reads its API URL from build-time environment variables:

| Variable | Example | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `https://agents.example.com` | Backend API base URL |
| `VITE_WS_BASE_URL` | `wss://agents.example.com` | WebSocket base URL for chat streaming |

For production builds, set these before running `npm run build`:

```bash
VITE_API_BASE_URL=https://agents.example.com \
VITE_WS_BASE_URL=wss://agents.example.com \
npm run build
```

The built `frontend/dist/` directory is served by the nginx-based frontend container in the Helm chart.

---

## 7. Health Check Verification

### 7.1 API Health Endpoints

| Endpoint | Purpose | Expected Response |
|----------|---------|-------------------|
| `GET /health/live` | Liveness — is the process alive? | `200 OK` |
| `GET /health/ready` | Readiness — are dependencies healthy? | `200 OK` |
| `GET /metrics` | Prometheus metrics scrape endpoint | `200 OK` with text/plain metrics |

The `/health/ready` endpoint checks connectivity to PostgreSQL and Redis. It returns `503` if either dependency is unreachable, causing Kubernetes to stop routing traffic to the pod.

### 7.2 Manual Verification Script

Run this sequence after any deployment:

```bash
# 1. API liveness
curl -sf http://localhost:8000/health/live && echo "PASS: liveness"

# 2. API readiness (checks DB + Redis)
curl -sf http://localhost:8000/health/ready && echo "PASS: readiness"

# 3. Database connectivity
make db-health | python3 -m json.tool

# 4. Redis connectivity
docker compose exec redis redis-cli ping   # should return PONG

# 5. LiteLLM health
curl -sf http://localhost:4000/health && echo "PASS: litellm"

# 6. Ollama (if enabled)
curl -sf http://localhost:11434/ && echo "PASS: ollama"

# 7. Frontend (dev)
curl -sf http://localhost:5173/ | grep -q "Enterprise Agent" && echo "PASS: frontend"
```

### 7.3 Kubernetes Readiness Verification

```bash
# All pods should be Running and Ready
kubectl get pods -n enterprise-agents

# HPA should show current replicas matching desired
kubectl get hpa -n enterprise-agents

# Check recent events for errors
kubectl get events -n enterprise-agents --sort-by='.lastTimestamp' | tail -20

# Check API pod logs for startup errors
kubectl logs -l app=enterprise-agent-platform-api -n enterprise-agents --tail=50

# End-to-end test through ingress
curl -sf https://agents.example.com/health/live
```

### 7.4 Prometheus / Monitoring

If `ENABLE_TELEMETRY=true` and a ServiceMonitor is deployed:

```bash
# Verify metrics are being scraped
kubectl get servicemonitor -n enterprise-agents

# Port-forward and check metrics locally
kubectl port-forward svc/enterprise-agent-platform-api 8000:8000 -n enterprise-agents
curl http://localhost:8000/metrics | grep enterprise_agent
```

---

## 8. Common Troubleshooting

### 8.1 API Pod Fails to Start — "SECRET_KEY must be changed from default"

**Symptom:** The API container exits immediately with a `ValueError`.

**Cause:** `ENVIRONMENT=prod` is set but `SECRET_KEY` or `DEV_JWT_SECRET` still contains the dev default value.

**Fix:**
```bash
# Generate a proper secret
openssl rand -hex 32

# Update the Kubernetes secret
kubectl patch secret enterprise-agent-platform \
  -n enterprise-agents \
  --type merge \
  -p '{"data":{"secret-key":"'$(echo -n "<new_secret>" | base64)'"}}'

# Restart pods
kubectl rollout restart deployment/enterprise-agent-platform-api -n enterprise-agents
```

### 8.2 Database Migrations Fail — "extension vector does not exist"

**Symptom:** Alembic fails with `ProgrammingError: extension "vector" does not exist`.

**Cause:** The `pgvector` extension has not been installed in the database.

**Fix:**
```bash
# Connect as superuser and install the extension
psql -h <db_host> -U postgres -d enterprise_agents \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Re-run migrations
alembic upgrade head
```

For Docker Compose, the `scripts/init-db.sql` file is auto-executed on first container start. If volumes were not cleaned before restart, the init script does not re-run. Use `make dev-reset` to fully reinitialise.

### 8.3 API Returns 502 Bad Gateway from Ingress

**Symptom:** The ingress returns 502 but pods appear running.

**Cause:** Readiness probe is failing, so no pods are in the `Ready` state.

**Diagnosis:**
```bash
kubectl describe pod -l app=enterprise-agent-platform-api -n enterprise-agents
kubectl logs -l app=enterprise-agent-platform-api -n enterprise-agents --previous
```

**Common root causes:**

- Database is not reachable (wrong `DATABASE_URL` or firewall rule).
- Redis is not reachable (wrong `REDIS_URL`).
- OIDC issuer URL is not reachable from within the cluster (DNS resolution failure).

### 8.4 LiteLLM Returns 401 Unauthorized

**Symptom:** API returns LLM errors referencing 401 from LiteLLM.

**Cause:** The `LITELLM_API_KEY` in the application does not match the `LITELLM_MASTER_KEY` in the LiteLLM container.

**Fix:**
```bash
# Verify the keys match
echo "App key: $(kubectl get secret enterprise-agent-platform -n enterprise-agents -o jsonpath='{.data.litellm-api-key}' | base64 -d)"
echo "LiteLLM key: $(kubectl get secret enterprise-agent-platform -n enterprise-agents -o jsonpath='{.data.litellm-master-key}' | base64 -d)"
```

Both values must be identical.

### 8.5 Embedding Dimension Mismatch on Document Ingestion

**Symptom:** Document ingestion fails with `pgvector: expected 1536 dimensions, got 768`.

**Cause:** `EMBEDDING_DIMENSIONS` does not match the actual output of the configured embedding model.

**Common mismatches:**

| Model | Correct Dimensions |
|-------|-------------------|
| `openai/text-embedding-3-small` | 1536 |
| `openai/text-embedding-3-large` | 3072 |
| `nomic-embed-text` (Ollama) | 768 |
| `mxbai-embed-large` (Ollama) | 1024 |

**Fix:** Update `EMBEDDING_DIMENSIONS` to match the model. Note: changing this after documents have already been indexed requires re-ingesting all documents, as the vector column width is fixed.

### 8.6 Redis Connection Refused

**Symptom:** API fails to start with `ConnectionRefusedError` on Redis.

**Fix:**
```bash
# Verify Redis is running
docker compose ps redis

# Verify the URL is correct
echo $REDIS_URL

# Test connectivity
redis-cli -u "$REDIS_URL" ping
```

In Kubernetes, confirm the Redis service name resolves correctly:
```bash
kubectl exec -it deployment/enterprise-agent-platform-api -n enterprise-agents \
  -- python -c "import redis; r=redis.from_url('$REDIS_URL'); print(r.ping())"
```

### 8.7 OIDC Authentication Fails — "JWKS fetch failed"

**Symptom:** All authenticated requests return 401 with "JWKS fetch failed" in logs.

**Cause:** The pod cannot reach the OIDC issuer URL.

**Diagnosis:**
```bash
kubectl exec -it deployment/enterprise-agent-platform-api -n enterprise-agents \
  -- curl -v "${OIDC_ISSUER_URL}/.well-known/openid-configuration"
```

**Common causes:**
- Keycloak is not reachable from within the cluster (network policy blocking egress).
- `OIDC_ISSUER_URL` uses an external hostname that does not resolve inside the cluster.

**Fix:** Use the internal Kubernetes service name for the issuer URL if Keycloak is running in the same cluster (e.g. `http://keycloak.keycloak-namespace.svc.cluster.local/realms/production`).

### 8.8 Ollama Model Not Found

**Symptom:** LiteLLM returns `model not found: ollama/qwen2.5:32b`.

**Cause:** The model has not been pulled into the Ollama service.

**Fix:**
```bash
# Check what models are available
curl http://localhost:11434/api/tags

# Pull the missing model
docker compose exec ollama ollama pull qwen2.5:32b
```

---

## 9. Scaling Guide

### 9.1 API Horizontal Pod Autoscaler (HPA)

The HPA is defined in `deploy/helm/enterprise-agent-platform/templates/hpa.yaml` and configured in `values.yaml`:

```yaml
autoscaling:
  enabled: true
  api:
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
```

Scale-up behavior: doubles replicas every 30 seconds (or adds 2 pods, whichever is larger) until `maxReplicas`.
Scale-down behavior: reduces by at most 50% per 60-second window, with a 5-minute stabilization window to prevent flapping.

**Monitor HPA activity:**
```bash
kubectl get hpa enterprise-agent-platform-api -n enterprise-agents --watch
kubectl describe hpa enterprise-agent-platform-api -n enterprise-agents
```

**Manual scaling override:**
```bash
kubectl scale deployment enterprise-agent-platform-api \
  --replicas=5 \
  -n enterprise-agents
```

### 9.2 Database Connection Pool Tuning

SQLAlchemy's async connection pool is configured via `DATABASE_URL`. The defaults are suitable for development. For production, add pool parameters to the URL:

```dotenv
DATABASE_URL=postgresql+asyncpg://app:password@db:5432/enterprise_agents?pool_size=20&max_overflow=10&pool_pre_ping=true
```

| Parameter | Recommended | Notes |
|-----------|-------------|-------|
| `pool_size` | 20 | Persistent connections per API pod |
| `max_overflow` | 10 | Burst connections beyond pool_size |
| `pool_pre_ping` | true | Test connections before use |
| `pool_recycle` | 3600 | Recycle connections after 1 hour |

Maximum concurrent database connections = `(pool_size + max_overflow) * replica_count`.

Ensure PostgreSQL's `max_connections` (default 100) is set above this value. For high-scale deployments, place PgBouncer in transaction pooling mode in front of PostgreSQL.

### 9.3 Background Worker Scaling

The `BACKGROUND_WORKER_CONCURRENCY` variable controls the number of concurrent background tasks per API pod. Document ingestion and embedding generation are the primary background workloads.

- Default: 4
- Recommended for high document throughput: 8–16
- Maximum: 32

This is a coroutine pool, not a thread pool. CPU-bound tasks benefit less from increasing this value — for CPU-bound workloads, increase replica count instead.

### 9.4 Redis Cluster

The default configuration uses a single Redis instance. For production high availability:

**Option A: Redis Sentinel (Bitnami chart)**

```yaml
redis:
  architecture: replication
  sentinel:
    enabled: true
  replica:
    replicaCount: 2
```

**Option B: Redis Cluster**

```yaml
redis:
  architecture: standalone
  # Use a dedicated Redis Cluster deployment
```

Update `REDIS_URL` to use the Sentinel URL format: `redis+sentinel://:password@redis-headless:26379/mymaster/0`

### 9.5 Rate Limiting at Scale

Rate limiting is implemented using Redis sliding window counters keyed by user ID. At scale:

- Each Redis operation is a single atomic `EVAL` call (Lua script).
- With 1,000 concurrent users at 60 req/min, Redis handles ~1,000 req/s sustained — well within a single Redis instance's capacity.
- If rate limiting becomes a bottleneck, consider increasing `RATE_LIMIT_PER_MINUTE` or sharding Redis by tenant.

---

## 10. Rollback Procedure

### 10.1 Helm Rollback

Helm maintains a release history. To roll back to the previous release:

```bash
# View release history
helm history enterprise-agent-platform -n enterprise-agents

# Roll back to the previous release
helm rollback enterprise-agent-platform -n enterprise-agents

# Roll back to a specific revision
helm rollback enterprise-agent-platform 3 -n enterprise-agents

# Verify the rollback
kubectl get pods -n enterprise-agents
kubectl rollout status deployment/enterprise-agent-platform-api -n enterprise-agents
```

### 10.2 Database Migration Rollback

**Roll back the most recent migration:**

```bash
make db-rollback
# Equivalent to: alembic downgrade -1
```

**Roll back multiple migrations:**

```bash
make db-rollback STEPS=3
```

**Roll back to a specific revision:**

```bash
alembic downgrade 011_compliance_runs
```

Important: Some migrations may be irreversible (e.g. dropping columns that contained data). Always take a database backup before applying or rolling back migrations in production.

### 10.3 Emergency Database Restore

If a migration causes data corruption:

```bash
# Stop the application immediately
kubectl scale deployment enterprise-agent-platform-api --replicas=0 -n enterprise-agents
kubectl scale deployment enterprise-agent-platform-worker --replicas=0 -n enterprise-agents

# Restore from the most recent backup
make db-restore BACKUP_FILE=/path/to/backup.dump

# Verify data integrity
make db-health

# Roll back to the migration state matching the backup
alembic downgrade <target_revision>

# Restart the application
kubectl scale deployment enterprise-agent-platform-api --replicas=2 -n enterprise-agents
kubectl scale deployment enterprise-agent-platform-worker --replicas=2 -n enterprise-agents
```

### 10.4 Docker Compose Rollback

```bash
# Stop the running stack
docker compose down

# Pull the previous image tag
docker pull <registry>/enterprise-agent-platform:1.0.0-previous

# Update docker-compose.yml to reference the previous tag
# Then restart
docker compose up -d

# Roll back the database migration
alembic downgrade -1
```

### 10.5 Rollback Decision Checklist

Before executing a rollback:

- [ ] Is the issue confirmed to be caused by the new deployment (not an external dependency)?
- [ ] Is a database backup available from before the deployment?
- [ ] Have migrations been applied that cannot be reversed?
- [ ] Has the on-call engineer been notified?
- [ ] Has the incident been logged?

If migrations cannot be safely reversed, coordinate with the database administrator before proceeding.
