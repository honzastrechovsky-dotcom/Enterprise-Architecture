# Observability Stack Deployment Guide

Quick start guide for deploying the observability stack (Prometheus, Grafana, OpenTelemetry) alongside the Enterprise Agent Platform.

## Quick Start (Docker Compose)

### 1. Create docker-compose.observability.yml

```yaml
version: '3.8'

services:
  # OpenTelemetry Collector
  otel-collector:
    image: otel/opentelemetry-collector:latest
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./deploy/otel/config.yaml:/etc/otel/config.yaml
    ports:
      - "4317:4317"  # OTLP gRPC receiver
      - "4318:4318"  # OTLP HTTP receiver
      - "8888:8888"  # Prometheus metrics
    networks:
      - observability

  # Jaeger (Trace Backend)
  jaeger:
    image: jaegertracing/all-in-one:latest
    environment:
      - COLLECTOR_OTLP_ENABLED=true
    ports:
      - "16686:16686"  # Jaeger UI
      - "14250:14250"  # gRPC receiver
    networks:
      - observability

  # Prometheus
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./deploy/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus-data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.console.libraries=/usr/share/prometheus/console_libraries'
      - '--web.console.templates=/usr/share/prometheus/consoles'
    ports:
      - "9090:9090"
    networks:
      - observability

  # Grafana
  grafana:
    image: grafana/grafana:latest
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - grafana-data:/var/lib/grafana
      - ./deploy/grafana/datasources:/etc/grafana/provisioning/datasources
      - ./deploy/grafana/dashboards:/etc/grafana/provisioning/dashboards
    ports:
      - "3000:3000"
    networks:
      - observability
    depends_on:
      - prometheus

volumes:
  prometheus-data:
  grafana-data:

networks:
  observability:
    driver: bridge
```

### 2. Create Configuration Files

**deploy/otel/config.yaml**:
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 10s
    send_batch_size: 1024

exporters:
  jaeger:
    endpoint: jaeger:14250
    tls:
      insecure: true

  prometheus:
    endpoint: "0.0.0.0:8888"

  logging:
    loglevel: info

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [jaeger, logging]

    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus, logging]
```

**deploy/prometheus/prometheus.yml**:
```yaml
global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  - job_name: 'enterprise-agent-platform'
    static_configs:
      - targets: ['host.docker.internal:8000']
    metrics_path: '/metrics'

  - job_name: 'otel-collector'
    static_configs:
      - targets: ['otel-collector:8888']
```

**deploy/grafana/datasources/datasources.yml**:
```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true

  - name: Jaeger
    type: jaeger
    access: proxy
    url: http://jaeger:16686
```

**deploy/grafana/dashboards/dashboards.yml**:
```yaml
apiVersion: 1

providers:
  - name: 'Enterprise Agent Platform'
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: true
    options:
      path: /etc/grafana/provisioning/dashboards
```

### 3. Update Application .env

```bash
# Enable telemetry
ENABLE_TELEMETRY=true
OTLP_ENDPOINT=http://localhost:4317

# Production logging (JSON format)
ENVIRONMENT=prod
```

### 4. Start Services

```bash
# Start observability stack
docker-compose -f docker-compose.observability.yml up -d

# Start application (with telemetry enabled)
docker-compose up -d
```

### 5. Access UIs

- **Grafana**: http://localhost:3000 (admin/admin)
- **Prometheus**: http://localhost:9090
- **Jaeger**: http://localhost:16686
- **Metrics**: http://localhost:8000/metrics

## Kubernetes Deployment

### 1. Install Prometheus Operator

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
```

### 2. Install OpenTelemetry Operator

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml

kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/latest/download/opentelemetry-operator.yaml
```

### 3. Deploy OpenTelemetry Collector

**otel-collector.yaml**:
```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: OpenTelemetryCollector
metadata:
  name: otel-collector
  namespace: monitoring
spec:
  mode: deployment
  config: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318

    processors:
      batch:
        timeout: 10s

    exporters:
      jaeger:
        endpoint: jaeger-collector.monitoring.svc:14250
        tls:
          insecure: true

      prometheus:
        endpoint: "0.0.0.0:8888"

    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [batch]
          exporters: [jaeger]
        metrics:
          receivers: [otlp]
          processors: [batch]
          exporters: [prometheus]
```

```bash
kubectl apply -f otel-collector.yaml
```

### 4. Deploy Application with Observability

```bash
helm install enterprise-agent-platform ./deploy/helm/enterprise-agent-platform \
  --namespace agents \
  --create-namespace \
  --set config.enableTelemetry=true \
  --set config.otlpEndpoint=http://otel-collector.monitoring.svc:4317 \
  --set monitoring.enabled=true \
  --set monitoring.serviceMonitor.enabled=true
