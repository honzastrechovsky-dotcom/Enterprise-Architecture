# Load Testing — Enterprise Agent Platform

Performance validation scripts for Phase 8C.

Two complementary tools cover identical scenarios so results can be
cross-validated:

| Tool   | File             | Best for                                      |
|--------|------------------|-----------------------------------------------|
| Locust | `locustfile.py`  | Interactive UI, Python-friendly, CI headless  |
| k6     | `k6_script.js`   | Low-overhead VUs, CI/CD pipelines, InfluxDB   |

---

## Prerequisites

### Locust

```bash
# Install into the project virtualenv (Python 3.12)
pip install locust PyJWT

# Verify
locust --version
```

### k6

```bash
# macOS
brew install k6

# Linux (Debian/Ubuntu)
sudo gpg -k
sudo gpg --no-default-keyring \
     --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
     --keyserver hkp://keyserver.ubuntu.com:80 \
     --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install k6

# Docker
docker pull grafana/k6:latest
```

---

## Environment Variables

Both tools read the same environment variables:

| Variable              | Default                                      | Description                             |
|-----------------------|----------------------------------------------|-----------------------------------------|
| `TARGET_HOST`         | `http://localhost:8000`                      | Base URL of the platform under test     |
| `DEV_JWT_SECRET`      | `dev-only-jwt-secret-not-for-production`     | HS256 secret (must match app config)    |
| `LOAD_TEST_TENANT_ID` | `6ba7b810-9dad-11d1-80b4-00c04fd430c8`       | UUID of tenant used by load test users  |

Export them before running:

```bash
export TARGET_HOST=http://localhost:8000
export DEV_JWT_SECRET=dev-only-jwt-secret-not-for-production
export LOAD_TEST_TENANT_ID=<your-test-tenant-uuid>
```

> The test tenant and users (`load-test-admin-user`, `load-test-operator-user`,
> `load-test-viewer-user`) are auto-provisioned by the application on first
> request in `dev` mode. In production, create them via the admin API first.

---

## Running Locust

### Interactive Web UI (recommended for exploration)

```bash
locust -f tests/load/locustfile.py \
       --host "$TARGET_HOST" \
       --web-port 8089
# Open http://localhost:8089, set users=50, spawn-rate=5, then Start
```

### Headless / CI mode

```bash
# 50 users, spawn 5/s, run for 5 minutes
locust -f tests/load/locustfile.py \
       --headless \
       --host "$TARGET_HOST" \
       --users 50 \
       --spawn-rate 5 \
       --run-time 5m \
       --html tests/load/results/locust_report.html \
       --csv  tests/load/results/locust
```

### Run only specific tagged tasks

```bash
# Health checks only
locust -f tests/load/locustfile.py --headless \
       --host "$TARGET_HOST" \
       --users 10 --spawn-rate 2 --run-time 2m \
       --tags health

# Chat tasks only
locust -f tests/load/locustfile.py --headless \
       --host "$TARGET_HOST" \
       --users 20 --spawn-rate 5 --run-time 3m \
       --tags chat
```

### Distributed Locust (master + workers)

```bash
# Terminal 1 — master
locust -f tests/load/locustfile.py \
       --master \
       --host "$TARGET_HOST" \
       --web-port 8089

# Terminal 2+ — workers (one per CPU core recommended)
locust -f tests/load/locustfile.py \
       --worker \
       --master-host localhost
```

---

## Running k6

### Basic run (built-in scenario config)

```bash
k6 run tests/load/k6_script.js \
   -e TARGET_HOST="$TARGET_HOST" \
   -e DEV_JWT_SECRET="$DEV_JWT_SECRET" \
   -e LOAD_TEST_TENANT_ID="$LOAD_TEST_TENANT_ID"
```

### Save results to JSON

```bash
k6 run tests/load/k6_script.js \
   -e TARGET_HOST="$TARGET_HOST" \
   --out json=tests/load/results/k6_results.json \
   --summary-trend-stats p(50),p(95),p(99),max
```

