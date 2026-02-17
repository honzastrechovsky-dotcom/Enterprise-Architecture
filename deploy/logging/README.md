# Log Aggregation Stack

Loki + Promtail log collection for the Enterprise Agent Platform.

Collects structured JSON logs from all Docker containers, parses
structlog fields, and makes them queryable via Grafana or raw LogQL.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Docker containers (api, db, redis, ollama, litellm, ...)   │
│  Each writes structlog JSON to stdout/stderr                 │
└──────────────────────────┬──────────────────────────────────┘
                           │ Docker socket
                           ▼
                    ┌─────────────┐
                    │   Promtail  │  port 9080
                    │  (collector)│
                    └──────┬──────┘
                           │ HTTP push
                           ▼
                    ┌─────────────┐
                    │    Loki     │  port 3100
                    │  (storage)  │
                    └──────┬──────┘
                           │ HTTP datasource
                           ▼
                    ┌─────────────┐
                    │   Grafana   │  port 3000
                    │  (queries)  │
                    └─────────────┘
```

---

## Quick Start

### 1. Start the full stack

```bash
# From the project root
docker compose \
  -f docker-compose.yml \
  -f deploy/logging/docker-compose.logging.yml \
  up -d
```

### 2. Verify services are healthy

```bash
# Loki readiness
curl -s http://localhost:3100/ready

# Promtail targets
curl -s http://localhost:9080/targets | python3 -m json.tool

# Loki labels discovered so far
curl -s http://localhost:3100/loki/api/v1/labels | python3 -m json.tool
```

### 3. Open Grafana

Navigate to `http://localhost:3000` and log in with:
- Username: `admin`
- Password: value of `GRAFANA_ADMIN_PASSWORD` env var (default: `admin`)

The Loki datasource is pre-provisioned. Open the Explore view and
select the **Loki** datasource to start querying.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana admin password (change in production) |

---

## Log Labels

Promtail promotes the following fields as Loki index labels.
Labels must have bounded cardinality — use JSON field filters for
high-cardinality values like `user_id` and `request_id`.

| Label | Source | Cardinality | Notes |
|---|---|---|---|
| `service` | Docker compose service name | Low (~10) | `api`, `db`, `redis`, etc. |
| `container` | Docker container name | Low (~10) | Full container name |
| `level` | structlog `level` field | Very low (5) | `debug`, `info`, `warning`, `error`, `critical` |
| `logger` | structlog `logger` field | Low (~50) | Python module path |
| `tenant_id` | structlog `tenant_id` field | Medium | UUID — bounded in enterprise use |
| `logstream` | Docker log stream | Very low (2) | `stdout` or `stderr` |
| `job` | Static | Very low | Always `docker-containers` |

Additional fields available via JSON filtering (not labels):
- `request_id`, `user_id`, `agent_id`, `trace_id`, `span_id`, `event`

---

## LogQL Query Examples

### Basic service log stream

```logql
{service="api"}
```

### All errors across all services

```logql
{job="docker-containers"} | json | level="error"
```

### Logs for a specific tenant

```logql
{service="api"} | json | tenant_id="<uuid>"
```

### Trace a single request end-to-end

```logql
{job="docker-containers"} | json | request_id="req_<hex>"
```

### Filter by structlog event prefix

```logql
{service="api"} | json | event=~"agent\\..*"
```

### Error rate (Grafana metric query)

```logql
sum by (service) (
  rate({job="docker-containers"} | json | level="error" [5m])
)
```

### Slow requests (parse latency_ms field)

```logql
{service="api"}
  | json
  | latency_ms > 1000
  | line_format "{{.event}} latency={{.latency_ms}}ms tenant={{.tenant_id}}"
```

### Recent agent execution events

```logql
{service="api"}
  | json
  | event=~"agent\\.execute\\..*"
  | line_format "{{.timestamp}} [{{.level}}] {{.event}} agent={{.agent_id}} tenant={{.tenant_id}}"
```

### Exceptions and tracebacks

