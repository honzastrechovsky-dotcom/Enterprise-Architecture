# Enterprise Agent Platform - Kubernetes Deployment Guide

Complete guide for deploying the Enterprise Agent Platform on on-premise Kubernetes clusters.

## Quick Reference

### Installation Commands

```bash
# 1. Add Bitnami repository
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

# 2. Validate the chart
helm lint ./enterprise-agent-platform

# 3. Dry-run to see generated manifests
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --dry-run --debug

# 4. Install
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace
```

### Verification Commands

```bash
# Check all resources
kubectl get all -n enterprise-agents

# Check pods
kubectl get pods -n enterprise-agents -o wide

# Check services
kubectl get svc -n enterprise-agents

# Check ingress (if enabled)
kubectl get ingress -n enterprise-agents

# Check persistent volumes
kubectl get pvc -n enterprise-agents

# View release info
helm list -n enterprise-agents
helm status enterprise-agents -n enterprise-agents
```

## Pre-Installation Checklist

### Infrastructure Requirements

- [ ] Kubernetes cluster 1.24+ running
- [ ] kubectl configured and working
- [ ] Helm 3.8+ installed
- [ ] StorageClass available (check with `kubectl get storageclass`)
- [ ] For vLLM: NVIDIA GPU nodes with nvidia-device-plugin
- [ ] For ingress: nginx-ingress controller installed

### Configuration Requirements

- [ ] Domain name configured (if using ingress)
- [ ] TLS certificates available (if using HTTPS)
- [ ] OIDC provider details (issuer URL, client ID)
- [ ] Secrets prepared (secret key, JWT secret, API keys)

### Security Requirements

- [ ] Network policies supported by CNI (Calico, Cilium, etc.)
- [ ] Pod Security Standards configured
- [ ] Image registry access configured (if private registry)

## Deployment Scenarios

### Scenario 1: Development Environment

**Characteristics:**
- Single node or small cluster
- No ingress (use port-forward)
- Ollama instead of vLLM
- Minimal resources
- Default secrets OK

```bash
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents-dev \
  --create-namespace \
  --set config.environment=dev \
  --set config.debug=true \
  --set vllm.enabled=false \
  --set ollama.enabled=true \
  --set ingress.enabled=false \
  --set autoscaling.enabled=false \
  --set networkPolicy.enabled=false
```

### Scenario 2: Staging Environment

**Characteristics:**
- Multi-node cluster
- Ingress enabled with staging domain
- vLLM with smaller model
- Moderate resources
- External secrets

```bash
# Create secrets first
kubectl create namespace enterprise-agents-staging
kubectl create secret generic enterprise-agent-platform-secrets \
  --namespace enterprise-agents-staging \
  --from-literal=secret-key="$(openssl rand -base64 32)" \
  --from-literal=dev-jwt-secret="$(openssl rand -base64 32)" \
  --from-literal=litellm-api-key="sk-staging-key" \
  --from-literal=database-url="postgresql+asyncpg://app:password@postgresql:5432/db"

# Install chart
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents-staging \
  --set config.environment=prod \
  --set config.debug=false \
  --set secrets.useExternalSecret=true \
  --set ingress.enabled=true \
  --set ingress.host=agents-staging.company.com \
  --set vllm.enabled=true \
  --set vllm.modelName="Qwen/Qwen2.5-32B-Instruct" \
  --set vllm.gpuCount=1
```

### Scenario 3: Production Environment

**Characteristics:**
- HA cluster with multiple nodes
- Ingress with production domain and TLS
- vLLM with large model
- Full resources and autoscaling
- NetworkPolicy enforcement
- External secrets operator

**Step 1: Prepare production values file**

