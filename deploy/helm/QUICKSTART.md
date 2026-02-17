# Quick Start Guide - Enterprise Agent Platform Helm Chart

Get the Enterprise Agent Platform running on Kubernetes in minutes.

## Prerequisites

- Kubernetes cluster (v1.24+)
- kubectl configured
- Helm 3.8+

## 5-Minute Installation

### 1. Add Helm Repositories

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

### 2. Install the Chart

```bash
cd /mnt/c/AI/enterprise-agent-platform/deploy/helm

helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --timeout 10m
```

### 3. Wait for Pods to Start

```bash
# Watch pods starting
kubectl get pods -n enterprise-agents -w

# Wait for all pods to be Running (Ctrl+C to stop watching)
```

### 4. Access the API

```bash
# Port-forward the API service
kubectl port-forward -n enterprise-agents svc/enterprise-agents-api 8000:8000 &

# Test the API
curl http://localhost:8000/health

# Expected: {"status":"healthy"}
```

### 5. View Logs

```bash
# API logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=api -f

# Worker logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=worker -f
```

## What Gets Deployed?

| Component | Description | Replicas |
|-----------|-------------|----------|
| **API Server** | FastAPI backend | 2 |
| **Worker** | Background task processor | 2 |
| **Frontend** | React UI (optional) | 1 |
| **PostgreSQL** | Database with pgvector | 1 |
| **Redis** | Cache and rate limiting | 1 |
| **LiteLLM** | LLM proxy gateway | 1 |
| **vLLM** | Local LLM inference (optional) | 0 (disabled by default) |

## Next Steps

### Enable Ingress

Edit `values.yaml`:

```yaml
ingress:
  enabled: true
  host: agents.your-domain.com
  tls:
    enabled: true
```

Then upgrade:

```bash
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values ./enterprise-agent-platform/values.yaml
```

### Configure OIDC Authentication

Edit `values.yaml`:

```yaml
config:
  oidcIssuerUrl: "https://your-auth-server.com/realms/production"
  oidcClientId: "your-client-id"
  oidcAudience: "your-audience"
```

### Change Secrets (REQUIRED for Production!)

**Option 1: Override via values.yaml**

```yaml
secrets:
  secretKey: "your-secure-random-key-here"
  devJwtSecret: "your-jwt-secret-here"
  litellmApiKey: "sk-your-litellm-key"
```

**Option 2: Use Kubernetes Secrets (Recommended)**

```bash
# Create secret manually
kubectl create secret generic enterprise-agent-platform-secrets \
  --namespace enterprise-agents \
  --from-literal=secret-key="$(openssl rand -base64 32)" \
  --from-literal=dev-jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=litellm-api-key="sk-your-key" \
  --from-literal=database-url="postgresql+asyncpg://app:password@postgresql:5432/db"

# Configure chart to use external secret
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --set secrets.useExternalSecret=true \
  --reuse-values
```

### Enable GPU Support for vLLM

```yaml
vllm:
  enabled: true
  modelName: "Qwen/Qwen2.5-72B-Instruct"
  gpuCount: 2
```

Requires:
- NVIDIA GPU nodes
- nvidia-device-plugin installed

### Scale Components

```bash
# Scale API
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --set api.replicaCount=5 \
  --reuse-values

# Scale workers
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --set worker.replicaCount=4 \
  --reuse-values
```

## Troubleshooting

### Pods Not Starting

```bash
# Check pod status
kubectl get pods -n enterprise-agents

# Describe problematic pod
kubectl describe pod -n enterprise-agents <pod-name>

# Check events
kubectl get events -n enterprise-agents --sort-by='.lastTimestamp'
```

### Database Connection Issues

```bash
# Check PostgreSQL
kubectl logs -n enterprise-agents -l app.kubernetes.io/name=postgresql

# Test connection from API pod
kubectl exec -n enterprise-agents <api-pod-name> -- \
  sh -c 'echo | nc enterprise-agents-postgresql 5432 && echo "OK"'
```

### View Generated Manifests

```bash
# Dry-run to see what will be deployed
helm install test ./enterprise-agent-platform \
  --namespace test \
  --dry-run --debug > manifests.yaml

# Review manifests.yaml
```

## Validation

Run the validation script before deploying:

```bash
cd /mnt/c/AI/enterprise-agent-platform/deploy/helm
./validate-chart.sh
```

## Uninstall

```bash
# Remove release
helm uninstall enterprise-agents -n enterprise-agents

# Remove namespace (includes PVCs)
kubectl delete namespace enterprise-agents
```

## Complete Examples

### Development Setup

```bash
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents-dev \
  --create-namespace \
  --set config.environment=dev \
  --set config.debug=true \
  --set ingress.enabled=false \
  --set networkPolicy.enabled=false \
  --set autoscaling.enabled=false
```

### Production Setup

```bash
# Create production values file
cat > prod-values.yaml <<EOF
image:
  repository: your-registry.com/enterprise-agent-platform
  tag: "1.0.0"
  pullSecrets:
    - name: registry-creds

api:
  replicaCount: 3

worker:
  replicaCount: 3
  concurrency: 8

config:
  environment: prod
  oidcIssuerUrl: https://auth.company.com/realms/prod

secrets:
  useExternalSecret: true

ingress:
  enabled: true
  host: agents.company.com
  tls:
    enabled: true

networkPolicy:
  enabled: true

autoscaling:
  enabled: true
EOF

# Install
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values prod-values.yaml \
  --timeout 15m
```

## Resources

- **Full Documentation**: [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md)
- **Chart README**: [enterprise-agent-platform/README.md](./enterprise-agent-platform/README.md)
- **Configuration Reference**: [enterprise-agent-platform/values.yaml](./enterprise-agent-platform/values.yaml)

## Support

Questions? Issues?
- GitHub: https://github.com/enterprise-agent-platform/enterprise-agent-platform
- Email: platform@example.com
