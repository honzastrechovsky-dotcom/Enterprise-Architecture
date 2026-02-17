# Observability Stack

The Enterprise Agent Platform includes comprehensive observability via Prometheus metrics, OpenTelemetry tracing, and structured logging.

## Architecture

```
┌─────────────────┐
│  FastAPI App    │
└────────┬────────┘
         │
         ├─> Prometheus Metrics (/metrics)
         ├─> OpenTelemetry Traces (OTLP)
         └─> Structured JSON Logs (stdout)
              ├─> trace_id correlation
              ├─> request_id tracking
              └─> tenant_id scoping
```

## Components

### 1. Prometheus Metrics (`/metrics`)

**Endpoint**: `GET /metrics`

Exports metrics in Prometheus exposition format for scraping.

**HTTP Metrics**:
- `http_requests_total{method, endpoint, status}` - Total HTTP requests
- `http_request_duration_seconds{method, endpoint}` - Request latency histogram
- `active_connections` - Current active HTTP connections

**LLM Metrics**:
- `llm_requests_total{model, status}` - Total LLM API calls
- `llm_request_duration_seconds{model}` - LLM request latency histogram
- `llm_tokens_total{model, token_type}` - Token consumption (prompt/completion)

**Agent Metrics**:
- `active_agent_runs` - Concurrent agent executions
- `agent_run_duration_seconds{agent_type, status}` - Agent execution latency
- `agent_steps_total{agent_type}` - Total agent steps executed

**Token Budget Metrics**:
- `token_budget_remaining{tenant_id, period}` - Remaining tokens (daily/monthly)
- `token_budget_used_total{tenant_id, period}` - Total tokens consumed

**Tool Metrics**:
- `tool_calls_total{tool_name, success}` - Tool invocations
- `tool_duration_seconds{tool_name}` - Tool execution latency

**Implementation**: `src/middleware/prometheus.py`

### 2. OpenTelemetry Distributed Tracing

**OTLP Exporter**: Sends traces to OpenTelemetry Collector (configurable endpoint)

**Automatic Instrumentation**:
- FastAPI requests (via `opentelemetry-instrumentation-fastapi`)
- HTTP client calls (via `opentelemetry-instrumentation-httpx`)
- Database queries (via `opentelemetry-instrumentation-sqlalchemy`)

**Manual Spans**:
- Agent execution (`trace_agent_execution`)
- LLM calls (`trace_llm_call`)
- Tool execution (`trace_tool_execution`)

**Trace Context Propagation**:
- Spans include `tenant_id`, `user_id`, `agent_id` attributes
- Parent-child relationships tracked automatically
- Context propagates across async boundaries

**Configuration**:
```bash
ENABLE_TELEMETRY=true
OTLP_ENDPOINT=http://localhost:4317
```

**Implementation**: `src/infra/telemetry.py`

### 3. Structured Logging

**Format** (Production):
```json
{
  "timestamp": "2026-02-17T10:30:45.123456Z",
  "level": "info",
  "event": "agent.execute.started",
  "trace_id": "abc123def456...",
  "span_id": "789012345678...",
  "request_id": "req_abc123",
  "tenant_id": "tenant-uuid",
  "user_id": "user-uuid",
  "agent_id": "agent-uuid",
  "message": "Starting agent execution"
}
```

**Features**:
- JSON output in production, human-readable in dev
- Automatic trace correlation (trace_id, span_id from OpenTelemetry)
- Request ID tracking (generated per request, added to response headers)
- Tenant and user context binding
- ISO8601 timestamps with timezone

**Context Binding**:
```python
from src.telemetry.logging import bind_tenant_context, bind_user_context

bind_tenant_context(tenant_id)
bind_user_context(user_id)
# All logs in this request now include tenant_id and user_id
```

**Implementation**: `src/telemetry/logging.py`

### 4. Enhanced Health Checks

**Endpoints**:
- `GET /health/live` - Liveness probe (process alive?)
- `GET /health/ready` - Readiness probe (can serve traffic?)
- `GET /health` - Detailed health with component status