### Override VU count and duration (quick smoke test)

```bash
k6 run tests/load/k6_script.js \
   -e TARGET_HOST="$TARGET_HOST" \
   --vus 5 --duration 1m
```

### Run in Docker

```bash
docker run --rm \
  -v "$(pwd)/tests/load":/scripts \
  -e TARGET_HOST="$TARGET_HOST" \
  -e DEV_JWT_SECRET="$DEV_JWT_SECRET" \
  grafana/k6:latest run /scripts/k6_script.js
```

---

## Running via Docker Compose

### Start the full stack + k6

```bash
# Build the app image first
docker compose -f tests/load/docker-compose.load.yml build app

# Bring up platform services
docker compose -f tests/load/docker-compose.load.yml up -d postgres redis app

# Wait for readiness
docker compose -f tests/load/docker-compose.load.yml exec app \
  curl -s http://localhost:8000/health/ready

# Run k6 once
docker compose -f tests/load/docker-compose.load.yml run --rm k6
```

### Locust via Docker Compose

```bash
# Start Locust master + 2 workers + web UI on port 8089
docker compose -f tests/load/docker-compose.load.yml \
  up locust-master locust-worker

# Headless run (CI)
docker compose -f tests/load/docker-compose.load.yml run --rm \
  locust-worker locust \
    -f /tests/load/locustfile.py \
    --headless --users 50 --spawn-rate 5 --run-time 5m \
    --host http://app:8000
```

### Observability stack (InfluxDB + Grafana)

```bash
# Start full observability profile
docker compose -f tests/load/docker-compose.load.yml \
  --profile observability up -d

# Run k6 with InfluxDB output
docker compose -f tests/load/docker-compose.load.yml \
  --profile observability run --rm k6-influx

# Open Grafana at http://localhost:3001
# Import the official k6 dashboard: https://grafana.com/grafana/dashboards/2587
```

---

## Load Test Scenarios

### Scenario 1 — Health Check

Endpoint: `GET /health/live`, `GET /health/ready`

- No auth required
- Always-on background polling (5 VUs in k6, CasualUser weight=3 in Locust)
- Validates the DB connectivity heartbeat

Threshold: **p95 < 500 ms**

### Scenario 2 — Chat (non-streaming)

Endpoint: `POST /api/v1/chat`

Request:
```json
{
  "message": "What are the key performance indicators for our Q4 production metrics?",
  "conversation_id": "<optional-uuid>"
}
```

Response:
```json
{
  "response": "...",
  "conversation_id": "uuid",
  "citations": [...],
  "model_used": "openai/gpt-4o-mini",
  "latency_ms": 1234
}
```

Auth: Bearer JWT, role `operator`
Threshold: **p95 < 2000 ms** (LLM-bound)

### Scenario 3 — Streaming Chat (SSE)

Endpoint: `POST /api/v1/chat/stream`

Same request body as non-streaming chat.
Response: `text/event-stream` SSE chunks.

Auth: Bearer JWT, role `operator`
Threshold: **p95 < 3000 ms** for first-byte

### Scenario 4 — Plan CRUD Lifecycle

Endpoints:
1. `POST /api/v1/plans` — create plan (role `admin`)
2. `GET /api/v1/plans/{id}` — fetch plan
3. `POST /api/v1/plans/{id}/approve` — approve (role `admin`)
4. `GET /api/v1/plans/{id}/status` — execution status

Create request:
```json
{
  "goal": "Analyse the last 30 days of production downtime events...",
  "context": "load test"
}
```

Auth: Bearer JWT, role `admin`
Threshold: **p95 < 5000 ms** (LLM decomposition step)

### Scenario 5 — Agent Analytics

Endpoint: `GET /api/v1/analytics/agents`

Auth: Bearer JWT, role `admin`

---

## Interpreting Results

### k6 Output

