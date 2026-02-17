# Enterprise Agent Platform Helm Chart

Production-ready Kubernetes deployment for the Enterprise Agent Platform - a multi-tenant AI agent platform with RAG, audit logging, and OIDC authentication designed for on-premise deployment.

## Features

- **Multi-component architecture**: API server, background workers, and frontend
- **Security-first design**: Non-root containers, read-only filesystems, NetworkPolicy enforcement
- **Scalability**: Horizontal Pod Autoscaling, Pod Disruption Budgets
- **Integrated dependencies**: PostgreSQL (with pgvector), Redis, LiteLLM, vLLM
- **Production-ready**: Health checks, resource limits, security contexts

## Prerequisites

- Kubernetes 1.24+
- Helm 3.8+
- NVIDIA GPU support for vLLM (optional, can use Ollama or cloud LLMs instead)
- StorageClass for persistent volumes (default or specify in values)

## Installation

### Quick Start (Development)

```bash
# Add Bitnami repository for dependencies
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# Install with default values (development mode)
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace
```

### Production Deployment

```bash
# Create a production values file
cat > production-values.yaml <<EOF
config:
  environment: prod
  oidcIssuerUrl: https://auth.company.com/realms/production
  oidcClientId: enterprise-agents
  oidcAudience: enterprise-agents-api

secrets:
  # Use external-secrets operator in production
  useExternalSecret: true
  externalSecretName: enterprise-agent-platform-secrets

ingress:
  enabled: true
  className: nginx
  host: agents.company.com
  tls:
    enabled: true
    secretName: enterprise-agent-platform-tls

autoscaling:
  enabled: true
  api:
    minReplicas: 3
    maxReplicas: 20

networkPolicy:
  enabled: true

vllm:
  enabled: true
  modelName: "Qwen/Qwen2.5-72B-Instruct"
  gpuCount: 2
EOF

# Install with production values
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values production-values.yaml
```

## Configuration

### Key Configuration Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `api.replicaCount` | Number of API server replicas | `2` |
| `worker.replicaCount` | Number of background worker replicas | `2` |
| `worker.concurrency` | Concurrent tasks per worker | `4` |
| `config.environment` | Application environment (dev/prod/test) | `prod` |
| `postgresql.enabled` | Enable PostgreSQL dependency | `true` |
| `redis.enabled` | Enable Redis dependency | `true` |
| `litellm.enabled` | Enable LiteLLM proxy | `true` |
| `vllm.enabled` | Enable vLLM for local inference | `true` |
| `ollama.enabled` | Enable Ollama (dev/test only) | `false` |
| `ingress.enabled` | Enable ingress | `false` |
| `networkPolicy.enabled` | Enable NetworkPolicy | `true` |
| `autoscaling.enabled` | Enable HPA | `true` |

### Security Configuration

All deployments use security-hardened containers:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
```

### Secrets Management

**Development:**
Secrets are stored inline in values.yaml (DO NOT use in production).

**Production (Recommended):**
Use external-secrets operator or sealed-secrets:

```yaml
secrets:
  useExternalSecret: true
  externalSecretName: enterprise-agent-platform-secrets
```

Create the external secret separately:

```bash
kubectl create secret generic enterprise-agent-platform-secrets \
  --namespace enterprise-agents \
  --from-literal=secret-key="$(openssl rand -base64 32)" \
  --from-literal=dev-jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=litellm-api-key="sk-your-key-here" \
  --from-literal=database-url="postgresql+asyncpg://user:pass@host:5432/db"
```

### Network Policy

NetworkPolicy restricts traffic between components:

- **API**: Can reach PostgreSQL, Redis, LiteLLM, and accept from frontend/ingress
- **Worker**: Can reach PostgreSQL, Redis, LiteLLM (no ingress)
- **Frontend**: Can reach API and accept from ingress

Adjust the ingress namespace selector if using a different ingress controller namespace:

```yaml
networkPolicy:
  enabled: true
  # Adjust this to match your ingress controller namespace
  ingressNamespaceLabel: ingress-nginx
```

## Accessing the Application

### Via Ingress (Production)

```bash
# After enabling ingress
curl https://agents.company.com/health
```

### Via Port Forward (Development)

```bash
# Forward API
kubectl port-forward -n enterprise-agents svc/enterprise-agents-api 8000:8000

# Forward frontend
kubectl port-forward -n enterprise-agents svc/enterprise-agents-frontend 8080:80

# Access
curl http://localhost:8000/health
open http://localhost:8080
```

## Monitoring

### Pod Status

```bash
kubectl get pods -n enterprise-agents
```

### Logs

```bash
# API logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=api -f

# Worker logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=worker -f

# All logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/instance=enterprise-agents -f --max-log-requests=10
```

### Metrics

If you enable ServiceMonitor (requires Prometheus Operator):

```yaml
monitoring:
  enabled: true
  serviceMonitor:
    enabled: true
```

## Scaling

### Manual Scaling

```bash
# Scale API
kubectl scale deployment enterprise-agents-api \
  --namespace enterprise-agents \
  --replicas=5

# Scale workers
kubectl scale deployment enterprise-agents-worker \
  --namespace enterprise-agents \
  --replicas=4
```

### Horizontal Pod Autoscaling

HPA is enabled by default. Adjust thresholds:

```yaml
autoscaling:
  enabled: true
  api:
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
```

## Upgrades

```bash
# Upgrade with new values
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml

# Rollback if needed
helm rollback enterprise-agents -n enterprise-agents
```

## Uninstallation

```bash
# Delete the release
helm uninstall enterprise-agents -n enterprise-agents

# Delete persistent volumes (optional)
kubectl delete pvc -n enterprise-agents -l app.kubernetes.io/instance=enterprise-agents
```

## Troubleshooting

### Pods not starting

```bash
# Check events
kubectl get events -n enterprise-agents --sort-by='.lastTimestamp'

# Describe pod
kubectl describe pod -n enterprise-agents <pod-name>

# Check logs
kubectl logs -n enterprise-agents <pod-name>
```

### Database connection issues

```bash
# Check PostgreSQL pod
kubectl get pods -n enterprise-agents -l app.kubernetes.io/name=postgresql

# Check PostgreSQL logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/name=postgresql

# Test connection from API pod
kubectl exec -n enterprise-agents <api-pod> -- \
  env | grep DATABASE_URL
```

### NetworkPolicy blocking traffic

```bash
# Temporarily disable NetworkPolicy for debugging
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --set networkPolicy.enabled=false \
  --reuse-values
```

## Architecture

```
┌─────────────────┐
│  Ingress        │
│  (nginx)        │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼──┐  ┌──▼────┐
│Frontend│  │ API   │
│ (nginx)│  │(Fast) │
└────────┘  └───┬───┘
                │
        ┌───────┼───────┐
        │       │       │
    ┌───▼──┐ ┌─▼──┐ ┌──▼────┐
    │Worker│ │Redis│ │PostgreSQL│
    │      │ │     │ │ +pgvector│
    └───┬──┘ └─────┘ └─────────┘
        │
    ┌───▼────────┐
    │  LiteLLM   │
    │  /vLLM     │
    └────────────┘
```

## Support

- GitHub Issues: https://github.com/enterprise-agent-platform/enterprise-agent-platform/issues
- Documentation: https://github.com/enterprise-agent-platform/enterprise-agent-platform
- Email: platform@example.com

## License

MIT License - See LICENSE file for details