```

### 5. Import Grafana Dashboard

```bash
kubectl create configmap grafana-dashboard-agent-platform \
  --from-file=deploy/grafana/dashboards/overview.json \
  --namespace monitoring

kubectl label configmap grafana-dashboard-agent-platform \
  grafana_dashboard=1 \
  --namespace monitoring
```

## Verification

### 1. Check Metrics Endpoint

```bash
curl http://localhost:8000/metrics
```

Expected output:
```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{endpoint="/health/live",method="GET",status="200"} 142.0
...
```

### 2. Check Health Endpoints

```bash
# Liveness
curl http://localhost:8000/health/live
# {"status": "alive"}

# Readiness
curl http://localhost:8000/health/ready
# {"status": "ready"}

# Detailed
curl http://localhost:8000/health | jq
```

### 3. Verify Traces in Jaeger

1. Open http://localhost:16686
2. Select service: `enterprise-agent-platform`
3. Click "Find Traces"
4. Should see HTTP requests with spans for DB queries, LLM calls, etc.

### 4. Check Prometheus Targets

1. Open http://localhost:9090/targets
2. Verify `enterprise-agent-platform` target is UP
3. Check scrape duration is reasonable (<100ms)

### 5. View Grafana Dashboard

1. Open http://localhost:3000
2. Navigate to Dashboards → Enterprise Agent Platform - Overview
3. Select tenant from dropdown
4. Verify panels show data

## Troubleshooting

### No metrics in Prometheus

```bash
# Check if metrics endpoint works
curl http://localhost:8000/metrics

# Check Prometheus scrape config
docker logs prometheus | grep "enterprise-agent-platform"

# Verify target is UP in Prometheus
# http://localhost:9090/targets
```

### No traces in Jaeger

```bash
# Verify telemetry is enabled
curl http://localhost:8000/health | jq '.components'

# Check OTLP collector logs
docker logs otel-collector

# Verify OTLP endpoint is reachable
curl http://localhost:4317
```

### Logs missing trace context

```bash
# Check logs for trace_id field
docker logs enterprise-agent-platform-api-1 | jq '.trace_id'

# Verify OpenTelemetry is initialized
docker logs enterprise-agent-platform-api-1 | grep "telemetry.initialized"
```

### High memory usage

```bash
# Check Prometheus metrics retention
# Reduce retention period or increase memory limits

# Check OpenTelemetry batch size
# Reduce batch size in otel-collector config

# Monitor metrics buffer size
curl http://localhost:8000/metrics | grep memory
```

## Production Considerations

### 1. Sampling

For high-traffic production environments, enable trace sampling:

**otel-collector.yaml**:
```yaml
processors:
  probabilistic_sampler:
    sampling_percentage: 10  # Sample 10% of traces
```

### 2. Long-term Storage

Configure Prometheus remote write to long-term storage:

```yaml
remote_write:
  - url: https://prometheus-remote-storage.example.com/api/v1/write
    basic_auth:
      username: <username>
      password: <password>
```

### 3. Log Aggregation

Ship structured logs to Loki or Elasticsearch:

```bash
# Loki Promtail sidecar
kubectl apply -f loki-promtail-daemonset.yaml
```

### 4. Alerts

Create Prometheus alert rules:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: enterprise-agent-platform-alerts
  namespace: monitoring
spec:
  groups:
    - name: agent-platform
      interval: 30s
      rules:
        - alert: HighErrorRate
          expr: |
            rate(http_requests_total{status=~"5.."}[5m])
            / rate(http_requests_total[5m]) > 0.05
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "Error rate >5% for 5 minutes"
```

### 5. Multi-cluster Tracing

Use Grafana Tempo for distributed tracing across clusters:

```bash
helm install tempo grafana/tempo \
  --namespace monitoring
```

## Cost Optimization

### Reduce Metrics Cardinality

```python
# Bad: High cardinality (unique tenant_id per metric)
metric.labels(tenant_id=tenant_id).inc()

# Good: Use queries to filter by tenant
metric.inc()  # Query with {tenant_id="..."} in Prometheus
```

### Reduce Trace Volume

```python
# Sample traces in high-traffic endpoints
if random.random() < 0.1:  # 10% sampling
    with create_span("expensive.operation"):
        ...
```

### Efficient Logging

```python
# Use appropriate log levels
log.debug("verbose.details")  # Only in dev
log.info("important.events")  # Key business events
log.error("failures")  # Always log
```

## Next Steps

1. Set up alerts for critical metrics
2. Configure log retention policies
3. Implement sampling for high-traffic endpoints
4. Create custom dashboards for specific workflows
5. Integrate with incident management (PagerDuty, Opsgenie)
