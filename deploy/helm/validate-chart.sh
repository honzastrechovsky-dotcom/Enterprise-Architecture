#!/bin/bash
#
# Helm Chart Validation Script
# Tests the Enterprise Agent Platform Helm chart for common issues
#

set -e

CHART_DIR="./enterprise-agent-platform"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "Enterprise Agent Platform - Chart Validation"
echo "=========================================="
echo ""

# Check if helm is installed
if ! command -v helm &> /dev/null; then
    echo -e "${RED}ERROR: helm is not installed${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Helm is installed ($(helm version --short))"

# Check if chart directory exists
if [ ! -d "$CHART_DIR" ]; then
    echo -e "${RED}ERROR: Chart directory not found: $CHART_DIR${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Chart directory exists"

# Validate Chart.yaml
echo ""
echo "Checking Chart.yaml..."
if [ ! -f "$CHART_DIR/Chart.yaml" ]; then
    echo -e "${RED}ERROR: Chart.yaml not found${NC}"
    exit 1
fi

NAME=$(grep '^name:' "$CHART_DIR/Chart.yaml" | awk '{print $2}')
VERSION=$(grep '^version:' "$CHART_DIR/Chart.yaml" | awk '{print $2}')
APP_VERSION=$(grep '^appVersion:' "$CHART_DIR/Chart.yaml" | awk '{print $2}')

echo -e "${GREEN}✓${NC} Chart: $NAME"
echo -e "${GREEN}✓${NC} Version: $VERSION"
echo -e "${GREEN}✓${NC} App Version: $APP_VERSION"

# Validate values.yaml
echo ""
echo "Checking values.yaml..."
if [ ! -f "$CHART_DIR/values.yaml" ]; then
    echo -e "${RED}ERROR: values.yaml not found${NC}"
    exit 1
fi

# Check for insecure defaults
if grep -q "dev-secret-key-not-for-production" "$CHART_DIR/values.yaml"; then
    echo -e "${YELLOW}⚠${NC}  Default secrets detected (OK for development)"
fi

echo -e "${GREEN}✓${NC} values.yaml exists"

# Check templates directory
echo ""
echo "Checking templates..."
TEMPLATE_COUNT=$(find "$CHART_DIR/templates" -name "*.yaml" -o -name "*.tpl" | wc -l)
echo -e "${GREEN}✓${NC} Found $TEMPLATE_COUNT template files"

# Required templates
REQUIRED_TEMPLATES=(
    "deployment-api.yaml"
    "deployment-worker.yaml"
    "service-api.yaml"
    "configmap.yaml"
    "secret.yaml"
    "_helpers.tpl"
    "NOTES.txt"
)

for template in "${REQUIRED_TEMPLATES[@]}"; do
    if [ -f "$CHART_DIR/templates/$template" ]; then
        echo -e "${GREEN}✓${NC} $template"
    else
        echo -e "${RED}✗${NC} Missing: $template"
        exit 1
    fi
done

# Run helm lint
echo ""
echo "Running helm lint..."
if helm lint "$CHART_DIR" > /tmp/helm-lint.log 2>&1; then
    echo -e "${GREEN}✓${NC} Helm lint passed"
    cat /tmp/helm-lint.log
else
    echo -e "${RED}✗${NC} Helm lint failed"
    cat /tmp/helm-lint.log
    exit 1
fi

# Dry-run install
echo ""
echo "Running dry-run installation..."
if helm install test-release "$CHART_DIR" \
    --dry-run \
    --debug \
    --namespace test \
    > /tmp/helm-dry-run.log 2>&1; then
    echo -e "${GREEN}✓${NC} Dry-run install successful"
    echo "  Generated $(grep -c '^---' /tmp/helm-dry-run.log) Kubernetes resources"
else
    echo -e "${RED}✗${NC} Dry-run install failed"
    cat /tmp/helm-dry-run.log
    exit 1
fi

