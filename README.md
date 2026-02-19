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
5. **Air-Gap Ready** - Fully operational without internet access using Ollama/vLLM for on-premise LLM deployment
6. **Compliance by Design** - Automated evidence generation for SOC 2, GDPR Article 15/17/20, ISO 27001
7. **Intelligence Loop** - Agent memory injection, feedback-driven learning, and auto-composition for smarter responses over time
8. **Persistent User Goals** - Goals span conversations; the platform tracks progress across sessions

Developed with AI-assisted engineering. Security-audited with comprehensive automated testing (1235+ tests).

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
  - Auto-composition: complexity classifier selects the right pattern automatically
  - Specialist agents: Document Analyst, Maintenance Advisor, Data Analyst, Quality Inspector, Generalist
  - Model routing with 3-tier complexity detection (LIGHT/STANDARD/HEAVY)
  - Automatic fallback and escalation on model failures

- **Intelligence Loop (Phase 11)**
  - Agent memory injected into system prompts — agents know the user's history and preferences
  - Feedback-driven learning: thumbs up/down updates memory and influences future RAG retrieval
  - Post-response LEARN step: extracts lessons and stores as memory
  - Persistent user goals spanning multiple conversations
  - Auto-composition: SIMPLE → direct agent, DEEP → Pipeline, MULTI-PERSPECTIVE → FanOut, QUALITY-CRITICAL → Gate

- **Enterprise-Grade RAG**
  - Hybrid search combining vector similarity (pgvector) and BM25 keyword matching
  - Reranking pipeline for relevance optimization
  - Feedback-weighted retrieval: positive-rated sources boosted, negative-rated deprioritized
  - Document versioning and citation tracking
  - Multi-format ingestion: PDF, DOCX, TXT, Markdown

- **Enterprise System Integration**
  - **SAP:** RFC and OData connectors with credential vault integration
  - **MES Systems:** ODBC/pyodbc with SQL Guard (query validation and sanitization)
  - **Human-in-the-Loop (HITL):** Approval workflow for all write operations with rollback capability
  - **Async Execution:** Background jobs for long-running operations

- **Model Economy & Routing**
  - 3-tier routing: 7B models (LIGHT), 32B models (STANDARD), 72B models (HEAVY)
  - Automatic model escalation on failure (7B → 32B → 72B)
  - Per-tenant token budgets with overflow protection
  - LiteLLM proxy for multi-provider support (OpenAI, Anthropic, Azure, vLLM)
  - Ollama integration for on-premise deployment (air-gap environments)

- **Security & Compliance**
  - JWT authentication (RS256/HS256) with OIDC/Keycloak integration
  - SAML 2.0 SSO support
  - Role-based access control (RBAC): Admin, Operator, Viewer
  - API key authentication for machine-to-machine access
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
  - Grafana dashboards: LLM Performance, Agent Operations, Tenant Budgets, Overview
  - Loki + Promtail log aggregation
  - Health checks: `/health/live`, `/health/ready`

