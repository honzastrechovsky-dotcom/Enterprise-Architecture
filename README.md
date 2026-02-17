# Enterprise Agent Platform

> Production-grade multi-tenant AI agent orchestration platform with structured reasoning, enterprise system integration, and comprehensive compliance coverage.

[![License](https://img.shields.io/badge/license-Proprietary-red.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-1235%20passed-brightgreen)]()
[![Security Audit](https://img.shields.io/badge/security%20audit-Opus%204.6-blue)]()

**Status:** Phases 1–11 Complete | Security Audited | 1235 Tests, 0 Failures
**Target Deployment:** Industrial/Manufacturing Enterprise | On-Premise | Zero Data Egress

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Development](#development)
- [Deployment](#deployment)
- [Security](#security)
- [Compliance](#compliance)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

The **Enterprise Agent Platform** is a production-ready, multi-tenant AI agent orchestration system designed for enterprise environments with strict security, compliance, and operational requirements. Built for **industrial and manufacturing operations**, it delivers intelligent document Q&A, enterprise system integration (SAP, MES), and structured reasoning with human-in-the-loop controls.

### Who Is This For?

- **Industrial/Manufacturing Enterprises** requiring on-premise AI deployment with no data egress
- **Regulated Industries** needing SOC 2, GDPR, ISO 27001 compliance
- **Organizations with Complex Enterprise Systems** (SAP, MES, legacy databases)
- **Teams requiring AI safety controls** with human approval workflows for critical operations

### What Makes This Different?

Unlike generic LLM platforms, this system provides:

1. **True Multi-Tenant Isolation** - Every database query is tenant-scoped with row-level security
2. **Structured Reasoning** - OBSERVE→THINK→VERIFY loops with RedTeam, FirstPrinciples, and Council thinking tools
3. **Enterprise Integration** - Native SAP RFC/OData, MES connectors with SQL injection protection
4. **Human-in-the-Loop Safety** - All write operations require approval before execution with rollback support
5. **Air-Gap Ready** - Fully operational without internet access using Ollama for on-premise LLM deployment
6. **Compliance by Design** - Automated evidence generation for SOC 2, GDPR Article 15/17/20, ISO 27001

Built using a **Sonnet+Opus pooling pattern** (~35 Sonnet agents for implementation, 5 Opus agents for architecture and security review) across multiple sessions. Opus security audit (Phase 11) identified and resolved 20 issues including CRITICAL auth bypasses and SQL injection vectors.

---

## Key Features

### Core Capabilities

- **Multi-Tenant Architecture**
  - Row-level security with automatic tenant filtering on all database queries
  - Per-tenant token budgets and rate limiting
  - Isolated vector stores for RAG (Retrieval-Augmented Generation)
  - Just-in-time user provisioning via SSO/OIDC

- **Structured Reasoning Engine**
  - OBSERVE→THINK→VERIFY loop for systematic problem-solving
  - Thinking tools: RedTeam (adversarial analysis), FirstPrinciples (deconstruction), Council (multi-agent debate)
  - Goal planner with automatic sub-task decomposition
  - Agent memory across conversation turns

- **Advanced Agent Orchestration**
  - Composition patterns: Pipeline, FanOut, Gate, TDDLoop
  - Specialist agents: Document Analyst, Maintenance Advisor, Data Analyst, Generalist
  - Model routing with 3-tier complexity detection (LIGHT/STANDARD/HEAVY)
  - Automatic fallback on model failures

- **Enterprise-Grade RAG**
  - Hybrid search combining vector similarity (pgvector) and BM25 keyword matching
  - Reranking pipeline for relevance optimization
  - Document versioning and citation tracking
  - Multi-format ingestion: PDF, DOCX, TXT, Markdown

- **Enterprise System Integration**
  - **SAP:** RFC and OData connectors with credential vault integration
  - **MES Systems:** ODBC/pyodbc with SQL Guard (query validation and sanitization)
  - **Human-in-the-Loop (HITL):** Approval workflow for all write operations with rollback capability
  - **Async Execution:** Celery-based background jobs for long-running operations

- **Model Economy & Routing**
  - 3-tier routing: 7B models (LIGHT), 32B models (STANDARD), 72B models (HEAVY)
  - Per-tenant token budgets with overflow protection
  - LiteLLM proxy for multi-provider support (OpenAI, Anthropic, Azure, AWS)
  - Ollama integration for on-premise deployment (air-gap environments)

- **Security & Compliance**
  - JWT authentication (RS256/HS256) with OIDC/Keycloak integration
  - Role-based access control (RBAC): Admin, Operator, Viewer
  - PII redaction with configurable regex patterns
  - Prompt injection detection using classifier models
  - Data classification labels (PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED)
  - Export control tracking (EAR/ITAR classifications)
  - Immutable audit logging for every operation
  - Encryption at rest (database) and in transit (TLS 1.3)

- **Observability & Operations**
  - Structured logging with `structlog`
  - OpenTelemetry tracing for distributed request tracking
  - Prometheus metrics (request latency, token usage, error rates)
  - Health checks: `/health/live`, `/health/ready`
  - Grafana-compatible monitoring dashboards

- **Internationalization & Scale**
  - Multi-region replication support (active-passive, active-active)
  - i18n framework for multi-language deployments
  - Fine-tuning workflows for domain adaptation
  - Read replica routing for read-heavy workloads

---

## Architecture

The platform implements a **7-layer architecture** with defense-in-depth security and complete tenant isolation.

```
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 7: CLIENT                                                  │
│   React 19 UI (SSE streaming) │ curl/CLI │ REST API              │
└──────────────────────────────────────────────────────────────────┘
                            │
                     HTTPS + JWT Bearer
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 6: API GATEWAY (FastAPI)                                  │
│   Auth Middleware → Rate Limiter → CORS → Telemetry             │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 5: POLICY ENGINE                                           │
│   RBAC │ Tenant Isolation │ Data Classification                  │
│   PII Redaction │ Audit Logging │ Prompt Injection Detection    │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 4: AGENT RUNTIME                                           │
│   Goal Planner │ Agent Memory │ Model Router                     │
│   Composition Patterns: Pipeline │ FanOut │ Gate │ TDDLoop      │
│   Token Budgets │ Complexity Detection │ Fallback Logic         │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 3: REASONING ENGINE                                        │
│   OBSERVE → THINK → VERIFY Loop                                 │
│   Thinking Tools: RedTeam │ FirstPrinciples │ Council           │
│   Specialist Agents: DocumentAnalyst │ MaintenanceAdvisor │ ... │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 2: TOOLS & CONNECTORS                                      │
│   RAG: Hybrid Search (vector + BM25) │ Reranker │ Citations     │
│   SAP: RFC/OData │ MES: ODBC + SQL Guard │ Document Ingestion   │
│   HITL: Approval Workflow with Rollback                          │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 1: MODEL & INFRASTRUCTURE                                  │
│   LiteLLM (multi-provider) │ Ollama (on-prem)                    │
│   PostgreSQL 16 + pgvector │ Redis (rate limit/cache)            │
│   Celery + RabbitMQ (async jobs)                                 │
└──────────────────────────────────────────────────────────────────┘
```

### Key Architectural Principles

- **Defense in Depth:** Multiple security layers (JWT → RBAC → Classification → PII Redaction → Audit)
- **Tenant Isolation:** Every query includes `WHERE tenant_id = ?` via `apply_tenant_filter()`
- **Immutable Audit:** All operations logged to `audit_logs` table with tamper detection
- **HITL Gate:** All write operations require human approval before execution
- **Zero Trust:** No operation trusts prior validation; every layer re-validates tenant/user/permissions

---

## Quick Start

### Prerequisites

- **Docker** 24.0+ and **Docker Compose** 2.20+
- **Python** 3.12+ (for local development outside Docker)
- **Git** (to clone repository)

### 5-Command Setup

```bash
# 1. Clone repository
git clone https://github.com/honzastrechovsky-dotcom/enterprise-agent-platform-v2.git
cd enterprise-agent-platform-v2

# 2. Configure environment
cp .env.example .env
# Edit .env: set DATABASE_URL, SECRET_KEY, LITELLM_API_KEY

# 3. Start all services (API + PostgreSQL + Redis + LiteLLM + Ollama)
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# 4. Initialize database schema and run migrations
docker-compose exec api python -m src.scripts.init_db

# 5. Seed test data (creates 2 tenants with admin/viewer users)
docker-compose exec api python -m src.scripts.seed_data
```

### Verify Installation

```bash
# Health check
curl http://localhost:8000/health/ready

# Expected output:
# {"status": "healthy", "database": "connected", "redis": "connected"}

# Get JWT token from seed_data output (printed to console)
export TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

# Test chat endpoint with streaming
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What documents do I have access to?", "stream": true}'

# Upload a document for RAG
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sample.pdf" \
  -F "classification=INTERNAL"
```

### Access the Web UI

```bash
cd frontend
npm install
npm run dev
# UI available at http://localhost:3000
# Login with credentials from seed_data output
```

**Default Seed Users:**

| Tenant | Email | Role | Password |
|--------|-------|------|----------|
| TenantA | admin@tenanta.example.com | admin | password123 |
| TenantA | viewer@tenanta.example.com | viewer | password123 |
| TenantB | admin@tenantb.example.com | admin | password123 |

---

## Configuration

All configuration is managed via environment variables. Copy `.env.example` to `.env` and customize.

### Essential Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| **Application** |
| `ENVIRONMENT` | Deployment environment | `dev` \| `prod` |
| `SECRET_KEY` | JWT signing key (256-bit hex) | `python -c "import secrets; print(secrets.token_hex(32))"` |
| **Database** |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://app:password@db:5432/enterprise_agents` |
| `POSTGRES_USER` | Database username | `app` |
| `POSTGRES_PASSWORD` | Database password | `secure-password` |
| `POSTGRES_DB` | Database name | `enterprise_agents` |
| **LiteLLM** |
| `LITELLM_BASE_URL` | LiteLLM proxy URL | `http://localhost:4000` |
| `LITELLM_API_KEY` | API key for LiteLLM | `sk-litellm-dev-key` |
| `LITELLM_DEFAULT_MODEL` | Default LLM model | `openai/gpt-4o-mini` |
| `LITELLM_EMBEDDING_MODEL` | Embedding model for RAG | `openai/text-embedding-3-small` |
| **Ollama (Optional - for air-gap)** |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://localhost:11434` |
| `OLLAMA_DEFAULT_MODEL` | Ollama model name | `llama3.2` |
| **Authentication** |
| `OIDC_ISSUER_URL` | OpenID Connect issuer | `https://keycloak.example.com/realms/enterprise` |
| `OIDC_CLIENT_ID` | OIDC client identifier | `enterprise-agents` |
| `OIDC_AUDIENCE` | Expected JWT audience | `enterprise-agents-api` |
| `DEV_JWT_SECRET` | Dev-only JWT secret (skip OIDC) | `dev-only-secret` |
| **Security** |
| `CORS_ALLOWED_ORIGINS` | Allowed CORS origins (JSON list) | `["https://app.example.com"]` |
| `RATE_LIMIT_PER_MINUTE` | Rate limit per user | `60` |
| **RAG** |
| `CHUNK_SIZE_TOKENS` | Document chunk size | `512` |
| `CHUNK_OVERLAP_TOKENS` | Chunk overlap for context | `50` |
| `VECTOR_TOP_K` | Number of vectors to retrieve | `5` |
| **Observability** |
| `LOG_LEVEL` | Logging verbosity | `INFO` \| `DEBUG` \| `WARNING` |
| `SENTRY_DSN` | Sentry error tracking URL | `https://...@sentry.io/...` |

### Configuration Notes

- **Production:** Set `ENVIRONMENT=prod`, generate secure `SECRET_KEY`, configure OIDC with real identity provider
- **Development:** Use `DEV_JWT_SECRET` to skip OIDC validation (dev mode accepts JWT with HS256)
- **Air-Gap:** Set `OLLAMA_BASE_URL` and `OLLAMA_DEFAULT_MODEL` to use on-premise LLMs without internet
- **CORS:** In production, specify exact allowed origins; dev mode allows all origins

---

## API Reference

All endpoints require **JWT Bearer authentication** with valid `tenant_id`, `sub` (user ID), `role`, and `exp` (expiration) claims.

### Authentication

```http
Authorization: Bearer <jwt_token>
```

**Required JWT Claims:**

```json
{
  "sub": "user-uuid-here",
  "tenant_id": "tenant-uuid-here",
  "role": "admin",  // admin | operator | viewer
  "exp": 1735689600  // Unix timestamp
}
```

### Endpoint Groups

#### Chat & Conversations

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/chat` | viewer+ | Send message to agent (supports SSE streaming) |
| `GET` | `/api/v1/conversations` | viewer+ | List user's conversations with pagination |
| `GET` | `/api/v1/conversations/{id}` | viewer+ | Get conversation details and message history |
| `DELETE` | `/api/v1/conversations/{id}` | viewer+ | Delete conversation (soft delete) |

**Example Chat Request:**

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Analyze production downtime from last week",
    "stream": true,
    "conversation_id": "optional-uuid-for-continuation"
  }'
```

#### Document Management (RAG)

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/documents/upload` | operator+ | Upload document for RAG ingestion |
| `GET` | `/api/v1/documents` | viewer+ | List documents with metadata and versioning |
| `GET` | `/api/v1/documents/{id}` | viewer+ | Get document details and chunk preview |
| `DELETE` | `/api/v1/documents/{id}` | operator+ | Delete document and embeddings |
| `POST` | `/api/v1/documents/{id}/reindex` | operator+ | Re-chunk and re-embed document |

**Example Upload:**

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@maintenance_manual.pdf" \
  -F "classification=CONFIDENTIAL" \
  -F "metadata={\"department\":\"maintenance\",\"version\":\"2.1\"}"
```

#### Administration

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/admin/users` | admin | Create user in tenant |
| `GET` | `/api/v1/admin/users` | admin | List tenant users |
| `PATCH` | `/api/v1/admin/users/{id}` | admin | Update user role or status |
| `GET` | `/api/v1/admin/audit` | admin | Query audit logs with filtering |
| `GET` | `/api/v1/admin/metrics` | admin | Get platform usage metrics |

#### Compliance & Export

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/compliance/soc2/export` | admin | Generate SOC 2 evidence package |
| `POST` | `/api/v1/compliance/gdpr/data-subject-request` | viewer+ | GDPR Article 15/20 data export |
| `POST` | `/api/v1/compliance/gdpr/erasure-request` | viewer+ | GDPR Article 17 right to erasure |
| `GET` | `/api/v1/compliance/iso27001/controls` | admin | ISO 27001 control verification status |

#### Health & Status

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `GET` | `/health/live` | none | Liveness probe (returns 200 if app running) |
| `GET` | `/health/ready` | none | Readiness probe (checks DB, Redis, LLM) |
| `GET` | `/metrics` | none | Prometheus metrics endpoint |

### Full API Documentation

- **Interactive Docs:** `http://localhost:8000/docs` (Swagger UI)
- **OpenAPI Spec:** `http://localhost:8000/openapi.json`
- **Detailed Guides:** See `docs/api/` directory

---

## Project Structure

```
enterprise-agent-platform/
├── src/                          # Python source code
│   ├── main.py                   # FastAPI app entry point + middleware
│   ├── config.py                 # Centralized settings (pydantic-settings)
│   ├── database.py               # SQLAlchemy async engine + session factory
│   │
│   ├── models/                   # SQLAlchemy ORM models
│   │   ├── user.py               # User model (tenant-scoped)
│   │   ├── conversation.py       # Conversation and Message models
│   │   ├── document.py           # Document metadata and chunks
│   │   ├── audit_log.py          # Immutable audit log
│   │   └── approval.py           # HITL approval workflow
│   │
│   ├── api/routes/               # REST API endpoints
│   │   ├── chat.py               # Chat endpoint with SSE streaming
│   │   ├── documents.py          # Document upload/list/delete
│   │   ├── admin.py              # User management, audit logs
│   │   ├── compliance.py         # SOC 2/GDPR/ISO export endpoints
│   │   └── health.py             # Health checks + metrics
│   │
│   ├── auth/                     # Authentication & authorization
│   │   ├── jwt_validator.py      # JWT signature + claims validation
│   │   ├── oidc.py               # OIDC/Keycloak integration
│   │   ├── rbac.py               # Role-based access control
│   │   └── tenant_context.py     # Tenant isolation context manager
│   │
│   ├── core/                     # Core policy & security
│   │   ├── policy_engine.py      # Data classification, PII redaction
│   │   ├── audit.py              # Audit log creation + verification
│   │   ├── rate_limiter.py       # Redis-backed rate limiting
│   │   └── prompt_injection.py   # Injection detection classifier
│   │
│   ├── agent/                    # Agent runtime & reasoning
│   │   ├── runtime.py            # Main agent execution loop
│   │   ├── reasoning.py          # OBSERVE→THINK→VERIFY loop
│   │   ├── orchestrator.py       # Multi-agent composition
│   │   ├── registry.py           # Agent type registry
│   │   │
│   │   ├── composition/          # Orchestration patterns
│   │   │   ├── patterns.py       # Pipeline, FanOut, Gate, TDDLoop
│   │   │   ├── goal_planner.py   # Task decomposition
│   │   │   └── agent_memory.py   # Conversation memory
│   │   │
│   │   ├── thinking/             # Thinking tools
│   │   │   ├── redteam.py        # Adversarial analysis (32 agents)
│   │   │   ├── first_principles.py  # Deconstruction reasoning
│   │   │   └── council.py        # Multi-agent debate (3-7 agents)
│   │   │
│   │   ├── specialists/          # Domain-specific agents
│   │   │   ├── document_analyst.py
│   │   │   ├── maintenance_advisor.py
│   │   │   ├── data_analyst.py
│   │   │   └── generalist.py
│   │   │
│   │   └── model_router/         # Tier-based model routing
│   │       ├── router.py         # Main routing logic
│   │       ├── complexity.py     # Complexity detection
│   │       ├── fallback.py       # Automatic retry with higher tier
│   │       ├── budget.py         # Token budget enforcement
│   │       └── metrics.py        # Routing decision metrics
│   │
│   ├── rag/                      # Retrieval-Augmented Generation
│   │   ├── ingestion.py          # Document chunking + embedding
│   │   ├── retrieval.py          # Hybrid search (vector + BM25)
│   │   ├── reranker.py           # Relevance reranking
│   │   ├── citations.py          # Citation extraction + formatting
│   │   └── versioning.py         # Document version management
│   │
│   ├── connectors/               # Enterprise system integrations
│   │   ├── sap/
│   │   │   ├── rfc_client.py     # SAP RFC connector
│   │   │   └── odata_client.py   # SAP OData API client
│   │   ├── mes/
│   │   │   ├── mes_client.py     # MES ODBC connector
│   │   │   └── sql_guard.py      # SQL injection prevention
│   │   └── credential_vault.py   # Encrypted credential storage
│   │
│   ├── operations/               # Write operations + HITL
│   │   ├── approval_workflow.py  # Human-in-the-loop approval
│   │   ├── sap_writer.py         # SAP write operations
│   │   ├── mes_writer.py         # MES write operations
│   │   └── rollback.py           # Operation rollback on failure
│   │
│   ├── compliance/               # Compliance frameworks
│   │   ├── standards/            # Industry compliance standards
│   │   │   ├── data_classification.py  # Data classification enforcement
│   │   │   └── export_control.py       # Export control tracking
│   │   ├── soc2/
│   │   │   └── evidence_export.py      # SOC 2 Type II evidence
│   │   ├── gdpr/
│   │   │   ├── data_subject_rights.py  # Articles 15/17/20
│   │   │   └── consent_management.py
│   │   └── iso27001/
│   │       └── control_verification.py # Annex A mapping
│   │
│   ├── infra/                    # Infrastructure & observability
│   │   ├── telemetry.py          # OpenTelemetry tracing
│   │   ├── metrics.py            # Prometheus metrics
│   │   ├── logging.py            # Structured logging setup
│   │   ├── health.py             # Health check logic
│   │   └── workers.py            # Celery worker configuration
│   │
│   ├── scale/                    # Scaling & replication
│   │   ├── replication.py        # Multi-region replication
│   │   ├── i18n.py               # Internationalization
│   │   ├── air_gap.py            # Air-gap deployment support
│   │   └── fine_tuning.py        # Model fine-tuning workflows
│   │
│   └── scripts/                  # Database & deployment scripts
│       ├── init_db.py            # Schema initialization
│       ├── seed_data.py          # Test data seeding
│       └── migrate.py            # Migration utilities
│
├── tests/                        # Test suite
│   ├── conftest.py               # Pytest fixtures
│   ├── test_tenant_isolation.py  # CRITICAL: Cross-tenant tests
│   ├── test_auth.py              # JWT + RBAC enforcement
│   ├── test_chat.py              # Chat endpoint + audit
│   ├── test_rag.py               # RAG pipeline + citations
│   ├── test_reasoning.py         # OBSERVE→THINK→VERIFY loop
│   ├── test_connectors.py        # SAP/MES integration tests
│   └── test_compliance.py        # Compliance export tests
│
├── frontend/                     # React 19 web UI
│   ├── src/
│   │   ├── components/           # React components
│   │   ├── pages/                # Page routes
│   │   ├── api/                  # API client with SSE support
│   │   └── App.tsx               # Main app component
│   ├── package.json              # Node dependencies
│   └── vite.config.ts            # Vite build config
│
├── docs/                         # Documentation
│   ├── architecture/             # Architecture diagrams + ADRs
│   ├── api/                      # API reference guides
│   ├── deployment/               # Deployment guides
│   └── roadmap/                  # Project roadmap
│
├── deploy/                       # Deployment configuration
│   └── helm/                     # Kubernetes Helm charts
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│
├── alembic/                      # Database migrations
│   ├── versions/                 # Migration files
│   └── env.py                    # Alembic configuration
│
├── docker-compose.yml            # Production stack
├── docker-compose.dev.yml        # Development with Ollama
├── Dockerfile                    # API container image
├── .env.example                  # Environment variable template
├── pyproject.toml                # Python dependencies + tooling
├── pytest.ini                    # Pytest configuration
├── README.md                     # This file
├── LICENSE                       # Proprietary license
├── SECURITY.md                   # Security policy
├── COMPLIANCE.md                 # Compliance details
└── PROJECT_STATUS.md             # Current project status
```

---

## Development

### Local Development Setup

```bash
# 1. Create Python virtual environment
python3.12 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2. Install dependencies (includes dev tools: pytest, ruff, mypy)
pip install -e ".[dev]"

# 3. Start only PostgreSQL and Redis (not the full app)
docker-compose up db redis -d

# 4. Configure database connection for local dev
export DATABASE_URL="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents"

# 5. Run database migrations
alembic upgrade head

# 6. Seed test data
python -m src.scripts.seed_data

# 7. Start API server with hot reload
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

### Running Tests

```bash
# Start test database
docker-compose up db -d

# Run all tests with coverage report
pytest --cov=src --cov-report=html

# View coverage report
open htmlcov/index.html

# Run specific test categories
pytest tests/test_tenant_isolation.py -v  # CRITICAL: Multi-tenancy tests
pytest tests/test_auth.py -v              # Authentication tests
pytest tests/test_rag.py -v               # RAG pipeline tests

# Run with detailed output
pytest -vv --tb=short
```

### Code Quality Tools

```bash
# Linting with Ruff (replaces flake8, isort, black)
ruff check src tests
ruff format src tests

# Type checking with MyPy
mypy src

# Security scanning with Bandit
bandit -r src -ll  # Only high/medium severity

# Run all quality checks
./scripts/check_quality.sh  # If available
```

### Database Migrations

```bash
# Create new migration after model changes
alembic revision --autogenerate -m "Add new table"

# Review generated migration in alembic/versions/
# Edit if necessary to ensure correctness

# Apply migration
alembic upgrade head

# Rollback one migration
alembic downgrade -1

# Show migration history
alembic history
```

### Frontend Development

```bash
cd frontend

# Install dependencies
npm install

# Start dev server with hot reload
npm run dev
# UI available at http://localhost:3000

# Build for production
npm run build

# Type checking
npm run type-check

# Linting
npm run lint
```

### Development Best Practices

1. **Test-Driven Development (TDD):**
   - Write tests FIRST (Red phase)
   - Implement minimal code to pass (Green phase)
   - Refactor while keeping tests green

2. **Tenant Isolation:**
   - ALL database queries MUST use `apply_tenant_filter()` utility
   - Never hardcode tenant IDs
   - Test cross-tenant access in `test_tenant_isolation.py`

3. **Audit Logging:**
   - Every write operation creates an audit log entry
   - Include before/after states for updates
   - Never skip audit logging

4. **Security:**
   - No hardcoded secrets - use environment variables
   - Always validate user input
   - Use parameterized SQL queries (SQLAlchemy protects by default)
   - PII must be redacted before logging

5. **Error Handling:**
   - Use structured logging with context
   - Return appropriate HTTP status codes
   - Include correlation IDs for tracing

---

## Deployment

### Docker Compose (Development)

```bash
# Start all services (API + DB + Redis + LiteLLM + Ollama)
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# View logs
docker-compose logs -f api

# Stop all services
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v
```

### Docker Compose (Production)

```bash
# Use production compose file only (no Ollama)
docker-compose up -d

# Scale API horizontally
docker-compose up -d --scale api=3

# View resource usage
docker stats
```

### Kubernetes (Helm)

Helm charts are in progress. Preliminary structure:

```bash
# Add Helm repository (when published)
helm repo add enterprise-agents https://charts.example.com

# Install with custom values
helm install enterprise-agents enterprise-agents/platform \
  --namespace production \
  --values custom-values.yaml

# Upgrade deployment
helm upgrade enterprise-agents enterprise-agents/platform \
  --namespace production \
  --values custom-values.yaml

# Rollback
helm rollback enterprise-agents 1
```

**Helm Chart Location:** `deploy/helm/`

**Deployment Documentation:** See `docs/deployment/` for detailed guides on:
- Kubernetes deployment
- Load balancer configuration
- TLS certificate management
- Database backup and restore
- Monitoring setup with Prometheus/Grafana

### Environment-Specific Configuration

| Environment | `ENVIRONMENT` | JWT | CORS | Rate Limit | Logging |
|-------------|---------------|-----|------|------------|---------|
| Development | `dev` | HS256 (dev secret) | All origins | 1000/min | DEBUG |
| Staging | `prod` | RS256 (OIDC) | Specific origins | 100/min | INFO |
| Production | `prod` | RS256 (OIDC) | Specific origins | 60/min | WARNING |

### Production Checklist

- [ ] Set `ENVIRONMENT=prod`
- [ ] Generate secure `SECRET_KEY` (256-bit random hex)
- [ ] Configure OIDC with real identity provider
- [ ] Set specific `CORS_ALLOWED_ORIGINS`
- [ ] Enable TLS/HTTPS (certificates via cert-manager or external LB)
- [ ] Configure PostgreSQL backups (daily snapshots)
- [ ] Set up monitoring (Prometheus + Grafana)
- [ ] Configure log aggregation (ELK stack or CloudWatch)
- [ ] Enable Sentry or similar error tracking
- [ ] Review and harden rate limits per tenant
- [ ] Test disaster recovery procedures
- [ ] Security scan with Trivy or similar tool
- [ ] Penetration testing (if required by compliance)

---

## Security

### Security Model

The platform implements **defense-in-depth** with multiple security layers:

```
Request → JWT Validation → RBAC Check → Tenant Filter → Classification → PII Redaction → Audit Log
```

1. **Authentication (Layer 1):**
   - JWT Bearer tokens (RS256 in production, HS256 in dev)
   - OIDC/Keycloak integration for SSO
   - Token expiration and refresh logic

2. **Authorization (Layer 2):**
   - Role-based access control (Admin, Operator, Viewer)
   - Endpoint-level permission checks
   - Resource-level access validation

3. **Tenant Isolation (Layer 3):**
   - Every database query scoped by `tenant_id`
   - Row-level security enforcement
   - No cross-tenant data leakage (validated in tests)

4. **Data Classification (Layer 4):**
   - Labels: PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED
   - Per-document classification
   - Response filtering based on user clearance

5. **PII Protection (Layer 5):**
   - Automatic redaction before logging
   - Configurable regex patterns (SSN, credit cards, emails)
   - Data subject rights (GDPR Article 15/17/20)

6. **Audit & Compliance (Layer 6):**
   - Immutable audit logs for all operations
   - Tamper detection via log chaining
   - Automated compliance evidence export

7. **Input Validation (Layer 7):**
   - Prompt injection detection
   - SQL Guard for dynamic queries
   - Pydantic validation on all API inputs

### Security Features

- **Encryption:**
  - At rest: PostgreSQL Transparent Data Encryption (TDE)
  - In transit: TLS 1.3 for all connections
  - Secrets: Encrypted credential vault for SAP/MES credentials

- **Network Security:**
  - CORS with explicit origin allowlist
  - Rate limiting per user and per tenant
  - API gateway with firewall rules

- **Application Security:**
  - No hardcoded secrets (environment variables only)
  - Parameterized SQL queries (SQLAlchemy ORM)
  - Content Security Policy headers
  - OWASP Top 10 mitigation

### Vulnerability Reporting

For security issues, see [SECURITY.md](SECURITY.md) for responsible disclosure process.

**Security Contact:** [Contact information - add your email or security team]

---

## Compliance

The platform implements comprehensive compliance controls for multiple frameworks.

### Supported Standards

| Standard | Status | Key Features |
|----------|--------|--------------|
| **Data Classification Policy** | Implemented | Data classification (PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED) |
| **Export Control Policy** | Implemented | Export control tracking (EAR/ITAR classifications) |
| **Application Security Standard** | Implemented | Secure coding, RBAC, audit logging, encryption |
| **SOC 2 Type II** | Implemented | Automated evidence export for all trust service criteria |
| **GDPR** | Implemented | Data subject rights API (Articles 15, 17, 20) |
| **ISO 27001 Annex A** | Implemented | Control mapping + automated verification (35 controls) |

### Compliance Features

1. **Automated Evidence Generation:**
   ```bash
   # Export SOC 2 evidence package
   curl -X POST http://localhost:8000/api/v1/compliance/soc2/export \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"start_date": "2025-01-01", "end_date": "2025-12-31"}'
   ```

2. **Data Subject Rights (GDPR):**
   - Article 15: Right to access personal data
   - Article 17: Right to erasure
   - Article 20: Data portability

   ```bash
   # Request data export
   curl -X POST http://localhost:8000/api/v1/compliance/gdpr/data-subject-request \
     -H "Authorization: Bearer $TOKEN"
   ```

3. **Audit Log Retention:**
   - Immutable logs for all operations
   - Tamper detection via cryptographic chaining
   - Configurable retention periods (default: 7 years)

4. **Export Control Classification:**
   - Track EAR (Export Administration Regulations) categories
   - ITAR (International Traffic in Arms Regulations) flags
   - Automatic blocking of restricted data transfer

5. **Data Classification Enforcement:**
   - Per-document and per-field classification labels
   - Access control based on user clearance level
   - Automatic downgrade/declassification workflows

### Compliance Documentation

- **Detailed Control Mapping:** See [COMPLIANCE.md](COMPLIANCE.md)
- **ISO 27001 Controls:** 35 controls across 14 domains with verification status
- **GDPR Implementation Guide:** `docs/compliance/gdpr_implementation.md`
- **SOC 2 Evidence Checklist:** `docs/compliance/soc2_evidence.md`

### Audit & Attestation

All compliance frameworks include:
- Automated control verification
- Evidence collection and packaging
- Audit log export in standard formats (JSON, CSV)
- Third-party auditor access endpoints

---

## Contributing

### Contribution Guidelines

We welcome contributions that improve the platform's security, performance, or capabilities.

1. **Follow Test-Driven Development (TDD):**
   - Write tests FIRST (Red phase)
   - Implement minimal code to pass tests (Green phase)
   - Refactor while keeping tests green

2. **Security Requirements:**
   - All database queries MUST use `apply_tenant_filter()` for tenant isolation
   - All write operations MUST create audit log entries
   - Never hardcode secrets - use environment variables
   - PII must be redacted before logging

3. **Code Quality:**
   - Pass `ruff check` and `ruff format`
   - Pass `mypy` type checking
   - Maintain or improve test coverage
   - Follow existing code structure and naming conventions

4. **Documentation:**
   - Update docstrings for new functions/classes
   - Add API documentation for new endpoints
   - Update README if adding major features

### Pull Request Process

1. **Create Feature Branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Write Tests First (Red Phase):**
   ```python
   # tests/test_your_feature.py
   def test_new_feature():
       # Test should fail initially
       assert new_feature() == expected_result
   ```

3. **Implement Feature (Green Phase):**
   ```python
   # src/module/your_feature.py
   def new_feature():
       # Minimal implementation to pass test
       return expected_result
   ```

4. **Refactor (while keeping tests green):**
   - Improve code quality
   - Add error handling
   - Optimize performance

5. **Run Quality Checks:**
   ```bash
   pytest --cov=src
   ruff check src tests
   mypy src
   ```

6. **Submit Pull Request:**
   - Clear description of changes
   - Reference related issues
   - Include test coverage report
   - Tag reviewers

### Code Review Checklist

Reviewers will check:
- [ ] Tests written before implementation (TDD)
- [ ] All tests pass
- [ ] Code coverage maintained or improved
- [ ] Tenant isolation preserved (no cross-tenant leaks)
- [ ] Audit logging for new write operations
- [ ] No hardcoded secrets
- [ ] Documentation updated
- [ ] Type hints present
- [ ] Error handling appropriate
- [ ] Security implications considered

### Development Philosophy

This project follows **principal-level engineering practices**:
- Strategic thinking over quick hacks
- Long-term maintainability over short-term speed
- Security by design, not as an afterthought
- Compliance integrated, not bolted on
- Testing as validation, not as a chore

---

## License

**Proprietary Software License**

Copyright (c) 2025-2026 Jan Střechovský (Honza). All rights reserved.

This software is licensed under a **Proprietary License** - see [LICENSE](LICENSE) file for full terms.

### Summary

- **Evaluation License:** You may evaluate this software for internal assessment purposes
- **Production Deployment:** Requires a separate Client Deployment License agreement
- **No Redistribution:** Redistribution, sublicensing, or resale prohibited without written consent
- **Attribution Required:** All deployments must maintain visible attribution to the original author

### Client Deployment

For production deployment licensing inquiries:
- Contact: [Add contact information]
- Custom licensing terms available for enterprise clients

### Open Source Components

This project uses open source dependencies (see `pyproject.toml`). All third-party licenses are preserved and respected.

---

## Roadmap

### All Phases Complete (1-9) — Production Ready

**Phases 1-6: Core Platform** ✅
- ✅ Multi-tenant architecture with row-level security
- ✅ JWT/OIDC authentication + SAML SSO + API keys
- ✅ Role-based access control (Admin/Operator/Viewer)
- ✅ Advanced RAG with pgvector + BM25 hybrid search + reranking
- ✅ Structured reasoning engine (OBSERVE→THINK→VERIFY)
- ✅ Thinking tools (RedTeam, FirstPrinciples, Council)
- ✅ Agent orchestration (Pipeline, FanOut, Gate, TDDLoop)
- ✅ SAP/MES connectors with SQL Guard
- ✅ Human-in-the-loop approval workflow with MFA
- ✅ 3-tier model routing (LIGHT/STANDARD/HEAVY)
- ✅ Compliance frameworks (SOC 2, GDPR, ISO 27001)
- ✅ React 19 web UI with SSE streaming
- ✅ Air-gap deployment with Ollama
- ✅ Kubernetes Helm charts (17 templates)
- ✅ Plugin/Extension SDK with sandbox
- ✅ Analytics dashboard with metrics API

**Phase 7: Production Readiness** ✅
- ✅ CI/CD pipeline (GitHub Actions: lint, typecheck, test, security scan, Docker build)
- ✅ In-memory stores persisted to PostgreSQL (budgets, metrics, plans, operations)
- ✅ Write execution wired to real SAP/MES connectors
- ✅ Real SMTP + webhook notifications
- ✅ Integration tests with real database
- ✅ Security scanning (Bandit, pip-audit, Trivy, TruffleHog)

**Phase 8: Enterprise Polish** ✅
- ✅ Grafana monitoring dashboards (LLM Performance, Agent Ops, Tenant Budgets)
- ✅ Prometheus metrics + Loki log aggregation
- ✅ Deployment runbook (1,185 lines) + architecture docs (625 lines)
- ✅ Load testing suite (Locust + k6)
- ✅ PostgreSQL backup/restore automation with verification

**Phase 9: Critical Fixes** ✅
- ✅ Broken imports fixed (tests unblocked)
- ✅ Real MFA/TOTP validation (pyotp)
- ✅ ConnectorRegistry wired to global executor (19 new tests)
- ✅ Escalation notifications (email + webhook)
- ✅ Write operations persisted to PostgreSQL

**Phase 10: Feature Completion** (in progress)
- ⬜ Fine-tuning job queue
- ⬜ Compliance dashboard real data
- ⬜ Analytics endpoints
- ⬜ Test suite green (100%)
- ⬜ Code cleanup

**Phase 11: State of the Art** (planned)
- ⬜ Streaming reasoning UI
- ⬜ Agent evaluation framework
- ⬜ Conversation analytics dashboards

---

## Support & Documentation

### Documentation

- **Architecture:** `docs/architecture/` - System design and ADRs
- **API Reference:** `docs/api/` - Detailed endpoint documentation
- **Deployment:** `docs/deployment/` - Kubernetes, Docker, production guides
- **Compliance:** [COMPLIANCE.md](COMPLIANCE.md) - Control mapping and evidence
- **Security:** [SECURITY.md](SECURITY.md) - Security model and disclosure process

### Getting Help

- **Issues:** GitHub Issues for bug reports and feature requests
- **Questions:** Discussions tab for general questions
- **Internal Support:** Contact the deployment organization's platform team

### Project Status

Current status: **Phases 1-9 complete** (207 src files, 81 test files, 14 migrations). Phase 10 in progress.

See [PROJECT_STATUS.md](PROJECT_STATUS.md) for detailed progress tracking.

---

## Acknowledgments

Built using the **PAI Algorithm** and **Sonnet+Opus pooling pattern**:
- ~20 Claude Sonnet 4.5 agents for implementation
- 2 Claude Opus 4 agents for architecture and security review
- Total build time: ~4 hours across 2 development sessions

**Key Technologies:**
- FastAPI, SQLAlchemy, Pydantic
- LiteLLM, LlamaIndex, Ollama
- PostgreSQL + pgvector
- React 19, TypeScript, TailwindCSS

**Compliance Frameworks:**
- Enterprise data classification, export control, application security standards
- SOC 2 Type II Trust Service Criteria
- GDPR Articles 15, 17, 20
- ISO/IEC 27001:2022 Annex A

---

**Built for Enterprise Manufacturing Operations | On-Premise Deployment | Zero Data Egress**