# Template all manifests
echo ""
echo "Templating all manifests..."
if helm template test-release "$CHART_DIR" \
    --namespace test \
    > /tmp/helm-template.yaml 2>&1; then
    echo -e "${GREEN}✓${NC} Template generation successful"

    # Count resources
    DEPLOYMENTS=$(grep -c 'kind: Deployment' /tmp/helm-template.yaml || true)
    SERVICES=$(grep -c 'kind: Service' /tmp/helm-template.yaml || true)
    CONFIGMAPS=$(grep -c 'kind: ConfigMap' /tmp/helm-template.yaml || true)
    SECRETS=$(grep -c 'kind: Secret' /tmp/helm-template.yaml || true)

    echo "  - Deployments: $DEPLOYMENTS"
    echo "  - Services: $SERVICES"
    echo "  - ConfigMaps: $CONFIGMAPS"
    echo "  - Secrets: $SECRETS"
else
    echo -e "${RED}✗${NC} Template generation failed"
    cat /tmp/helm-template.yaml
    exit 1
fi

# Validate with different values
echo ""
echo "Testing with production values..."
cat > /tmp/prod-values.yaml <<EOF
config:
  environment: prod
  debug: false

secrets:
  secretKey: "production-secret-key-override"
  devJwtSecret: "production-jwt-secret-override"
  litellmApiKey: "sk-production-key"

ingress:
  enabled: true
  host: agents.example.com

networkPolicy:
  enabled: true

autoscaling:
  enabled: true
EOF

if helm template test-release "$CHART_DIR" \
    --values /tmp/prod-values.yaml \
    --namespace test \
    > /tmp/helm-template-prod.yaml 2>&1; then
    echo -e "${GREEN}✓${NC} Production values template successful"

    # Verify production settings
    if grep -q 'environment: prod' /tmp/helm-template-prod.yaml && \
       grep -q 'kind: Ingress' /tmp/helm-template-prod.yaml && \
       grep -q 'kind: NetworkPolicy' /tmp/helm-template-prod.yaml; then
        echo -e "${GREEN}✓${NC} Production features verified"
    else
        echo -e "${YELLOW}⚠${NC}  Some production features not found"
    fi
else
    echo -e "${RED}✗${NC} Production values template failed"
    exit 1
fi

# Security checks
echo ""
echo "Running security checks..."

SECURITY_ISSUES=0

# Check for runAsNonRoot
if grep -q 'runAsNonRoot: true' /tmp/helm-template.yaml; then
    echo -e "${GREEN}✓${NC} Non-root containers configured"
else
    echo -e "${RED}✗${NC} Non-root containers not configured"
    SECURITY_ISSUES=$((SECURITY_ISSUES + 1))
fi

# Check for readOnlyRootFilesystem
if grep -q 'readOnlyRootFilesystem: true' /tmp/helm-template.yaml; then
    echo -e "${GREEN}✓${NC} Read-only root filesystem configured"
else
    echo -e "${YELLOW}⚠${NC}  Read-only root filesystem not found"
fi

# Check for dropped capabilities
if grep -q 'drop:' /tmp/helm-template.yaml && grep -q '- ALL' /tmp/helm-template.yaml; then
    echo -e "${GREEN}✓${NC} Capabilities dropped"
else
    echo -e "${YELLOW}⚠${NC}  Capability dropping not found"
fi

# Summary
echo ""
echo "=========================================="
echo "Validation Summary"
echo "=========================================="

if [ $SECURITY_ISSUES -eq 0 ]; then
    echo -e "${GREEN}✓ All validation checks passed!${NC}"
    echo ""
    echo "Chart is ready for deployment:"
    echo "  helm install enterprise-agents $CHART_DIR --namespace enterprise-agents --create-namespace"
    exit 0
else
    echo -e "${YELLOW}⚠ Validation completed with warnings${NC}"
    echo "  Security issues found: $SECURITY_ISSUES"
    echo "  Review issues before production deployment"
    exit 0
fi
