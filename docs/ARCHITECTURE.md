# Enterprise Agent Platform — System Architecture

**Platform:** Enterprise Agent Platform v1.0.0
**Customer:** Enterprise client
**Last updated:** 2026-02-17

---

## Table of Contents

1. [Overview](#1-overview)
2. [High-level Architecture Diagram](#2-high-level-architecture-diagram)
3. [Component Descriptions](#3-component-descriptions)
4. [Request Flow Diagrams](#4-request-flow-diagrams)
5. [Data Architecture](#5-data-architecture)
6. [Security Architecture](#6-security-architecture)
7. [Deployment Topology](#7-deployment-topology)
8. [Technology Decisions](#8-technology-decisions)

---

## 1. Overview

The Enterprise Agent Platform is a multi-tenant AI agent orchestration system built for enterprise clients. It provides:

- Authenticated, isolated workspaces per tenant
- Retrieval-Augmented Generation (RAG) over enterprise documents
- Multi-model LLM routing (cloud and on-premise)
- Full audit logging and compliance evidence generation
- Human-in-the-loop (HITL) approval workflows for write operations
- Real-time streaming chat via WebSocket
- Plugin-extensible agent tooling
- Prometheus metrics and OpenTelemetry distributed tracing

The system is designed to run on-premise on Kubernetes and supports multi-region deployment for data residency requirements (EU GDPR, APAC sovereignty).

---

## 2. High-level Architecture Diagram

```
                              Enterprise Users
                                     |
                         +-----------+-----------+
                         |                       |
                   HTTPS / WSS               HTTPS
                         |                       |
              +----------v----------+   +--------v--------+
              |   React 19 Frontend |   |  External OIDC  |
              |   (Vite / Tailwind) |   |  (Keycloak /    |
              |   Port 5173 (dev)   |   |   Azure AD)     |
              +----------+----------+   +--------+--------+
                         |                       |
                   HTTPS REST + WSS         JWT JWKS
                         |                       |
              +----------v-----------+           |
              |   nginx Ingress      |           |
              |   (Kubernetes)       |           |
              +----------+-----------+           |
                         |                       |
              +----------v-----------+           |
              |   FastAPI API Server |<----------+
              |   (Python 3.12)      |  JWKS validation
              |                      |
              |  Middleware stack:   |
              |  - RequestId         |
              |  - Auth (JWT/OIDC)   |
              |  - RateLimit (Redis) |
              |  - SecurityHeaders   |
              |  - CORS              |
              |  - Tracing (OTEL)    |
              |  - Prometheus        |
              |                      |
              |  API routes /api/v1: |
              |  /chat  /documents   |
              |  /conversations      |
              |  /compliance  /admin |
              |  /plans /plugins     |
              |  /analytics /memory  |
              |  /keys  /sso         |
              |  /webhooks /spaces   |
              |  WS /ws/chat/{id}    |
              +---+------+------+----+
                  |      |      |
          +-------+  +---+  +--+--------+
          |           |          |
 +--------v--+  +-----v---+  +--v---------+
 | PostgreSQL|  |  Redis 7 |  | Background |
 | 16 +      |  |          |  | Worker Pool|
 | pgvector  |  | - Rate   |  | (asyncio)  |
 |           |  |   limits |  |            |
 | - Tenants |  | - Cache  |  | - Ingestion|
 | - Users   |  | - Session|  | - Embedding|
 | - Convos  |  |          |  | - Metrics  |
 | - Docs    |  +----------+  | - Plans    |
 | - Vectors |                +-----+------+
 | - Audit   |                      |
 | - Budget  |                      |
 | - Metrics |          +-----------v-----------+
 | - Plugins |          |   LiteLLM Proxy        |
 | - Plans   |          |   (Port 4000)          |
 +-----------+          |                        |
                        |  Model registry:       |
                        |  - openai/gpt-4o-mini  |
                        |  - anthropic/claude    |
                        |  - ollama/qwen2.5:7b   |
                        |  - ollama/qwen2.5:32b  |
                        |  - vllm/qwen2.5:72b    |
                        +---+--------+--------+--+
                            |        |        |
               +------------+  +-----+  +-----+-------+
               |               |              |
        +------v-----+  +------v------+  +----v-------+
        | OpenAI API |  |  Ollama     |  |  vLLM      |
        | Anthropic  |  |  (Port      |  |  (Port     |
        | (Cloud)    |  |  11434)     |  |  8080)     |
        |            |  |             |  |            |
        | gpt-4o     |  | qwen2.5:7b  |  |qwen2.5:72b|
        | claude-3.5 |  | qwen2.5:32b |  | GPU x2    |
        +------------+  +-------------+  +------------+

  Observability (optional):
  +-------------------------+
  | OpenTelemetry Collector |
  | OTLP (Port 4317)        |
  | -> Jaeger / Tempo       |
  +-------------------------+

  +-------------------------+
  | Prometheus              |
  | Scrapes /metrics        |
  | -> Grafana dashboards   |
  +-------------------------+
```

---

## 3. Component Descriptions

### 3.1 React 19 Frontend

**Repository path:** `frontend/`
**Technology:** React 19, TypeScript, Vite, Tailwind CSS, shadcn/ui
**Port:** 5173 (dev server), served as static files via nginx in production

The frontend is a single-page application providing:

- Tenant-scoped chat interface with Server-Sent Events (SSE) streaming
- Document management and knowledge base browsing
- Agent playground for testing and prompt engineering
- Compliance dashboard and audit log viewer
- Tenant administration portal
- Plugin management UI

In production, the Vite build output (`frontend/dist/`) is served by an nginx container in the Helm chart (`deployment-frontend.yaml`). The frontend communicates with the API via REST and WebSocket.

### 3.2 FastAPI Application Server

**Repository path:** `src/`
**Technology:** Python 3.12, FastAPI, Uvicorn, SQLAlchemy (async), structlog, pydantic-settings
**Port:** 8000

The API server is the core orchestration layer. It handles all business logic and coordinates between the database, cache, LLM backend, and background workers.

**Startup sequence:**
1. Load settings from environment
2. Initialise database connection pool (asyncpg)
3. Initialise Redis rate limiter
4. Configure OpenTelemetry tracing
5. Start background worker pool (4 coroutines by default)
6. Initialise metrics collector
7. Initialise WebSocket connection manager

**Key modules:**

| Module | Path | Purpose |
|--------|------|---------|
| Agent Runtime | `src/agent/runtime.py` | Orchestrates tool calls and reasoning loops |
| Reasoning Engine | `src/agent/reasoning.py` | Multi-step chain-of-thought reasoning |
| Model Router | `src/agent/model_router/router.py` | Routes requests to light/standard/heavy tier |
| RAG Retrieval | `src/rag/retrieve.py` | Vector similarity search + reranking |
| Auth / OIDC | `src/auth/oidc.py` | JWKS fetching and JWT validation |
| PII Redaction | `src/core/pii.py` | Strips PII from logs and stored data |
| Write Framework | `src/operations/write_framework.py` | HITL approval workflow |
| Audit Export | `src/compliance/audit_export.py` | Compliance evidence generation |
| SAP Connector | `src/connectors/sap.py` | Enterprise system integration |

**Middleware stack (execution order):**
1. CORS (outermost — runs first on inbound, last on outbound)
2. OpenTelemetry distributed tracing
3. Prometheus metrics collection
4. DB-backed metrics collector
5. Security headers (CSP, HSTS, X-Frame-Options)
6. Request size limiter (10 MB maximum)
7. Request ID injector (X-Request-ID header)
8. JWT authentication and tenant extraction

### 3.3 PostgreSQL 16 + pgvector

**Image:** `pgvector/pgvector:pg16`
**Port:** 5432
**Extensions:** `vector`, `uuid-ossp`, `pg_trgm`

PostgreSQL serves as both the relational data store and the vector store for RAG embeddings. The `pgvector` extension adds an `IVFFLAT` or `HNSW` index type on the `vector` column type.

**Key tables:**

| Table | Description |
|-------|-------------|
| `tenants` | Tenant registry with configuration and isolation settings |
| `users` | User accounts, roles, and tenant memberships |
| `conversations` | Chat conversation metadata |
| `messages` | Individual chat turns with token counts |
| `documents` | Document metadata (source, ingestion status, tenant) |
| `document_chunks` | Chunked text with vector embeddings (`vector(1536)`) |
| `agent_traces` | Reasoning step traces for audit and replay |
| `audit_logs` | Immutable write-operation audit trail |
| `api_keys` | Hashed API keys for programmatic access |
| `plugins` | Tenant plugin registry |
| `budget_records` | Per-tenant token budget usage |
| `metrics` | DB-persisted platform metrics |
| `execution_plans` | HITL approval workflow state |
| `compliance_runs` | Compliance scan results and evidence |

All queries against multi-tenant tables must apply a tenant filter via `apply_tenant_filter()`. This is enforced at the repository layer.

### 3.4 Redis 7

**Image:** `redis:7-alpine`
**Port:** 6379
**Persistence:** AOF (append-only file) with `everysec` fsync

Redis provides:

- Distributed rate limiting: sliding window counters keyed by `{user_id}:{minute_bucket}`
- Response caching for expensive RAG queries
- WebSocket session state
- Short-lived token/nonce storage for OIDC flows

In production, Redis Sentinel or Redis Cluster is recommended for high availability.

### 3.5 LiteLLM Proxy

**Image:** `ghcr.io/berriai/litellm:main-latest`
**Port:** 4000
**Config file:** `litellm_config.yaml`

LiteLLM is a model-agnostic LLM gateway. It exposes a single OpenAI-compatible API regardless of the underlying provider, enabling the platform to switch or combine providers without code changes.

It handles:
- API key management and rotation per provider
- Retry logic (3 retries, 600s timeout)
- Fallback model routing
- Usage tracking

**Supported providers (configured):**
- OpenAI (gpt-4o-mini, gpt-4o, text-embedding-3-small)
- Anthropic (claude-3-5-sonnet)
- Ollama (qwen2.5:7b, qwen2.5:32b — local inference)
- vLLM (qwen2.5:72b — production GPU inference)

### 3.6 Model Router (Three-tier Architecture)

The `src/agent/model_router/` module implements intelligent cost/quality routing:

```
Request complexity assessment
          |
+---------v----------+
|   LIGHT tier        |  Simple tasks: intent classification, PII detection
|   ollama/qwen2.5:7b |  ~5B parameters, fastest, cheapest
+---------+----------+
          |  (if complex)
+---------v----------+
|  STANDARD tier      |  Most agent tasks, document Q&A, summarisation
|  ollama/qwen2.5:32b |  ~32B parameters, balanced
+---------+----------+
          |  (if complex)
+---------v----------+
|   HEAVY tier        |  Multi-step reasoning, security review, code generation
|   vllm/qwen2.5:72b  |  ~72B parameters, highest quality
+--------------------+
```

Token budgets are enforced per tenant per day and per month using the `budget_records` table.

### 3.7 Background Worker Pool

**Module:** `src/infra/background_worker.py`
**Concurrency:** 4 coroutines (configurable via `BACKGROUND_WORKER_CONCURRENCY`)

The background worker pool handles asynchronous tasks that should not block API responses:

- Document chunking and embedding generation (ingestion pipeline)
- Metrics aggregation and persistence
- Execution plan state machine transitions
- Cache warming
- Webhook delivery

Workers run as asyncio tasks within the same process. For heavy ingestion workloads, increase `BACKGROUND_WORKER_CONCURRENCY` or scale API replicas.

### 3.8 WebSocket Manager

**Module:** `src/websocket/`
**Endpoint:** `ws://api/ws/chat/{conversation_id}`

The WebSocket manager handles real-time streaming of agent responses. It maintains a registry of active connections by conversation ID and broadcasts LLM token streams to subscribed clients.

Multi-pod deployments require a shared connection registry. The current implementation uses an in-process registry; for multi-pod setups, ensure sticky sessions are configured on the load balancer (by conversation ID) or implement Redis Pub/Sub for cross-pod message fan-out.

### 3.9 Ollama (Local LLM Inference — Dev / Test)

**Image:** `ollama/ollama:latest`
**Port:** 11434
**GPU:** NVIDIA GPU passthrough via nvidia-docker2 (optional in dev)

Ollama provides self-hosted LLM inference for development and testing. In production, Ollama is disabled (`ollama.enabled: false` in Helm values) and replaced by vLLM.

Models are stored in a named Docker volume (`ollama-models`) and persist across container restarts.

### 3.10 vLLM (Production GPU Inference)

**Image:** `vllm/vllm-openai:latest`
**Port:** 8080
**GPU:** 2x NVIDIA A100 / H100 (required)
**Model:** `Qwen/Qwen2.5-72B-Instruct`

vLLM is the production-grade inference backend. It serves the Qwen 2.5 72B model with continuous batching and PagedAttention for high throughput. The vLLM server exposes an OpenAI-compatible API, which LiteLLM proxies.

Resource requirements: 32 GB RAM, 2 NVIDIA GPUs with at least 40 GB VRAM each.

---

## 4. Request Flow Diagrams

### 4.1 Chat Request (Streaming)

```
User Browser
    |
    | POST /api/v1/chat (REST to initiate)
    v
FastAPI AuthMiddleware
    | Validates JWT against OIDC JWKS
    | Extracts tenant_id, user_id
    v
Rate Limiter (Redis)
    | Checks sliding window counter
    | Increments usage counter
    v
Chat Endpoint Handler
    | Loads conversation from PostgreSQL
    | Applies tenant isolation filter
    v
Agent Runtime
    | Classifies request complexity (MODEL_LIGHT call)
    | Selects MODEL_STANDARD or MODEL_HEAVY
    v
RAG Retrieval (if documents exist)
    | Embeds user query (LITELLM_EMBEDDING_MODEL)
    | Searches pgvector index (cosine similarity, top-k=5)
    | Reranks results
    v
LiteLLM Proxy
    | Routes to selected model tier
    | Streams token response
    v
WebSocket Manager
    | Broadcasts streamed tokens to
    | connected browser client (WSS)
    v
User Browser (tokens appear in real-time)

After completion:
    |
    v
Background Worker
    | Persists conversation turn to PostgreSQL
    | Records token usage to budget_records
    | Appends audit log entry
```

### 4.2 Document Ingestion Flow

```
User
    |
    | POST /api/v1/documents/upload (multipart/form-data)
    v
FastAPI (Auth + Tenant isolation)
    v
Ingestion Endpoint
    | Creates document record in PostgreSQL (status: pending)
    | Queues ingestion job to Background Worker
    | Returns 202 Accepted with job_id
    v
Background Worker Pool
    | Step 1: Extract text (PDF/DOCX/TXT)
    | Step 2: Chunk text (CHUNK_SIZE_TOKENS=512, overlap=50)
    | Step 3: Batch embed chunks (LITELLM_EMBEDDING_MODEL)
    |         -> LiteLLM -> Ollama/OpenAI
    | Step 4: Store chunks + vectors in PostgreSQL (pgvector)
    | Step 5: Update document status to "indexed"
    v
User can poll GET /api/v1/documents/jobs/{job_id}
```

### 4.3 Write Operation (HITL Approval Workflow)

```
Agent detects write intent (SAP order, config change, etc.)
    |
    v
Write Framework (src/operations/write_framework.py)
    | Creates execution_plan record (status: pending_approval)
    | Sends notification to approver (SMTP or webhook)
    v
Approver receives email / Slack message
    | Reviews plan details
    | Approves or rejects via API (POST /api/v1/plans/{id}/approve)
    v
Write Framework
    | If approved: executes SAP/MES connector call
    | If rejected: marks plan as rejected, notifies agent
    | Appends full audit log entry regardless of outcome
```

---

## 5. Data Architecture

### 5.1 Multi-tenancy Model

Every row in every business table includes a `tenant_id` foreign key. The `apply_tenant_filter()` function in `src/core/tenant.py` is called on every query to enforce row-level isolation. This is a mandatory coding standard — queries without a tenant filter will not pass code review.

```
tenants table (id, name, config, created_at)
     |
     +-- users (tenant_id, id, email, role, ...)
     |
     +-- conversations (tenant_id, id, title, agent_id, ...)
     |       |
     |       +-- messages (conversation_id, role, content, tokens, ...)
     |
     +-- documents (tenant_id, id, filename, status, ...)
     |       |
     |       +-- document_chunks (document_id, tenant_id, content, embedding vector(1536), ...)
     |
     +-- audit_logs (tenant_id, id, operation, actor, payload, created_at)
     |
     +-- budget_records (tenant_id, model, tokens_used, date, ...)
     |
     +-- api_keys (tenant_id, id, key_hash, scopes, ...)
```

### 5.2 Vector Search

Document embeddings are stored in the `document_chunks.embedding` column as `vector(1536)`. The pgvector extension provides:

- `<=>` operator: cosine distance (used for semantic similarity)
- `<->` operator: L2 (Euclidean) distance
- `<#>` operator: negative inner product

The default query uses cosine similarity with a `vector_top_k=5` limit. An `IVFFLAT` or `HNSW` index is recommended for datasets above 100,000 chunks.

### 5.3 Audit Trail

The `audit_logs` table stores an immutable record of all write operations. Records are INSERT-only — no UPDATE or DELETE operations are permitted. This supports compliance exports for SOC 2, ISO 27001, and enterprise client internal security policies.

---

## 6. Security Architecture

### 6.1 Authentication Flow

```
Browser
    |
    | 1. Redirect to OIDC provider (Keycloak)
    v
Keycloak
    | 2. User authenticates (MFA, SSO, SAML)
    | 3. Issues signed JWT (RS256)
    v
Browser
    | 4. Includes JWT in Authorization: Bearer header
    v
FastAPI AuthMiddleware
    | 5. Fetches JWKS from {OIDC_ISSUER_URL}/.well-known/openid-configuration
    | 6. Validates JWT signature, expiry, audience, client_id
    | 7. Extracts tenant_id and user roles from custom claims
    | 8. Injects into request context
```

The JWKS endpoint response is cached to avoid fetching on every request.

In `ENVIRONMENT=dev`, the OIDC validation is bypassed and symmetric HMAC-SHA256 JWTs signed with `DEV_JWT_SECRET` are accepted. This path is never exercised in production.

### 6.2 API Key Authentication (Programmatic Access)

Tenants can generate API keys via the `/api/v1/keys` endpoint for use in programmatic integrations. Keys are:

- Generated as cryptographically random 256-bit values
- Stored only as SHA-256 hashes in the database
- Never logged in plaintext
- Scoped to specific API capabilities

### 6.3 Security Controls

| Control | Implementation |
|---------|---------------|
| Transport encryption | TLS 1.2+ enforced by nginx Ingress |
| Authentication | OIDC JWT (RS256) via Keycloak |
| Authorisation | Role-based (tenant admin, user, read-only) |
| Tenant isolation | Row-level filtering on all queries |
| Rate limiting | Redis sliding window, 60 req/min per user |
| Request size | 10 MB maximum enforced by middleware |
| Security headers | CSP, HSTS, X-Frame-Options, X-Content-Type |
| PII protection | Automatic redaction before logging |
| Audit logging | Immutable INSERT-only audit trail |
| Secret management | Kubernetes Secrets (recommend Vault/Sealed Secrets) |
| Container security | Non-root user (UID 1000), read-only root filesystem, dropped capabilities |
| Network policy | Kubernetes NetworkPolicy restricts pod-to-pod traffic |
| SAST / SCA | Bandit + pip-audit in CI pipeline |

---

## 7. Deployment Topology

### 7.1 Kubernetes Workloads

```
Namespace: enterprise-agents
|
+-- Deployment: enterprise-agent-platform-api (2-10 pods, HPA)
|       Image: enterprise-agent-platform:1.0.0
|       Ports: 8000 (http)
|       Probes: /health/live, /health/ready
|       Resources: 250m CPU / 256Mi RAM (request), 500m / 512Mi (limit)
|
+-- Deployment: enterprise-agent-platform-worker (2 pods)
|       Image: enterprise-agent-platform:1.0.0
|       Command: background worker mode
|       Resources: 250m CPU / 256Mi RAM (request), 500m / 512Mi (limit)
|
+-- Deployment: enterprise-agent-platform-frontend (1 pod)
|       Image: enterprise-agent-platform:1.0.0 (nginx stage)
|       Port: 5173
|       Resources: 100m CPU / 128Mi RAM (request), 200m / 256Mi (limit)
|
+-- Deployment: litellm (1 pod)
|       Image: ghcr.io/berriai/litellm:main-latest
|       Port: 4000
|       Config: litellm_config.yaml via ConfigMap
|
+-- Deployment: vllm (1 pod, GPU node)
|       Image: vllm/vllm-openai:latest
|       Port: 8080
|       Resources: 8 CPU / 32Gi RAM, 2x nvidia.com/gpu
|
+-- StatefulSet: postgresql (via Bitnami chart)
|       Image: pgvector/pgvector:pg16
|       Port: 5432
|       Storage: 20 Gi PVC
|
+-- StatefulSet: redis (via Bitnami chart)
|       Image: redis:7-alpine
|       Port: 6379
|       Storage: 5 Gi PVC
|
+-- HorizontalPodAutoscaler: enterprise-agent-platform-api
|       min: 2, max: 10, CPU: 70%, Memory: 80%
|
+-- PodDisruptionBudget: enterprise-agent-platform
|       minAvailable: 1
|
+-- NetworkPolicy: enterprise-agent-platform
|       Restricts ingress/egress to defined service ports
|
+-- Ingress: enterprise-agent-platform
        class: nginx
        host: agents.example.com
        TLS: cert-manager / letsencrypt
```

### 7.2 Multi-region Topology

```
Global Load Balancer (Route53 / Traffic Director)
    |         |              |
    |         |              |
 US-EAST-1  EU-WEST-1   AP-SOUTHEAST-1
    |         |              |
  K8s         K8s            K8s
  Cluster     Cluster        Cluster
    |         |              |
  Primary   Secondary      Secondary
  DB (RW)   DB (RO replica) DB (RO replica)
```

Routing strategy is `residency_first`: tenant requests are routed to their configured data residency zone. EU-domiciled tenants always route to `eu-west-1` for GDPR compliance. If a region becomes unavailable, automatic failover triggers after 3 consecutive health check failures (with a 30-minute cooldown to prevent flapping).

---

## 8. Technology Decisions

### Why FastAPI over Django / Flask?

FastAPI's native async support (asyncio + asyncpg) is essential for handling concurrent LLM streaming responses efficiently. A synchronous framework would require thread pools or process workers, significantly increasing resource consumption and latency under concurrent load.

### Why PostgreSQL + pgvector over a dedicated vector database?

Storing vectors alongside relational data in PostgreSQL eliminates the operational burden of a separate vector store and allows transactional consistency between document metadata and embeddings. pgvector's performance is sufficient for the expected scale (millions of chunks). The decision can be revisited if vector search query times exceed acceptable thresholds at scale.

### Why LiteLLM over direct provider SDKs?

LiteLLM provides a stable abstraction layer that allows switching LLM providers, adding fallbacks, and testing with local models without changing application code. This is strategically important for enterprise clients given the evolving landscape of enterprise LLM providers and the requirement to support both cloud and fully on-premise deployments.

### Why Redis for rate limiting over in-process limiting?

In-process rate limiting state is lost on pod restart and is not shared across replicas. Redis provides a distributed, consistent rate limiting counter that works correctly when the API is horizontally scaled. The cost is a network round-trip per rate-limited request, which is acceptable given Redis's sub-millisecond latency on a local network.

### Why three-tier model routing?

The three-tier routing strategy optimises for cost and latency. Routing every request through a 72B parameter model would be prohibitively expensive. The vast majority of requests (intent classification, simple Q&A) are handled by the 7B model at a fraction of the cost and with lower latency. The heavy tier is reserved for complex reasoning tasks where quality justifies the additional compute cost.