```
scenarios: (100.00%) 5 scenarios
  ✓ health_check
  ✓ chat_baseline
  ✓ chat_streaming
  ✓ plan_lifecycle
  ✓ agent_analytics

checks.........................: 99.23%
http_req_duration p(95)........: 1843ms  [chat_baseline]
http_req_failed................: 0.41%   < 1% threshold
load_test_error_rate...........: 0.41%
```

Key metrics to examine:

| Metric                   | Good      | Warning     | Critical    |
|--------------------------|-----------|-------------|-------------|
| `health p(95)`           | < 100 ms  | 100-500 ms  | > 500 ms    |
| `chat p(95)`             | < 1000 ms | 1-2000 ms   | > 2000 ms   |
| `http_req_failed rate`   | < 0.1 %   | 0.1-1 %     | > 1 %       |
| `checks passing`         | > 99 %    | 95-99 %     | < 95 %      |

### Locust Output

The HTML report (`--html`) and CSV files contain:
- **Response time percentiles** per endpoint
- **Requests/second** throughput over time
- **Failure count** and error messages
- **User count** ramp chart

Common failure modes and remediation:

| Symptom                              | Likely cause                     | Action                            |
|--------------------------------------|----------------------------------|-----------------------------------|
| `401 Unauthorized`                   | Wrong `DEV_JWT_SECRET`           | Check env var matches app config  |
| `403 Forbidden`                      | Role lacks permission            | Use `admin` role for plan tasks   |
| `429 Too Many Requests`              | Rate limiter hit (60 req/min/user) | Spread load across more user IDs |
| `502 Bad Gateway`                    | LiteLLM proxy down               | Start the LLM proxy service       |
| `503 Service Unavailable`            | LLM provider rate limit          | Reduce VU count or use mock LLM   |
| p95 chat > 2000 ms under low load    | LLM model latency                | Switch to faster model or local   |
| DB connection timeouts               | Pool exhausted                   | Increase `DB_POOL_SIZE`           |

### Reading the Prometheus `/metrics` Endpoint

```bash
curl -s http://localhost:8000/metrics | grep -E 'http_request|rate_limit'
```

Key Prometheus counters emitted by the platform:

- `http_requests_total{method, endpoint, status}` — request count
- `http_request_duration_seconds{endpoint}` — response time histogram
- `rate_limit_hits_total{user_id}` — rate limiter activations

---

## CI Integration (GitHub Actions)

Add to `.github/workflows/`:

```yaml
jobs:
  load-test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: app_password
          POSTGRES_DB: enterprise_agents
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-retries 10
      redis:
        image: redis:7-alpine
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - name: Start app
        run: |
          pip install -e ".[dev]"
          uvicorn src.main:app --port 8000 &
          sleep 10
        env:
          ENVIRONMENT: dev
          DATABASE_URL: postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents
          REDIS_URL: redis://localhost:6379/0

      - name: Install k6
        run: |
          sudo gpg --no-default-keyring \
            --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
            --keyserver hkp://keyserver.ubuntu.com:80 \
            --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
          echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] \
            https://dl.k6.io/deb stable main" \
            | sudo tee /etc/apt/sources.list.d/k6.list
          sudo apt-get update && sudo apt-get install -y k6

      - name: Run k6 smoke test
        run: |
          k6 run tests/load/k6_script.js \
            --vus 5 --duration 1m \
            -e TARGET_HOST=http://localhost:8000 \
            -e DEV_JWT_SECRET=dev-only-jwt-secret-not-for-production
```

---

## File Reference

```
tests/load/
├── locustfile.py            Locust scenarios (Python, PyJWT)
├── k6_script.js             k6 scenarios (JS, built-in HS256)
├── docker-compose.load.yml  Full stack for isolated load testing
├── README.md                This document
└── results/                 Output directory (git-ignored)
    ├── locust_report.html
    ├── locust_stats.csv
    ├── locust_failures.csv
    └── k6_results.json
```

Add `tests/load/results/` to `.gitignore` to avoid committing large result files.
