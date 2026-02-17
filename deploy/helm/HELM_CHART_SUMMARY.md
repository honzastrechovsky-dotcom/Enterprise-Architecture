# Enterprise Agent Platform - Helm Chart Summary

## Overview

A production-ready Kubernetes Helm chart for the Enterprise Agent Platform, designed for secure on-premise deployment with comprehensive security controls, scalability, and observability.

**Created**: 2026-02-17
**Chart Version**: 0.1.0
**App Version**: 1.0.0

---

## What Was Created

### Directory Structure

```
deploy/helm/
├── DEPLOYMENT_GUIDE.md           # Complete deployment guide
├── QUICKSTART.md                 # 5-minute quick start
├── validate-chart.sh             # Validation script
└── enterprise-agent-platform/    # Helm chart
    ├── Chart.yaml                # Chart metadata
    ├── values.yaml               # Configuration values (~150 lines)
    ├── .helmignore               # Helm ignore patterns
    ├── README.md                 # Chart documentation
    └── templates/                # Kubernetes manifests
        ├── _helpers.tpl          # Template helpers
        ├── NOTES.txt             # Post-install instructions
        ├── configmap.yaml        # Non-secret configuration
        ├── secret.yaml           # Sensitive configuration
        ├── deployment-api.yaml   # API server deployment
        ├── deployment-worker.yaml # Background worker deployment
        ├── deployment-frontend.yaml # Frontend deployment
        ├── deployment-litellm.yaml # LiteLLM proxy deployment
        ├── deployment-vllm.yaml  # vLLM inference deployment
        ├── service-api.yaml      # API ClusterIP service
        ├── service-frontend.yaml # Frontend ClusterIP service
        ├── ingress.yaml          # External access (optional)
        ├── hpa.yaml              # Horizontal Pod Autoscaler
        ├── networkpolicy.yaml    # Network segmentation
        ├── serviceaccount.yaml   # Service account
        └── pdb.yaml              # Pod Disruption Budget
```

**Total Files**: 20 Kubernetes templates + 4 documentation files

---

## Key Features

### Security-First Design

- **Non-root containers**: All containers run as UID 1000
- **Read-only filesystems**: Root filesystem is read-only
- **Dropped capabilities**: All capabilities dropped (CAP_DROP: ALL)
- **seccompProfile**: RuntimeDefault enforced
- **NetworkPolicy**: Zero-trust network segmentation
- **External secrets support**: Integration with external-secrets operator

### Production-Ready Components

| Component | Purpose | Default Replicas | Scaling |
|-----------|---------|------------------|---------|
| API Server | FastAPI backend | 2 | HPA 2-10 |
| Worker | Background tasks | 2 | Manual |
| Frontend | React UI | 1 | Manual |
| PostgreSQL | Database + pgvector | 1 | Stateful |
| Redis | Cache + rate limiting | 1 | Stateful |
| LiteLLM | LLM proxy | 1 | Manual |
| vLLM | Local LLM inference | 0 (optional) | Manual |

### Resource Management

**API Server:**
- Requests: 256Mi memory, 250m CPU
- Limits: 512Mi memory, 500m CPU

**Worker:**
- Requests: 256Mi memory, 250m CPU
- Limits: 512Mi memory, 500m CPU
- Concurrency: 4 tasks per worker

**PostgreSQL:**
- Requests: 512Mi memory, 500m CPU
- Limits: 1Gi memory, 1000m CPU
- Storage: 20Gi persistent volume

**Redis:**
- Requests: 256Mi memory, 250m CPU
- Limits: 512Mi memory, 500m CPU
- Storage: 5Gi persistent volume

### High Availability

- **HPA**: Auto-scales API from 2-10 replicas based on CPU (70%) and memory (80%)
- **PDB**: Ensures minimum 1 replica always available during disruptions
- **Anti-affinity**: Distributes pods across nodes
- **Health probes**: Liveness and readiness checks on all components

### Network Security

**NetworkPolicy Rules:**

1. **API Pods**:
   - Ingress: Frontend, ingress-controller → API
   - Egress: API → PostgreSQL, Redis, LiteLLM, OIDC (external)

2. **Worker Pods**:
   - Ingress: None (no incoming traffic)
   - Egress: Worker → PostgreSQL, Redis, LiteLLM

3. **Frontend Pods**:
   - Ingress: ingress-controller → Frontend
   - Egress: Frontend → API

---

## Configuration

### Environment-Specific Values

**Development:**
```yaml
config:
  environment: dev
  debug: true

ingress:
  enabled: false

networkPolicy:
  enabled: false

autoscaling:
  enabled: false

vllm:
  enabled: false

ollama:
  enabled: true  # Use Ollama for dev
```

