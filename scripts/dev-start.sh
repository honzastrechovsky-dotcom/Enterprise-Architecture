#!/usr/bin/env bash
# =============================================================================
# Enterprise Agent Platform - Local Development Startup Script
#
# Brings up the full dev stack, waits for each service to be healthy,
# runs database migrations, and seeds sample data.
#
# Usage:
#   ./scripts/dev-start.sh          # normal start
#   ./scripts/dev-start.sh --reset  # tear down volumes, start fresh
#
# Requirements:
#   - docker compose v2 (docker compose, not docker-compose)
#   - python 3.12+ with project dependencies installed (for seed + alembic)
#   - .env.dev file present (copied from .env.example)
# =============================================================================

set -euo pipefail

COMPOSE_FILE="docker-compose.dev.yml"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ---- colour helpers ---------------------------------------------------------
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'  # no colour

info()    { echo -e "${CYAN}[dev-start]${NC} $*"; }
success() { echo -e "${GREEN}[dev-start]${NC} $*"; }
warn()    { echo -e "${YELLOW}[dev-start]${NC} $*"; }
error()   { echo -e "${RED}[dev-start]${NC} $*" >&2; exit 1; }

# ---- parse arguments --------------------------------------------------------
RESET=false
for arg in "$@"; do
  case "$arg" in
    --reset) RESET=true ;;
    --help|-h)
      echo "Usage: $0 [--reset]"
      echo "  --reset   Stop containers, remove volumes, start fresh"
      exit 0
      ;;
    *) warn "Unknown argument: $arg" ;;
  esac
done

# ---- ensure .env.dev exists -------------------------------------------------
if [ ! -f ".env.dev" ]; then
  warn ".env.dev not found. Copying from .env.example..."
  if [ -f ".env.example" ]; then
    cp .env.example .env.dev
    warn "Created .env.dev from .env.example. Review and update as needed."
  else
    error ".env.example also missing. Cannot create .env.dev automatically."
  fi
fi

# ---- optional reset ---------------------------------------------------------
if [ "$RESET" = true ]; then
  info "Reset requested: stopping containers and removing named volumes..."
  docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans 2>/dev/null || true
  success "Volumes cleared."
fi

# ---- start services ---------------------------------------------------------
info "Starting services with: docker compose -f $COMPOSE_FILE up -d"
docker compose -f "$COMPOSE_FILE" up -d --build

# ---- wait for db health -----------------------------------------------------
info "Waiting for PostgreSQL to be healthy..."
RETRIES=30
DELAY=3
for i in $(seq 1 $RETRIES); do
  STATUS=$(docker compose -f "$COMPOSE_FILE" ps --format json db 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health','unknown'))" 2>/dev/null \
    || echo "unknown")

  if docker compose -f "$COMPOSE_FILE" exec -T db \
      pg_isready -U app -d enterprise_agents > /dev/null 2>&1; then
    success "PostgreSQL is ready."
    break
  fi

  if [ "$i" -eq "$RETRIES" ]; then
    error "PostgreSQL did not become healthy after $((RETRIES * DELAY))s."
  fi

  echo -n "  waiting ($i/$RETRIES)..."
  sleep "$DELAY"
  echo " retrying"
done

# ---- wait for redis health --------------------------------------------------
info "Waiting for Redis to be healthy..."
for i in $(seq 1 $RETRIES); do
  if docker compose -f "$COMPOSE_FILE" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
    success "Redis is ready."
    break
  fi
  if [ "$i" -eq "$RETRIES" ]; then
    error "Redis did not become healthy after $((RETRIES * DELAY))s."
  fi
  echo -n "  waiting ($i/$RETRIES)..."
  sleep "$DELAY"
  echo " retrying"
done

# ---- run alembic migrations -------------------------------------------------
info "Running database migrations (alembic upgrade head)..."

# Prefer running inside the api container if it is healthy to avoid
# any local Python path issues. Fall back to local Python if needed.
if docker compose -f "$COMPOSE_FILE" ps --services 2>/dev/null | grep -q "^api$"; then
  # Give the API container a moment to finish building/starting
  sleep 3
  if docker compose -f "$COMPOSE_FILE" exec -T api \
      alembic upgrade head 2>&1; then
    success "Migrations applied (inside api container)."
  else
    warn "Migration via container failed; trying locally..."
    DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
      alembic upgrade head || error "Alembic migration failed."
    success "Migrations applied (local)."
  fi
else
  DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
    alembic upgrade head || error "Alembic migration failed."
  success "Migrations applied (local)."
fi

# ---- seed sample data -------------------------------------------------------
info "Seeding sample data..."

if docker compose -f "$COMPOSE_FILE" ps --services 2>/dev/null | grep -q "^api$"; then
  docker compose -f "$COMPOSE_FILE" exec -T api \
    python /app/scripts/seed.py 2>&1 || {
      warn "Seed via container failed; trying locally..."
      DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
        python scripts/seed.py || warn "Seed script encountered errors (may already be seeded)."
    }
else
  DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents" \
    python scripts/seed.py || warn "Seed script encountered errors (may already be seeded)."
fi

success "Data seeded."

# ---- final status -----------------------------------------------------------
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN} Enterprise Agent Platform - Development Stack Ready${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo -e "  ${CYAN}API (FastAPI)${NC}  : http://localhost:8000"
echo -e "  ${CYAN}API Docs       ${NC}: http://localhost:8000/docs"
echo -e "  ${CYAN}Frontend (Vite)${NC}: http://localhost:5173"
echo -e "  ${CYAN}LiteLLM Proxy  ${NC}: http://localhost:4000"
echo -e "  ${CYAN}PostgreSQL     ${NC}: localhost:5432  (db: enterprise_agents, user: app)"
echo -e "  ${CYAN}Redis          ${NC}: localhost:6379"
echo ""
echo -e "  Logs : docker compose -f $COMPOSE_FILE logs -f"
echo -e "  Stop : make dev-stop   (or docker compose -f $COMPOSE_FILE down)"
echo ""