- **Internationalization & Scale**
  - Multi-region replication support (active-passive, active-active)
  - i18n framework for multi-language deployments
  - Fine-tuning workflows for domain adaptation
  - Read replica routing for read-heavy workloads
  - Edge deployment support for network-isolated sites

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
│   Auto-Composition: Pipeline │ FanOut │ Gate │ TDDLoop          │
│   Token Budgets │ Complexity Detection │ Fallback + Escalation  │
└──────────────────────────────────────────────────────────────────┘
                            │
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 3: REASONING ENGINE                                        │
│   OBSERVE → THINK → VERIFY → LEARN Loop                        │
│   Thinking Tools: RedTeam │ FirstPrinciples │ Council           │
│   Specialist Agents: DocumentAnalyst │ MaintenanceAdvisor │ ... │
│   Memory Injection │ Feedback-Weighted Retrieval                │
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
│   LiteLLM (multi-provider) │ vLLM/Ollama (on-prem)              │
│   PostgreSQL 16 + pgvector │ Redis (rate limit/cache)            │
└──────────────────────────────────────────────────────────────────┘
```

### Key Architectural Principles

- **Defense in Depth:** Multiple security layers (JWT → RBAC → Classification → PII Redaction → Audit)
- **Tenant Isolation:** Every query includes `WHERE tenant_id = ?` via `apply_tenant_filter()`
- **Immutable Audit:** All operations logged to `audit_logs` table with tamper detection
- **HITL Gate:** All write operations require human approval before execution
- **Zero Trust:** No operation trusts prior validation; every layer re-validates tenant/user/permissions
- **Intelligence Loop:** Memory → Reasoning → Response → Learn → Memory (gets smarter per user)

---

## Quick Start

### Prerequisites

- **Docker** 24.0+ and **Docker Compose** 2.20+
- **Python** 3.12+ (for local development outside Docker)
- **Git** (to clone repository)

### 5-Command Setup

```bash
# 1. Clone repository
git clone https://github.com/honzastrechovsky-dotcom/Enterprise-Architecture.git
cd Enterprise-Architecture

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
| `JWKS_LOCAL_PATH` | Offline JWKS for air-gap | `/config/jwks.json` |
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
- **Air-Gap:** Set `OLLAMA_BASE_URL` and use `litellm_config.prod.yaml` with vLLM-only endpoints. Use `JWKS_LOCAL_PATH` for offline JWT validation
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

#### Memory & Goals

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `GET` | `/api/v1/memory` | viewer+ | Recall agent memories for current user |
| `GET` | `/api/v1/goals` | viewer+ | List user's persistent goals |
| `POST` | `/api/v1/goals` | viewer+ | Create a new persistent goal |
| `PATCH` | `/api/v1/goals/{id}` | viewer+ | Update goal progress |

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

#### Feedback & Analytics

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `POST` | `/api/v1/feedback` | viewer+ | Submit thumbs up/down with optional comment |
| `GET` | `/api/v1/analytics/metrics` | admin | Platform-wide usage metrics |
| `GET` | `/api/v1/analytics/costs` | admin | Token usage and cost tracking |

#### Health & Status

| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| `GET` | `/health/live` | none | Liveness probe (returns 200 if app running) |
| `GET` | `/health/ready` | none | Readiness probe (checks DB, Redis, LLM) |
| `GET` | `/metrics` | none | Prometheus metrics endpoint |

### Full API Documentation

- **Interactive Docs:** `http://localhost:8000/docs` (Swagger UI)
- **OpenAPI Spec:** `http://localhost:8000/openapi.json`

---

## Project Structure

