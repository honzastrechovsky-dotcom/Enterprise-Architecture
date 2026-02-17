.PHONY: help test test-unit test-integration test-all test-cov clean lint format install \
        dev dev-stop dev-reset seed mock-llm migrate \
        db-backup db-restore db-health db-maintenance db-migrate db-rollback db-shell

COMPOSE_DEV := docker compose -f docker-compose.dev.yml

help:  ## Show this help message
	@echo "Enterprise Agent Platform - Makefile Commands"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

install:  ## Install dependencies
	pip install -e ".[dev]"

# ============================================================
# Local Development Stack (Phase 11C)
# ============================================================

dev:  ## Start full local dev stack (API + DB + Redis + Frontend + LiteLLM)
	@bash scripts/dev-start.sh

dev-up:  ## Start dev containers without migration/seed (faster restart)
	$(COMPOSE_DEV) up -d --build

dev-stop:  ## Stop all dev containers (data preserved)
	$(COMPOSE_DEV) down

dev-reset:  ## Full reset: stop, remove volumes, restart, migrate, seed
	@echo "WARNING: This will DELETE all Docker volumes and data."
	@read -p "Type 'yes' to confirm: " confirm && [ "$$confirm" = "yes" ] || (echo "Aborted." && exit 1)
	$(COMPOSE_DEV) down --volumes --remove-orphans
	@bash scripts/dev-start.sh

dev-logs:  ## Tail logs from all dev containers
	$(COMPOSE_DEV) logs -f

dev-logs-api:  ## Tail logs from the API container only
	$(COMPOSE_DEV) logs -f api

dev-shell:  ## Open a bash shell inside the running API container
	$(COMPOSE_DEV) exec api bash

dev-status:  ## Show status of all dev containers
	$(COMPOSE_DEV) ps

seed:  ## Seed the database with sample tenants, users, conversations, and documents
	python scripts/seed.py

mock-llm:  ## Start the mock LLM provider on port 4000 (fully offline dev)
	uvicorn src.testing.mock_llm:app --host 0.0.0.0 --port 4000 --reload

migrate:  ## Run database migrations
	alembic upgrade head

migrate-create:  ## Create new migration (usage: make migrate-create message="your message")
	alembic revision --autogenerate -m "$(message)"

# ============================================================
# Database Operations (Phase 7C)
# ============================================================

BACKUP_DIR ?= ./backups
BACKUP_MODE ?= full

db-backup:  ## Backup database (usage: make db-backup [BACKUP_MODE=full|schema|data] [BACKUP_DIR=./backups])
	@bash scripts/backup.sh $(BACKUP_MODE) $(BACKUP_DIR)

db-restore:  ## Restore database from backup (usage: make db-restore BACKUP_FILE=<path> [CREATE_DB=1])
	@if [ -z "$(BACKUP_FILE)" ]; then \
		echo "Usage: make db-restore BACKUP_FILE=<path_to_dump> [CREATE_DB=1]"; \
		exit 1; \
	fi
	@if [ -n "$(CREATE_DB)" ]; then \
		bash scripts/restore.sh $(BACKUP_FILE) --create-db; \
	else \
		bash scripts/restore.sh $(BACKUP_FILE); \
	fi

db-health:  ## Check database health (outputs JSON; exit 0=healthy, 1=warning, 2=critical)
	@bash scripts/db-health.sh

db-health-pretty:  ## Check database health with pretty-printed JSON output
	@bash scripts/db-health.sh | python3 -m json.tool

db-maintenance:  ## Run VACUUM ANALYZE, reindex bloated indexes, update statistics
	@python3 scripts/db-maintenance.py

db-maintenance-dry:  ## Preview maintenance actions without executing (dry run)
	@DRY_RUN=true python3 scripts/db-maintenance.py

db-migrate:  ## Run pending database migrations (alias for migrate)
	alembic upgrade head

db-rollback:  ## Rollback last migration (usage: make db-rollback [STEPS=1])
	alembic downgrade -$(or $(STEPS),1)

db-shell:  ## Open psql shell into the database (Docker or local)
	@if docker compose -f docker-compose.dev.yml ps --services 2>/dev/null | grep -q "^db$$"; then \
		docker compose -f docker-compose.dev.yml exec db psql -U app -d enterprise_agents; \
	else \
		psql "$(or $(DATABASE_URL),postgresql://app:app_password@localhost:5432/enterprise_agents)" ; \
	fi

test:  ## Run all tests (unit + integration)
	pytest

test-unit:  ## Run only unit tests (exclude integration)
	pytest -m "not integration"

test-integration:  ## Run only integration tests
	pytest -m integration tests/integration/

test-integration-sqlite:  ## Run integration tests with SQLite (fast)
	pytest -m integration tests/integration/ -v

test-integration-postgres:  ## Run integration tests with PostgreSQL (requires docker-compose)
	DATABASE_URL=postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents \
	pytest -m integration tests/integration/ -v

test-cov:  ## Run tests with coverage report
	pytest --cov=src --cov-report=term-missing --cov-report=html

test-watch:  ## Run tests in watch mode (requires pytest-watch)
	ptw -- -v

lint:  ## Run linters (ruff)
	ruff check src/ tests/

lint-fix:  ## Fix linting issues automatically
	ruff check --fix src/ tests/

format:  ## Format code with ruff
	ruff format src/ tests/

format-check:  ## Check code formatting without making changes
	ruff format --check src/ tests/

typecheck:  ## Run type checker (mypy)
	mypy src/

clean:  ## Clean up generated files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete
	rm -rf dist/ build/ *.egg-info

docker-build:  ## Build Docker image
	docker-compose build api

docker-up:  ## Start all services
	docker-compose up -d

docker-down:  ## Stop all services
	docker-compose down

docker-logs:  ## Show logs from all services
	docker-compose logs -f

docker-logs-api:  ## Show logs from API service
	docker-compose logs -f api

docker-shell:  ## Open shell in API container
	docker-compose exec api bash

redis-shell:  ## Open Redis CLI
	docker-compose exec redis redis-cli

run:  ## Run API server locally (development)
	uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

run-prod:  ## Run API server in production mode
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 4

check:  ## Run all checks (lint, format, typecheck, test)
	@echo "Running format check..."
	@make format-check
	@echo "\nRunning linter..."
	@make lint
	@echo "\nRunning type checker..."
	@make typecheck
	@echo "\nRunning unit tests..."
	@make test-unit
	@echo "\n✅ All checks passed!"

ci:  ## Run CI checks (format, lint, typecheck, test with coverage)
	@make format-check
	@make lint
	@make typecheck
	@make test-cov