**Components Checked**:
- Database (PostgreSQL connection + query)
- Redis (PING command)
- LLM Proxy (LiteLLM health endpoint)
- Disk space (temp directory usage)

**Response Format**:
```json
{
  "status": "healthy",
  "timestamp": "2026-02-17T10:30:45Z",
  "components": {
    "database": {
      "status": "healthy",
      "latency_ms": 5.2
    },
    "llm_proxy": {
      "status": "healthy",
      "latency_ms": 150.3
    }
  }
}
```

**Status Codes**:
- `200` - Healthy or degraded (still serving traffic)
- `503` - Unhealthy (critical components failing)

**Implementation**: `src/infra/health.py`

## Deployment

### Prometheus Configuration

**Scrape Config** (prometheus.yml):
```yaml
scrape_configs:
  - job_name: 'enterprise-agent-platform'
    scrape_interval: 30s
    static_configs:
      - targets: ['api:8000']
    metrics_path: '/metrics'
```

### Grafana Dashboard

**Import**: `deploy/grafana/dashboards/overview.json`

**Panels**:
- Request rate, error rate, latency (P50/P95/P99)
- Active connections and agent runs
- LLM token usage and latency
- Token budget remaining per tenant
- Estimated cost per hour
- Agent execution metrics
- Tool call rate and success rate

**Variables**:
- `tenant_id` - Filter by tenant (multi-select)

### OpenTelemetry Collector

**Deployment**: Use official OpenTelemetry Collector image

**Example Config** (otel-collector-config.yaml):
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch:
    timeout: 10s

exporters:
  jaeger:
    endpoint: jaeger:14250
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger]
```

**Kubernetes Deployment**:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: opentelemetry-collector
spec:
  ports:
    - name: otlp-grpc
      port: 4317
      protocol: TCP
  selector:
    app: opentelemetry-collector
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: opentelemetry-collector
spec:
  replicas: 1
  selector:
    matchLabels:
      app: opentelemetry-collector
  template:
    metadata:
      labels:
        app: opentelemetry-collector
    spec:
      containers:
        - name: collector
          image: otel/opentelemetry-collector:latest
          ports:
            - containerPort: 4317
              name: otlp-grpc
          volumeMounts:
            - name: config
              mountPath: /etc/otel
          args:
            - --config=/etc/otel/otel-collector-config.yaml
      volumes:
        - name: config
          configMap:
            name: otel-collector-config
```

### Helm Values Configuration

**Enable Observability**:
```yaml
config:
  enableTelemetry: true
  otlpEndpoint: "http://opentelemetry-collector:4317"

monitoring:
  enabled: true
  serviceMonitor:
    enabled: true
    interval: 30s
```

## Usage Examples

### Recording Custom Metrics

```python
from src.middleware.prometheus import (
    record_agent_run,
    record_llm_request,
    record_tool_call,
    update_token_budget,
)

# Record agent execution
record_agent_run(
    agent_type="rag_agent",
    status="success",
    duration_seconds=12.5,
    steps=8,
)

# Record LLM request
record_llm_request(
    model="openai/gpt-4o-mini",
    status="success",
    duration_seconds=2.3,
    prompt_tokens=150,
    completion_tokens=300,
)

# Record tool call
record_tool_call(
    tool_name="web_search",
    success=True,
    duration_seconds=0.8,
)

# Update token budget
update_token_budget(
    tenant_id="tenant-123",
    period="daily",
    remaining=950000,
    used=50000,
)
```

### Creating Custom Spans

```python
from src.infra.telemetry import create_span

with create_span("custom.operation", attributes={"key": "value"}) as span:
    result = await do_work()
    if span:
        span.set_attribute("result_count", len(result))
```

### Binding Log Context

```python
from src.telemetry.logging import bind_tenant_context, bind_agent_context
import structlog

log = structlog.get_logger(__name__)

bind_tenant_context(tenant_id)
bind_agent_context(agent_id, "rag_agent")

log.info("agent.started")  # Automatically includes tenant_id, agent_id
```

## Monitoring Queries

### Prometheus Queries