```
Enterprise-Architecture/
├── src/                              # Python source (215 files, ~63K lines)
│   ├── main.py                       # FastAPI app entry point + middleware
│   ├── config.py                     # Centralized settings (pydantic-settings)
│   ├── database.py                   # SQLAlchemy async engine + session factory
│   │
│   ├── models/                       # SQLAlchemy ORM models (20 models)
│   │   ├── user.py                   # User model (tenant-scoped)
│   │   ├── conversation.py           # Conversation + Message
│   │   ├── document.py               # Document metadata + chunks
│   │   ├── audit.py                  # Immutable audit log
│   │   ├── feedback.py               # User feedback (thumbs up/down)
│   │   ├── agent_memory.py           # Agent memory (FACT/PREFERENCE/SKILL/CONTEXT/EPISODIC)
│   │   ├── user_goal.py              # Persistent user goals
│   │   ├── write_operation.py        # HITL write operation records
│   │   ├── fine_tuning.py            # Fine-tuning job records
│   │   ├── gdpr_request.py           # GDPR data subject requests
│   │   └── ...                       # tenant, plugin, webhook, api_key, etc.
│   │
│   ├── api/                          # REST API endpoints
│   │   ├── chat.py                   # Chat with SSE streaming
│   │   ├── conversations.py          # Conversation CRUD
│   │   ├── documents.py              # Document upload/list/delete
│   │   ├── admin.py                  # User management, audit logs
│   │   ├── compliance.py             # SOC 2/GDPR/ISO export endpoints
│   │   ├── compliance_admin.py       # Compliance dashboard data
│   │   ├── analytics.py              # Usage metrics + cost tracking
│   │   ├── feedback.py               # Feedback submission + export
│   │   ├── goals.py                  # Persistent goals API
│   │   ├── memory.py                 # Agent memory API
│   │   ├── health.py                 # Health checks + Prometheus metrics
│   │   ├── keys.py                   # API key management
│   │   ├── plugins.py                # Plugin management
│   │   ├── sso.py                    # SSO/OIDC endpoints
│   │   ├── webhooks.py               # Webhook configuration
│   │   └── routes/                   # Additional route modules
│   │       ├── operations.py         # Write operations API
│   │       └── spaces.py             # Shared spaces
│   │
│   ├── auth/                         # Authentication & authorization
│   │   ├── dependencies.py           # JWT validation + tenant extraction
│   │   ├── middleware.py             # Auth middleware (JWT Bearer)
│   │   ├── oidc.py                   # OIDC/Keycloak integration
│   │   ├── saml.py                   # SAML 2.0 SSO
│   │   └── api_key_auth.py           # API key authentication
│   │
│   ├── core/                         # Core policy & security
│   │   ├── pii.py                    # PII redaction engine
│   │   ├── audit.py                  # Audit log creation + verification
│   │   ├── security.py               # Security utilities
│   │   ├── classification.py         # Data classification enforcement
│   │   ├── input_validation.py       # Input sanitization
│   │   ├── rate_limit.py             # Redis-backed rate limiting
│   │   ├── policy.py                 # Policy engine
│   │   ├── disclosure.py             # AI disclosure requirements
│   │   └── export_control.py         # EAR/ITAR export control
│   │
│   ├── agent/                        # Agent runtime & reasoning
│   │   ├── runtime.py                # Main agent execution loop + memory injection
│   │   ├── reasoning.py              # OBSERVE→THINK→VERIFY→LEARN loop
│   │   ├── orchestrator.py           # Multi-agent composition + auto-selection
│   │   ├── registry.py               # Agent type registry
│   │   ├── llm.py                    # LLM client wrapper
│   │   ├── tools.py                  # Agent tool definitions
│   │   │
│   │   ├── composition/              # Orchestration patterns
│   │   │   ├── patterns.py           # Pipeline, FanOut, Gate, TDDLoop
│   │   │   ├── goal_planner.py       # DAG decomposition + persistent goals
│   │   │   └── agent_memory.py       # Memory injection + recall
│   │   │
│   │   ├── thinking/                 # Thinking tools
│   │   │   ├── red_team.py           # Adversarial analysis
│   │   │   ├── first_principles.py   # Deconstruction reasoning
│   │   │   └── council.py            # Multi-agent debate
│   │   │
│   │   ├── specialists/              # Domain-specific agents
│   │   │   ├── document_analyst.py
│   │   │   ├── maintenance_advisor.py
│   │   │   ├── data_analyst.py
│   │   │   ├── quality_inspector.py
│   │   │   ├── procedure_expert.py
│   │   │   └── generalist.py
│   │   │
│   │   └── model_router/             # Tier-based model routing
│   │       ├── router.py             # Main routing logic
│   │       ├── complexity.py         # Complexity detection + auto-escalation
│   │       ├── fallback.py           # Automatic retry with higher tier
│   │       ├── budget.py             # Token budget enforcement
│   │       └── metrics.py            # Routing decision metrics
│   │
│   ├── reasoning/                    # Advanced reasoning strategies
│   │   └── strategies/
│   │       ├── chain_of_thought.py   # Standard CoT
│   │       ├── tree_of_thought.py    # Tree-based exploration
│   │       ├── self_consistency.py   # Multiple paths + majority vote
│   │       └── rar.py                # Retrieval-Augmented Reasoning
│   │
│   ├── rag/                          # Retrieval-Augmented Generation
│   │   ├── ingest.py                 # Document chunking + embedding
│   │   ├── retrieve.py               # Hybrid search (vector + BM25)
│   │   ├── hybrid_search.py          # Search implementation
│   │   ├── reranker.py               # Relevance reranking
│   │   ├── citations.py              # Citation extraction + formatting
│   │   ├── versioning.py             # Document version management
│   │   ├── metadata_filter.py        # Metadata-based filtering
│   │   └── conversation_memory.py    # Conversation context for RAG
│   │
│   ├── connectors/                   # Enterprise system integrations
│   │   ├── sap.py                    # SAP RFC/OData connector
│   │   ├── mes.py                    # MES ODBC connector
│   │   ├── sql_guard.py              # SQL injection prevention
│   │   ├── approval.py               # HITL approval workflow
│   │   ├── base.py                   # Connector base class + registry
│   │   └── cache.py                  # Connector response caching
│   │
│   ├── operations/                   # Write operations + HITL
│   │   ├── write_framework.py        # Approval workflow (PROPOSED→APPROVED→EXECUTED)
│   │   ├── sap_writer.py             # SAP write operations
│   │   ├── mes_writer.py             # MES write operations
│   │   ├── escalation.py             # Timeout escalation logic
│   │   └── notification.py           # Email + webhook notifications
│   │
│   ├── compliance/                   # Compliance frameworks
│   │   ├── audit_export.py           # SOC 2 Type II evidence export
│   │   ├── gdpr.py                   # GDPR Articles 15/17/20
│   │   ├── iso27001.py               # ISO 27001 Annex A control mapping
│   │   ├── dashboard.py              # Compliance dashboard data
│   │   ├── evidence.py               # Evidence collection
│   │   ├── monitor.py                # Continuous compliance monitoring
│   │   ├── scheduler.py              # Automated compliance checks
│   │   └── testing.py                # Compliance test utilities
│   │
│   ├── services/                     # Business logic services
│   │   ├── conversation.py           # Conversation management
│   │   ├── memory.py                 # Memory service (5 types, semantic search, decay)
│   │   ├── feedback.py               # Feedback → memory pipeline
│   │   ├── finetuning.py             # Fine-tuning job management
│   │   ├── goal_service.py           # Persistent goal tracking
│   │   ├── analytics.py              # Analytics aggregation
│   │   ├── api_keys.py               # API key lifecycle
│   │   ├── ingestion.py              # Document ingestion pipeline
│   │   └── webhook.py                # Webhook delivery
│   │
│   ├── infra/                        # Infrastructure & observability
│   │   ├── telemetry.py              # OpenTelemetry tracing
│   │   ├── streaming.py              # SSE streaming utilities
│   │   ├── health.py                 # Health check logic
│   │   └── background_worker.py      # Background job execution
│   │
│   ├── scale/                        # Scaling & deployment
│   │   ├── air_gap.py                # Air-gap deployment support
│   │   ├── fine_tuning.py            # Model fine-tuning workflows
│   │   ├── i18n.py                   # Internationalization
│   │   ├── replication.py            # Read replica routing
│   │   └── shared_spaces.py          # Cross-tenant shared spaces
│   │
│   ├── multiregion/                  # Multi-region support
│   │   ├── routing.py                # Region-aware request routing
│   │   ├── replication.py            # Data replication
│   │   └── failover.py               # Automatic failover
│   │
│   ├── websocket/                    # WebSocket support
│   │   ├── manager.py                # Connection management
│   │   ├── chat.py                   # Real-time chat
│   │   └── events.py                 # Event broadcasting
│   │
│   ├── plugins/                      # Plugin SDK
│   │   ├── base.py                   # Plugin base class
│   │   ├── registry.py               # Plugin registry (tenant-scoped)
│   │   ├── loader.py                 # Dynamic plugin loading
│   │   ├── tool_plugin.py            # Tool plugin interface
│   │   └── hot_reload.py             # Hot-reload support
│   │
│   ├── skills/                       # Built-in skills
│   │   ├── registry.py               # Skill registry
│   │   └── builtin/                  # 4 built-in skills
│   │       ├── document_analysis.py
│   │       ├── procedure_lookup.py
│   │       ├── report_generation.py
│   │       └── calculations.py
│   │
│   ├── sdk/                          # Client SDK generator
│   │   ├── generator.py              # Multi-language SDK generation
│   │   └── templates/                # Python, TypeScript, Go templates
│   │
│   └── scripts/                      # Database & deployment scripts
│       ├── init_db.py                # Schema initialization
│       └── seed_data.py              # Test data seeding
│
├── tests/                            # Test suite (92 files, ~31K lines)
│   ├── conftest.py                   # Pytest fixtures
│   ├── agent/                        # Agent runtime + reasoning tests
│   ├── api/                          # API endpoint tests
│   ├── auth/                         # Authentication tests
│   ├── compliance/                   # Compliance framework tests
│   ├── connectors/                   # SAP/MES connector tests
│   ├── core/                         # Security + PII tests
│   ├── infra/                        # Health check tests
│   ├── integration/                  # Full-stack integration tests
│   ├── load/                         # Locust + k6 load tests
│   ├── models/                       # ORM model tests
│   ├── operations/                   # Write operation tests
│   ├── plugins/                      # Plugin SDK tests
│   ├── rag/                          # RAG pipeline tests
│   └── services/                     # Service layer tests
│
├── frontend/                         # React 19 web UI (23 TS/TSX files)
│   ├── src/
│   │   ├── App.tsx                   # Main app + routing
│   │   ├── components/               # ChatMessage, DocumentUpload, Sidebar, shadcn/ui
│   │   ├── pages/                    # Chat, Agents, Documents, Admin, Login
│   │   └── lib/                      # API client (SSE), auth, utils
│   ├── package.json
│   └── vite.config.ts
│
├── docs/                             # Documentation
│   ├── ARCHITECTURE.md               # System architecture (625 lines)
│   ├── RUNBOOK.md                    # Deployment runbook (1,185 lines)
│   ├── PAI_GAP_ANALYSIS.md           # Algorithm gap analysis
│   ├── PLUGIN_SDK.md                 # Plugin developer guide
│   ├── roadmap/ROADMAP.md            # Unified project roadmap
│   └── ...                           # Phase docs, observability guides
│
├── deploy/                           # Deployment configuration
│   ├── helm/                         # Kubernetes Helm charts (18 files)
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   ├── values-multiregion.yaml
│   │   └── templates/                # API, frontend, LiteLLM, vLLM, worker, HPA, PDB, NetworkPolicy
│   ├── grafana/                      # Monitoring stack
│   │   ├── dashboards/               # 4 Grafana dashboards (LLM, Agents, Budgets, Overview)
│   │   ├── prometheus.yml
│   │   └── docker-compose.monitoring.yml
│   ├── logging/                      # Log aggregation
│   │   ├── loki-config.yml
│   │   ├── promtail-config.yml
│   │   └── docker-compose.logging.yml
│   └── edge/                         # Edge deployment
│       ├── Dockerfile.edge
│       └── docker-compose.edge.yml
│
├── alembic/                          # Database migrations (18 migrations)
│   ├── versions/                     # 001–018: conversations → foreign keys
│   └── env.py
│
├── scripts/                          # Operational scripts
│   ├── backup/                       # pg_dump/restore automation
│   ├── build_offline.sh              # Air-gap Docker build
│   ├── dev-start.sh                  # Dev environment launcher
│   └── db-maintenance.py             # Database maintenance
│
├── tools/                            # Developer tools
│   └── eap-cli/                      # CLI for plugin scaffolding
│
├── docker-compose.yml                # Production stack
├── docker-compose.dev.yml            # Development with Ollama
├── Dockerfile                        # API container image
├── litellm_config.yaml               # LiteLLM dev config
├── litellm_config.prod.yaml          # LiteLLM production (vLLM-only, air-gap)
├── .env.example                      # Environment variable template
├── pyproject.toml                    # Python dependencies + ruff/mypy/pytest config
├── README.md                         # This file
├── LICENSE                           # Proprietary license
├── SECURITY.md                       # Security policy + disclosure
├── COMPLIANCE.md                     # Compliance control mapping
├── PROJECT_STATUS.md                 # Detailed phase completion status
└── NOTICE.md                         # Third-party licenses
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
pytest tests/rag/ -v                      # RAG pipeline tests
pytest tests/integration/ -v              # Full integration tests

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

# Dependency audit
pip-audit
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

```bash
# Install with custom values
helm install enterprise-agents deploy/helm/enterprise-agent-platform \
  --namespace production \
  --values custom-values.yaml