```bash
cat > production-values.yaml <<EOF
image:
  repository: registry.company.com/enterprise-agent-platform
  tag: "1.0.0"
  pullPolicy: IfNotPresent
  pullSecrets:
    - name: registry-credentials

api:
  replicaCount: 3
  resources:
    requests:
      memory: "512Mi"
      cpu: "500m"
    limits:
      memory: "1Gi"
      cpu: "1000m"

worker:
  replicaCount: 3
  concurrency: 8

config:
  environment: prod
  debug: false
  oidcIssuerUrl: https://auth.company.com/realms/production
  oidcClientId: enterprise-agents
  oidcAudience: enterprise-agents-api
  tokenBudgetDaily: 5000000
  tokenBudgetMonthly: 100000000

secrets:
  useExternalSecret: true
  externalSecretName: enterprise-agent-platform-secrets

postgresql:
  primary:
    persistence:
      size: 100Gi
      storageClass: fast-ssd
    resources:
      requests:
        memory: "2Gi"
        cpu: "1000m"
      limits:
        memory: "4Gi"
        cpu: "2000m"

redis:
  master:
    persistence:
      size: 20Gi
      storageClass: fast-ssd

vllm:
  enabled: true
  modelName: "Qwen/Qwen2.5-72B-Instruct"
  gpuCount: 2
  resources:
    requests:
      memory: "64Gi"
      cpu: "16000m"
    limits:
      nvidia.com/gpu: 2

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/rate-limit: "100"
  host: agents.company.com
  tls:
    enabled: true
    secretName: enterprise-agent-platform-tls

autoscaling:
  enabled: true
  api:
    minReplicas: 3
    maxReplicas: 20
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80

networkPolicy:
  enabled: true

podDisruptionBudget:
  enabled: true
  minAvailable: 2

nodeSelector:
  workload: compute-intensive

tolerations:
  - key: workload
    operator: Equal
    value: compute-intensive
    effect: NoSchedule

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchExpressions:
          - key: app.kubernetes.io/component
            operator: In
            values:
            - api
        topologyKey: kubernetes.io/hostname
EOF
```

**Step 2: Install**

```bash
# Validate first
helm lint ./enterprise-agent-platform --values production-values.yaml

# Dry-run
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values production-values.yaml \
  --dry-run --debug > deployment-preview.yaml

# Review deployment-preview.yaml carefully

# Deploy
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values production-values.yaml \
  --timeout 10m \
  --wait
```

## Post-Installation Steps

### 1. Verify Deployment

```bash
# Check pod status (all should be Running)
kubectl get pods -n enterprise-agents

# Check services
kubectl get svc -n enterprise-agents

# Check HPA status
kubectl get hpa -n enterprise-agents

# View deployment details
helm status enterprise-agents -n enterprise-agents
```

### 2. Test API Endpoint

```bash
# Via ingress
curl https://agents.company.com/health

# Via port-forward
kubectl port-forward -n enterprise-agents svc/enterprise-agents-api 8000:8000 &
curl http://localhost:8000/health

# Expected response: {"status":"healthy"}
```

### 3. Check Logs

```bash
# API logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=api --tail=50

# Worker logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/component=worker --tail=50

# Check for errors
kubectl logs -n enterprise-agents -l app.kubernetes.io/instance=enterprise-agents --tail=100 | grep -i error
```

### 4. Verify Database

```bash
# Connect to PostgreSQL pod
kubectl exec -it -n enterprise-agents \
  $(kubectl get pod -n enterprise-agents -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U app -d enterprise_agents

# In psql:
\dx                      # List extensions (should see 'vector')
\dt                      # List tables
SELECT version();        # Check PostgreSQL version
\q                       # Quit
```

### 5. Monitor Resource Usage

```bash
# Current resource usage
kubectl top pods -n enterprise-agents
kubectl top nodes

# Watch for scaling events
kubectl get hpa -n enterprise-agents -w
```

## Upgrade Procedures

### Minor Updates (Configuration Changes)

```bash
# Update values file
vim production-values.yaml

# Upgrade
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml \
  --timeout 10m \
  --wait

# Verify
helm list -n enterprise-agents
kubectl rollout status deployment/enterprise-agents-api -n enterprise-agents
```

### Major Updates (Version Upgrades)

```bash
# Backup current release
helm get values enterprise-agents -n enterprise-agents > backup-values.yaml

# Test upgrade in dry-run
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml \
  --dry-run --debug

# Upgrade with rollback capability
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml \
  --timeout 15m \
  --wait \
  --atomic  # Auto-rollback on failure

# If manual rollback needed
helm rollback enterprise-agents -n enterprise-agents
```

## Troubleshooting Guide

### Issue: Pods Not Starting

```bash
# Check pod status
kubectl get pods -n enterprise-agents

# Describe problematic pod
kubectl describe pod -n enterprise-agents <pod-name>

# Common issues:
# - Image pull errors: Check image repository and pull secrets
# - Resource constraints: Check node resources with kubectl top nodes
# - Volume mount errors: Check PVC status with kubectl get pvc
```

### Issue: Database Connection Failures