**Request Rate**:
```promql
rate(http_requests_total[5m])
```

**Error Rate**:
```promql
rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m])
```

**P95 Latency**:
```promql
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
```

**Token Burn Rate** (tokens/hour):
```promql
rate(llm_tokens_total[1h]) * 3600
```

**Cost Estimate** (USD/hour at $0.02/1M tokens):
```promql
rate(llm_tokens_total[1h]) * 3600 * 0.00002
```

### LogQL Queries (Loki)

**Find all errors**:
```logql
{app="enterprise-agent-platform"} | json | level="error"
```

**Trace all requests for a tenant**:
```logql
{app="enterprise-agent-platform"} | json | tenant_id="tenant-123"
```

**Find slow LLM requests**:
```logql
{app="enterprise-agent-platform"} | json | event="llm.request.completed" | duration_ms > 5000
```

## Alerts

### Recommended Prometheus Alerts

```yaml
groups:
  - name: enterprise-agent-platform
    rules:
      - alert: HighErrorRate
        expr: |
          rate(http_requests_total{status=~"5.."}[5m])
          / rate(http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High error rate (>5%)"

      - alert: HighLatency
        expr: |
          histogram_quantile(0.95,
            rate(http_request_duration_seconds_bucket[5m])
          ) > 2
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P95 latency >2s"

      - alert: TokenBudgetLow
        expr: |
          token_budget_remaining{period="daily"} < 100000
        labels:
          severity: warning
        annotations:
          summary: "Daily token budget <100k"

      - alert: LLMProxyDown
        expr: |
          up{job="enterprise-agent-platform"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "LLM proxy unreachable"
```

## Troubleshooting

### Metrics Not Appearing

1. Check `/metrics` endpoint returns data
2. Verify Prometheus scrape config targets correct endpoint
3. Check Prometheus logs for scrape errors
4. Ensure middleware is registered in main.py

### Traces Not Appearing

1. Verify `ENABLE_TELEMETRY=true`
2. Check OTLP collector is reachable
3. Look for "telemetry.initialized" log entry
4. Verify FastAPI instrumentation with `instrument_fastapi(app)`

### Missing Trace Context in Logs

1. Ensure `configure_logging()` called before first log
2. Verify OpenTelemetry is enabled
3. Check span is active when logging

### Health Checks Failing

1. Check individual component status in `/health` response
2. Verify database connectivity
3. Test LiteLLM proxy `/health` endpoint directly
4. Check Redis connection if configured

## Performance Impact

**Prometheus Metrics**:
- Overhead: <1ms per request
- Memory: ~10MB for metric registry

**OpenTelemetry Tracing**:
- Overhead: ~2-5ms per request (batch export)
- Memory: ~50MB for span buffer
- Network: ~1KB per span (gzipped)

**Structured Logging**:
- Overhead: <1ms per log entry
- JSON serialization is CPU-bound but fast

**Total Impact**: <10ms latency overhead, <100MB memory overhead

## Best Practices

1. **Use tenant_id filters** in dashboards to scope metrics
2. **Set appropriate histogram buckets** for your latency SLOs
3. **Sample traces** in production (e.g., 10% sampling rate)
4. **Aggregate logs** to a central system (Loki, Elasticsearch)
5. **Set up alerting** on key metrics (error rate, latency, budget)
6. **Monitor costs** via token usage metrics
7. **Use trace context** to correlate logs with distributed traces

## Dependencies

**Required**:
- `prometheus-client>=0.21.0`
- `opentelemetry-api>=1.28.0`
- `opentelemetry-sdk>=1.28.0`
- `opentelemetry-exporter-otlp-proto-grpc>=1.28.0`
- `opentelemetry-instrumentation-fastapi>=0.49b0`
- `opentelemetry-instrumentation-httpx>=0.49b0`
- `opentelemetry-instrumentation-sqlalchemy>=0.49b0`

**Optional** (for full observability stack):
- Prometheus server
- Grafana
- OpenTelemetry Collector
- Jaeger or Tempo (trace backend)
- Loki (log aggregation)