```logql
{service="api"} |= "Traceback"
```

---

## Grafana Dashboard Tips

### Adding a Loki panel

1. Create a new dashboard or open an existing one.
2. Add a **Logs** visualization panel.
3. Set datasource to **Loki**.
4. Enter a LogQL query (see examples above).
5. Enable **Deduplication** for repeated log lines.
6. Enable **Wrap lines** for long JSON payloads.

### Correlating logs with Prometheus metrics

Both the Prometheus datasource (via `/metrics`) and Loki are available
in Grafana. Use **Explore** split-view to show metrics alongside logs
for the same time range:

1. Open Explore.
2. Select **Prometheus** in the left pane.
3. Click the split button.
4. Select **Loki** in the right pane.
5. Zoom into an anomaly in the metric, and the log pane updates to match.

### Linking trace IDs to Jaeger/Tempo

If you add Tempo to the stack, update `grafana-datasources.yml`:

```yaml
derivedFields:
  - name: TraceID
    matcherRegex: '"trace_id":"([a-f0-9]+)"'
    url: http://tempo:3200/trace/${__value.raw}
    datasourceUid: tempo
```

This makes every `trace_id` in a log line a clickable link to the trace.

---

## Retention

Logs are retained for **30 days** (720 hours). This is configured in
`loki-config.yml` under `limits_config.retention_period` and enforced
by the Loki compactor process.

To change retention:

```yaml
# loki-config.yml
limits_config:
  retention_period: 720h   # 30 days — adjust as needed
```

Restart Loki after changes: `docker compose restart loki`

---

## Production Considerations

### Security

- Change `GRAFANA_ADMIN_PASSWORD` in production.
- Loki has no built-in auth (`auth_enabled: false`). Place it behind a
  reverse proxy with authentication (nginx + basic auth, or Grafana's
  own auth proxy).
- Restrict port `3100` to internal networks only — do not expose to the internet.

### Storage

Loki stores data under the `loki-data` Docker volume (filesystem backend).
For production scale, replace with an object store (S3, GCS, Azure Blob):

```yaml
# loki-config.yml
common:
  storage:
    s3:
      endpoint: s3.amazonaws.com
      bucketnames: your-loki-bucket
      region: us-east-1
```

### High cardinality labels

Avoid adding `user_id` or `request_id` as Loki labels — these have
unbounded cardinality and will degrade index performance. Filter them
using `| json | user_id="<value>"` instead.

### Scaling Promtail

For multi-host deployments, run Promtail as a DaemonSet (Kubernetes)
or deploy one Promtail instance per Docker host.

---

## Troubleshooting

### Promtail not finding containers

```bash
# Check Promtail targets
curl -s http://localhost:9080/targets

# Check Docker socket is accessible
docker exec promtail ls -la /var/run/docker.sock
```

### Logs not appearing in Loki

```bash
# Check Promtail metrics for push errors
curl -s http://localhost:9080/metrics | grep promtail_sent_bytes_total

# Check Loki ingestion metrics
curl -s http://localhost:3100/metrics | grep loki_ingester_chunks_created_total
```

### JSON not being parsed

Verify that the `api` container is running in production mode
(`ENVIRONMENT=prod`), which enables `json_logs=True` in `configure_logging()`.
In development mode, structlog outputs colorized console text which
Promtail collects as plain text (labels still apply; JSON fields will not).

### Loki out of disk space

```bash
# Check volume usage
docker system df -v | grep loki

# Manually trigger compaction
curl -X POST http://localhost:3100/loki/api/v1/admin/compaction/run
```

---

## File Reference

| File | Purpose |
|---|---|
| `docker-compose.logging.yml` | Loki, Promtail, and Grafana services |
| `loki-config.yml` | Loki storage, retention, and limits |
| `promtail-config.yml` | Docker log scraping and JSON pipeline |
| `grafana-datasources.yml` | Auto-provision Loki datasource in Grafana |
| `grafana-dashboards.yml` | Auto-provision dashboard folder in Grafana |