# Upgrade deployment
helm upgrade enterprise-agents deploy/helm/enterprise-agent-platform \
  --namespace production \
  --values custom-values.yaml

# Rollback
helm rollback enterprise-agents 1

# Validate chart before deploy
bash deploy/helm/validate-chart.sh
```

**Helm Chart:** `deploy/helm/enterprise-agent-platform/` — 18 templates including API, frontend, LiteLLM, vLLM, worker deployments, HPA, PDB, NetworkPolicy, Ingress.

**Deployment Documentation:** See `deploy/helm/DEPLOYMENT_GUIDE.md` and `deploy/helm/QUICKSTART.md` for detailed guides.

### Air-Gap Deployment

```bash
# Build Docker images with pre-cached dependencies
bash scripts/build_offline.sh

# Use production LiteLLM config (vLLM-only, no cloud APIs)
cp litellm_config.prod.yaml litellm_config.yaml

# Configure offline JWKS for JWT validation
export JWKS_LOCAL_PATH="/config/jwks.json"
```

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
- [ ] Configure PostgreSQL backups (daily snapshots via `scripts/backup/`)
- [ ] Set up monitoring (Prometheus + Grafana via `deploy/grafana/`)
- [ ] Configure log aggregation (Loki + Promtail via `deploy/logging/`)
- [ ] Enable Sentry or similar error tracking
- [ ] Review and harden rate limits per tenant
- [ ] Test disaster recovery procedures
- [ ] Security scan with Trivy
- [ ] Use `litellm_config.prod.yaml` (vLLM-only endpoints)
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
   - SAML 2.0 support for legacy IdPs
   - API key authentication for machine-to-machine
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

### Security Audit

**Opus 4.6 Security Audit** — 20 issues identified and resolved:
- 5 CRITICAL (auth bypass, SQL injection vectors, scope enforcement)
- 8 HIGH (missing tenant checks, PII exposure in logs)
- 7 MEDIUM (CORS misconfiguration, rate limit gaps)

All issues resolved in dedicated security migration (018_add_missing_foreign_keys.py) and code fixes.

### Security Features

- **Encryption:**
  - At rest: PostgreSQL Transparent Data Encryption (TDE)
  - In transit: TLS 1.3 for all connections
  - Secrets: Encrypted credential vault for SAP/MES credentials

- **Network Security:**
  - CORS with explicit origin allowlist
  - Rate limiting per user and per tenant
  - Kubernetes NetworkPolicy for pod isolation

- **Application Security:**
  - No hardcoded secrets (environment variables only)
  - Parameterized SQL queries (SQLAlchemy ORM)
  - Content Security Policy headers
  - OWASP Top 10 mitigation

- **CI/CD Security Pipeline:**
  - Bandit (SAST)
  - pip-audit (dependency scanning)
  - Trivy (container scanning)
  - TruffleHog (secret detection)

### Vulnerability Reporting

For security issues, see [SECURITY.md](SECURITY.md) for responsible disclosure process.

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
| **GDPR** | Implemented | Data subject rights API (Articles 15, 17, 20) with request persistence |
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
   - All requests persisted in `gdpr_requests` table (migration 016)

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
- Contact: honza.strechovsky@gmail.com
- Custom licensing terms available for enterprise clients

### Open Source Components

This project uses open source dependencies (see `pyproject.toml`). All third-party licenses are preserved and respected. See [NOTICE.md](NOTICE.md) for details.

---

## Roadmap

### Phases 1–11 Complete — Production Ready

**Phases 1–6: Core Platform** ✅
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
- ✅ Kubernetes Helm charts (18 templates)
- ✅ Plugin/Extension SDK with sandbox
- ✅ Analytics dashboard with metrics API
- ✅ Feedback loop (thumbs up/down → fine-tuning dataset)

**Phase 7: Production Readiness** ✅
- ✅ CI/CD pipeline (GitHub Actions: lint, typecheck, test, security scan, Docker build)
- ✅ In-memory stores persisted to PostgreSQL (budgets, metrics, plans, operations)
- ✅ Write execution wired to real SAP/MES connectors
- ✅ Real SMTP + webhook notifications
- ✅ Integration tests with real database
- ✅ Security scanning (Bandit, pip-audit, Trivy, TruffleHog)

**Phase 8: Enterprise Polish** ✅
- ✅ Grafana monitoring dashboards (LLM Performance, Agent Ops, Tenant Budgets, Overview)
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

**Phase 10: Feature Completion** ✅
- ✅ Fine-tuning job queue (PersistentFineTuningManager, migration 015, 25 tests)
- ✅ Compliance dashboard real data (DB queries for all compliance metrics)
- ✅ Test suite fixes (auth dependency tests updated)
- ✅ Code cleanup (80+ phase comments, TODOs removed across 60+ files)

**Phase 11: Intelligence Layer** ✅
- ✅ Air-gapped production config (`litellm_config.prod.yaml`, offline JWKS, `build_offline.sh`)
- ✅ Memory injection into agent runtime (agents recall user history + preferences)
- ✅ Learning loop (feedback → memory → improved responses)
- ✅ Auto-composition selection (complexity classifier → auto Pipeline/FanOut/Gate)
- ✅ Persistent user goals (migration 017, GoalService, Goals API)
- ✅ GDPR request persistence (migration 016)
- ✅ Missing foreign keys added (migration 018)

**Security Audit** ✅
- ✅ Opus 4.6 comprehensive audit: 20 issues found and resolved
- ✅ 5 CRITICAL, 8 HIGH, 7 MEDIUM — all fixed with dedicated migration + code changes

### Phase 12: Customer-Driven Expansion (Planned)

Features built only when requested by actual customer deployments:

- **Proactive Monitoring** — Scheduled MES polling, configurable alert thresholds, auto root-cause analysis
- **Thinking Tools in Main Flow** — Council/FirstPrinciples for complex queries (opt-OUT, not opt-IN)
- **Agent Quality Evaluation** — Golden dataset + automated scoring for answer quality
- **Multi-Modal Support** — Vision model for quality inspection images, OCR pipeline
- **Tenant Admin Portal** — Self-service user/role/API key management
- **Multi-Site Support** — Edge agents for network-isolated plant locations

---

## Support & Documentation

### Documentation

- **Architecture:** `docs/ARCHITECTURE.md` - System design (625 lines)
- **Runbook:** `docs/RUNBOOK.md` - Deployment & operations (1,185 lines)
- **Plugin SDK:** `docs/PLUGIN_SDK.md` - Plugin developer guide
- **Compliance:** [COMPLIANCE.md](COMPLIANCE.md) - Control mapping and evidence
- **Security:** [SECURITY.md](SECURITY.md) - Security model and disclosure process
- **Project Status:** [PROJECT_STATUS.md](PROJECT_STATUS.md) - Detailed phase tracking
- **Roadmap:** `docs/roadmap/ROADMAP.md` - Full roadmap with future phases

### Getting Help

- **Issues:** GitHub Issues for bug reports and feature requests
- **Questions:** Discussions tab for general questions
- **Internal Support:** Contact the deployment organization's platform team

### Project Scale

**215** Python source files (~63K lines) | **92** test files (~31K lines) | **18** Alembic migrations | **23** frontend files | **18** Helm templates | **4** Grafana dashboards

1235 tests passed, 51 skipped, 0 failures. Security audited by Claude Opus 4.6.

---

## Acknowledgments

Built using the **PAI Algorithm** and **Sonnet+Opus pooling pattern**:
- ~35 Claude Sonnet 4.5 agents for implementation
- 4 Claude Opus 4.6 agents for architecture and security review
- Total build time: ~6 hours across 3 development sessions

**Key Technologies:**
- FastAPI, SQLAlchemy, Pydantic
- LiteLLM, LlamaIndex, Ollama
- PostgreSQL 16 + pgvector
- React 19, TypeScript, TailwindCSS
- Prometheus, Grafana, Loki

**Compliance Frameworks:**
- Enterprise data classification, export control, application security standards
- SOC 2 Type II Trust Service Criteria
- GDPR Articles 15, 17, 20
- ISO/IEC 27001:2022 Annex A

---

**Built for Enterprise Manufacturing Operations | On-Premise Deployment | Zero Data Egress**