**Production:**
```yaml
config:
  environment: prod
  debug: false
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

vllm:
  enabled: true
  gpuCount: 2
```

### All Configuration Options

See `values.yaml` for complete list. Key sections:

- **image**: Repository, tag, pull policy, pull secrets
- **api**: Replicas, resources, health probes, security context
- **worker**: Replicas, concurrency, resources
- **frontend**: Enabled flag, replicas, resources
- **postgresql**: Bitnami chart configuration
- **redis**: Bitnami chart configuration
- **litellm**: Proxy configuration
- **vllm**: GPU inference configuration
- **ingress**: External access configuration
- **config**: Application settings (all env vars from config.py)
- **secrets**: Sensitive values (support for external secrets)
- **autoscaling**: HPA configuration
- **networkPolicy**: Network segmentation
- **serviceAccount**: RBAC configuration

---

## Deployment Workflows

### Quick Development Install

```bash
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents-dev \
  --create-namespace \
  --set config.environment=dev \
  --set ingress.enabled=false
```

### Production Install

```bash
# 1. Create secrets
kubectl create secret generic enterprise-agent-platform-secrets \
  --namespace enterprise-agents \
  --from-literal=secret-key="$(openssl rand -base64 32)" \
  --from-literal=dev-jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=litellm-api-key="sk-prod-key" \
  --from-literal=database-url="postgresql+asyncpg://..."

# 2. Install chart
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values production-values.yaml \
  --timeout 15m \
  --wait
```

### Upgrade

```bash
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml \
  --timeout 15m \
  --wait \
  --atomic  # Auto-rollback on failure
```

### Rollback

```bash
helm rollback enterprise-agents -n enterprise-agents
```

### Uninstall

```bash
helm uninstall enterprise-agents -n enterprise-agents
kubectl delete namespace enterprise-agents  # Include PVCs
```

---

## Validation

Run the validation script before deployment:

```bash
cd /mnt/c/AI/enterprise-agent-platform/deploy/helm
./validate-chart.sh
```

**Checks performed:**
- Helm installation
- Chart structure validation
- Chart.yaml and values.yaml presence
- Template completeness
- Helm lint
- Dry-run installation
- Security hardening (non-root, read-only FS, capabilities)
- Production values testing

---

## Integration Points

### External Dependencies (via Bitnami charts)

- **postgresql**: Version ~13.0.0 (with pgvector extension)
- **redis**: Version ~18.0.0

These are declared in `Chart.yaml` and auto-installed as dependencies.

### External Services Required

- **OIDC Provider**: For authentication (issuer URL, client ID, audience)
- **Image Registry**: For container images (optional authentication)
- **Storage Class**: For persistent volumes (uses cluster default if not specified)
- **Ingress Controller**: For external access (nginx-ingress recommended)
- **GPU Device Plugin**: For vLLM (nvidia-device-plugin if using GPUs)

### External Integrations (Optional)

- **cert-manager**: For TLS certificate management
- **external-secrets**: For secret management
- **Prometheus Operator**: For metrics collection (ServiceMonitor support)
- **OpenTelemetry**: For distributed tracing

---

## Environment Variables Mapping

All environment variables from `src/config.py` are mapped to either ConfigMap (non-secret) or Secret (sensitive):

**ConfigMap** (`configmap.yaml`):
- ENVIRONMENT, DEBUG, DB_ECHO_SQL
- REDIS_URL, LITELLM_BASE_URL
- LITELLM_DEFAULT_MODEL, LITELLM_EMBEDDING_MODEL
- MODEL_ROUTING_ENABLED, MODEL_LIGHT, MODEL_STANDARD, MODEL_HEAVY
- TOKEN_BUDGET_DAILY, TOKEN_BUDGET_MONTHLY
- OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_AUDIENCE
- RATE_LIMIT_PER_MINUTE
- CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS, VECTOR_TOP_K, EMBEDDING_DIMENSIONS
- BACKGROUND_WORKER_CONCURRENCY, ENABLE_TELEMETRY, OTLP_ENDPOINT

**Secret** (`secret.yaml`):
- SECRET_KEY, DEV_JWT_SECRET, LITELLM_API_KEY, DATABASE_URL

---

## Security Considerations

### Default Secrets (Development Only)

The chart includes default secrets for development:
- `SECRET_KEY`: "dev-secret-key-not-for-production"
- `DEV_JWT_SECRET`: "dev-only-jwt-secret-not-for-production"
- `LITELLM_API_KEY`: "sk-dev-key"

**WARNING**: These MUST be changed for production. The application will refuse to start in production mode with default secrets (validated by `src/config.py`).

### Production Secret Management

**Recommended approach:**
1. Use external-secrets operator or sealed-secrets
2. Set `secrets.useExternalSecret=true` in values
3. Create secret separately in cluster
4. Reference secret name in `secrets.externalSecretName`