```bash
# Check PostgreSQL pod
kubectl get pods -n enterprise-agents -l app.kubernetes.io/name=postgresql

# Check PostgreSQL logs
kubectl logs -n enterprise-agents -l app.kubernetes.io/name=postgresql --tail=100

# Test connectivity from API pod
kubectl exec -it -n enterprise-agents \
  $(kubectl get pod -n enterprise-agents -l app.kubernetes.io/component=api -o jsonpath='{.items[0].metadata.name}') \
  -- sh -c 'echo | nc enterprise-agents-postgresql 5432 && echo "Connected"'

# Check DATABASE_URL secret
kubectl get secret enterprise-agent-platform-secrets -n enterprise-agents -o jsonpath='{.data.database-url}' | base64 -d
```

### Issue: NetworkPolicy Blocking Traffic

```bash
# List NetworkPolicies
kubectl get networkpolicy -n enterprise-agents

# Describe NetworkPolicy
kubectl describe networkpolicy -n enterprise-agents

# Temporarily disable for debugging
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --reuse-values \
  --set networkPolicy.enabled=false

# Re-enable after fixing
helm upgrade enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --reuse-values \
  --set networkPolicy.enabled=true
```

### Issue: GPU Not Available for vLLM

```bash
# Check GPU nodes
kubectl get nodes -l nvidia.com/gpu=true

# Check NVIDIA device plugin
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds

# Check vLLM pod events
kubectl describe pod -n enterprise-agents -l app.kubernetes.io/component=vllm

# Verify GPU allocation
kubectl get pod -n enterprise-agents -l app.kubernetes.io/component=vllm -o json | \
  jq '.items[].spec.containers[].resources'
```

## Backup and Disaster Recovery

### Backup Database

```bash
# Backup PostgreSQL
kubectl exec -n enterprise-agents \
  $(kubectl get pod -n enterprise-agents -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}') \
  -- pg_dump -U app enterprise_agents > backup-$(date +%Y%m%d).sql

# Backup to S3 (if configured)
kubectl exec -n enterprise-agents \
  $(kubectl get pod -n enterprise-agents -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}') \
  -- pg_dump -U app enterprise_agents | \
  aws s3 cp - s3://backups/enterprise-agents/$(date +%Y%m%d).sql.gz
```

### Backup Helm Release

```bash
# Export current values
helm get values enterprise-agents -n enterprise-agents > backup-values.yaml

# Export all manifests
helm get manifest enterprise-agents -n enterprise-agents > backup-manifests.yaml

# Export release metadata
helm history enterprise-agents -n enterprise-agents > backup-history.txt
```

### Restore Procedures

```bash
# Restore database
cat backup-20260217.sql | \
  kubectl exec -i -n enterprise-agents \
  $(kubectl get pod -n enterprise-agents -l app.kubernetes.io/name=postgresql -o jsonpath='{.items[0].metadata.name}') \
  -- psql -U app enterprise_agents

# Restore release
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --create-namespace \
  --values backup-values.yaml
```

## Uninstallation

### Complete Removal

```bash
# 1. Delete Helm release
helm uninstall enterprise-agents -n enterprise-agents

# 2. Delete namespace (includes all resources)
kubectl delete namespace enterprise-agents

# 3. Delete persistent volumes (if needed)
kubectl get pv | grep enterprise-agents
kubectl delete pv <pv-name>
```

### Partial Removal (Keep Data)

```bash
# Delete release but keep PVCs
helm uninstall enterprise-agents -n enterprise-agents

# List remaining PVCs
kubectl get pvc -n enterprise-agents

# Reinstall later with existing PVCs
helm install enterprise-agents ./enterprise-agent-platform \
  --namespace enterprise-agents \
  --values production-values.yaml
```

## Support and Resources

- **Helm Chart Documentation**: `./enterprise-agent-platform/README.md`
- **Application Documentation**: `/mnt/c/AI/enterprise-agent-platform/README.md`
- **Issue Tracker**: GitHub Issues
- **Support Email**: platform@example.com

## Appendix: kubectl Cheat Sheet

```bash
# Get all resources
kubectl get all -n enterprise-agents

# Watch pods
kubectl get pods -n enterprise-agents -w

# Follow logs
kubectl logs -f -n enterprise-agents <pod-name>

# Execute commands in pod
kubectl exec -it -n enterprise-agents <pod-name> -- sh

# Port forwarding
kubectl port-forward -n enterprise-agents svc/<service-name> <local-port>:<remote-port>

# Copy files
kubectl cp -n enterprise-agents <pod-name>:/path/to/file ./local-file

# Resource usage
kubectl top pods -n enterprise-agents
kubectl top nodes

# Debugging
kubectl describe pod -n enterprise-agents <pod-name>
kubectl get events -n enterprise-agents --sort-by='.lastTimestamp'
```