### Network Isolation

NetworkPolicy is enabled by default in production. Adjust the ingress namespace selector if your ingress controller is in a different namespace:

```yaml
networkPolicy:
  enabled: true
  # Adjust to match your environment
  ingressNamespaceLabel: ingress-nginx
```

### Pod Security Standards

All pods comply with "restricted" Pod Security Standards:
- runAsNonRoot: true
- readOnlyRootFilesystem: true (with /tmp emptyDir for writes)
- allowPrivilegeEscalation: false
- capabilities.drop: [ALL]
- seccompProfile.type: RuntimeDefault

---

## Troubleshooting

### Common Issues

**1. Pods stuck in Pending**
- Check: `kubectl describe pod <pod-name>`
- Causes: Insufficient resources, missing StorageClass, GPU not available

**2. Image pull errors**
- Check: Image repository and tag in values.yaml
- Check: imagePullSecrets configured if private registry

**3. Database connection failures**
- Check: PostgreSQL pod running (`kubectl get pods -l app.kubernetes.io/name=postgresql`)
- Check: DATABASE_URL in secret
- Check: NetworkPolicy allows API→PostgreSQL

**4. Ingress not working**
- Check: Ingress controller installed
- Check: DNS pointing to ingress IP
- Check: TLS secret created (if using HTTPS)

**5. vLLM not scheduling**
- Check: GPU nodes exist (`kubectl get nodes -l nvidia.com/gpu`)
- Check: nvidia-device-plugin running
- Check: GPU toleration and affinity in deployment-vllm.yaml

### Debug Commands

```bash
# View all resources
kubectl get all -n enterprise-agents

# Describe deployment
kubectl describe deployment enterprise-agents-api -n enterprise-agents

# Check events
kubectl get events -n enterprise-agents --sort-by='.lastTimestamp'

# View logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=api --tail=100

# Test connectivity
kubectl exec -n enterprise-agents <api-pod> -- sh -c 'nc -zv postgresql 5432'

# View templated manifests
helm template enterprise-agents ./enterprise-agent-platform > manifests.yaml
```

---

## Maintenance

### Backup

**Database:**
```bash
kubectl exec -n enterprise-agents <postgresql-pod> -- \
  pg_dump -U app enterprise_agents > backup-$(date +%Y%m%d).sql
```

**Helm release:**
```bash
helm get values enterprise-agents -n enterprise-agents > backup-values.yaml
helm get manifest enterprise-agents -n enterprise-agents > backup-manifests.yaml
```

### Upgrades

Test upgrades in staging first. Use `--dry-run` to preview changes.

```bash
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml \
  --dry-run --debug
```

### Monitoring

**Resource usage:**
```bash
kubectl top pods -n enterprise-agents
kubectl top nodes
```

**HPA status:**
```bash
kubectl get hpa -n enterprise-agents -w
```

**Logs aggregation:**
Consider using a logging stack (EFK, Loki) for centralized logs.

---

## Documentation

| Document | Purpose |
|----------|---------|
| **QUICKSTART.md** | 5-minute installation guide |
| **DEPLOYMENT_GUIDE.md** | Complete deployment procedures |
| **enterprise-agent-platform/README.md** | Chart documentation |
| **enterprise-agent-platform/values.yaml** | Configuration reference |
| **HELM_CHART_SUMMARY.md** | This document |

---

## Testing

### Validation Checklist

- [ ] Run `./validate-chart.sh` and verify all checks pass
- [ ] Test `helm lint` succeeds
- [ ] Test `helm install --dry-run` succeeds
- [ ] Test with production values
- [ ] Verify security contexts in generated manifests
- [ ] Verify NetworkPolicy resources generated
- [ ] Verify secrets not committed to git

### Deployment Checklist

- [ ] Secrets changed from defaults
- [ ] OIDC configuration correct
- [ ] Ingress host and TLS configured
- [ ] Resource limits appropriate for workload
- [ ] Storage classes available
- [ ] GPU nodes available (if using vLLM)
- [ ] Monitoring and alerting configured

---

## Support and Contribution

**Project**: Enterprise Agent Platform
**Repository**: https://github.com/enterprise-agent-platform/enterprise-agent-platform
**Issues**: GitHub Issues
**Contact**: platform@example.com

---

## License

MIT License - See LICENSE file for details

---

## Changelog

### v0.1.0 (2026-02-17)
- Initial Helm chart release
- Support for API, Worker, Frontend, PostgreSQL, Redis, LiteLLM, vLLM
- Security-hardened deployments (non-root, read-only FS, NetworkPolicy)
- HPA and PDB for high availability
- Comprehensive documentation and validation tooling
- Support for both development and production deployments
