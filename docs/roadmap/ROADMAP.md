# Enterprise Agent Platform — Unified Roadmap

**Author:** Jan Střechovský (Honza)
**Last Updated:** 2026-02-17
**Status:** Phases 1–10 Complete | Phase 11 Next

---

## Vision

Build a production-grade, multi-tenant AI agent platform with **specialist agent orchestration** for enterprise on-premise deployment. Enables intelligent AI assistants over internal document libraries and operational systems (SAP, MES).

**On-Premise First**: All AI inference runs on local GPU servers via vLLM/Ollama behind a LiteLLM proxy. Cloud APIs (OpenAI, Anthropic) are available as optional development/fallback but **never used in production**. Production LiteLLM config must point exclusively to on-premise endpoints.

---

## Compliance Framework

Designed to comply with enterprise client policies:

| Policy | Key Requirements |
|--------|-----------------|
| Data Classification (Class I–IV) | Enforce classification on ingested documents. Class III/IV require data owner approval |
| Global AI Policy | Human oversight required. AI output must be verified. Audit all AI usage |
| Application Security Standard | OWASP, input validation, TLS 1.2+, SAST/DAST, centralized logging |
| IAM Standard | Entra ID / OIDC, MFA for privileged access |
| Network Security | Security zones, firewall deny-all, encrypted internal comms |
| Confidential Information | No data leakage across tenant boundaries |
| Records Management | Audit logs = Official Records. Retention per schedule. Destruction holds |
| Security Architecture | Security by design, defense in depth, least privilege, fail secure |
| GDPR (if EU deployment) | Right to access, right to erasure, data portability. Implemented in `src/compliance/gdpr.py` |

### Data Classification Enforcement

| Class | Platform Behavior |
|-------|-------------------|
| **Class I — General** | No restrictions. Available to all authenticated users within tenant |
| **Class II — Confidential** | Default level. Tenant-isolated. Standard RBAC |
| **Class III — Critical** | Need-to-know ACL. Audit every access. PII sanitization mandatory |
| **Class IV — Restricted** | Data owner approval before ingestion. Cannot enter agent prompts without pre-approval |

---

## On-Premise AI Model Strategy

### Production Inference (vLLM on GPU servers)

| Role | Model | Parameters | GPU | Purpose |
|------|-------|-----------|-----|---------|
| Fast (default) | Qwen 2.5 7B | 7B | 1x A100/H100 | Q&A, summarization, intent routing |
| Standard | Qwen 2.5 32B | 32B | 1-2x A100/H100 | Skill execution, agent tasks |
| Heavy (escalation) | Qwen 2.5 72B | 72B | 2-4x A100/H100 | Complex reasoning, orchestration |
| Embedding | nomic-embed-text | 137M | CPU or GPU | Document/query embedding |
| Re-ranking | bge-reranker-v2-m3 | 568M | 1x GPU | Cross-encoder re-ranking |

### Development Inference (Ollama, no GPU)

| Role | Model | Purpose |
|------|-------|---------|
| Fast | llama3.2 (3B) | Local development |
| Embedding | nomic-embed-text | Local embedding |

### Model Routing via LiteLLM
- All inference through LiteLLM proxy — app never calls models directly
- Production: vLLM only (no external network calls)
- Development: Ollama locally, optional cloud fallback for convenience
- Cost tracking per tenant, user, and agent

---

## Completed Phases (1–10)

### Phase 1: MVP Core Backend ✅

Multi-tenant API with RAG over uploaded documents.

- Multi-tenant data model (UUID PKs, tenant-scoped tables)
- JWT / OIDC authentication (dev: HS256, prod: JWKS)
- Role-based access control (admin / operator / viewer)
- Chat endpoint with LiteLLM, conversation history
- RAG pipeline: ingest → chunk → embed (pgvector) → retrieve
- Citation tracking, audit logging, rate limiting
- Docker Compose, Alembic migrations, health checks

### Phase 2: Agent Runtime + Compliance ✅

Multi-agent system with specialist agents and compliance controls.

- **5 specialist agents**: Document Analyst, Procedure Expert, Data Analyst, Quality Inspector, Maintenance Advisor
- **Agent Orchestrator**: intent classification → specialist routing
- **Reasoning loop**: OBSERVE → THINK → VERIFY (3-phase)
- **Thinking tools**: Council, RedTeam, FirstPrinciples
- **Compliance**: PII sanitization, data classification enforcement, export control guard, AI disclosure, destruction hold support
- **Security**: TLS, CORS, Pydantic validation, MFA (TOTP), lockout after 6 failures, SAST/DAST pipeline

### Phase 3: Skills + Connectors + UI ✅

Extensible skills, enterprise connectors, web interface.

- **Skill Registry**: plugin system with role-based access, 4 built-in skills (Document Analysis, Procedure Lookup, Report Generation, Calculations)
- **RAG**: hybrid search (pgvector + BM25), cross-encoder reranking, metadata filtering, conversation memory
- **SAP connector** (read): purchase orders, inventory, cost centers, material master (OData v2)
- **MES connector** (read): production orders, machine status, quality reports, downtime events
- **SQL Guard**: safe structured data queries with guardrails
- **React 19 UI**: chat with SSE streaming, document management, agent selection, admin panel, classification selector

### Phase 4: Orchestration + Write Ops + Governance ✅

Multi-agent composition and HITL write operations.

- **Composition patterns**: Pipeline, Fan-out, Gate, TDD Loop
- **Goal Planner**: DAG decomposition with topological ordering
- **Write operations**: HITL approval workflow (PROPOSED → APPROVED → EXECUTED), SAP/MES write connectors
- **Model routing**: 3-tier (LIGHT/STANDARD/HEAVY) with automatic escalation, token budgets per tenant
- **Compliance**: SOC 2 Type II export, compliance dashboard, AI governance metrics
- **Notifications**: approval via email/webhook, timeout escalation
- **GDPR**: right to access, erasure (anonymization), data portability (partial — see Phase 11)

### Phase 5: Production Hardening ✅

- 1177 tests (88 test files, 28K+ lines), 0 failures
- Integration tests against Docker PostgreSQL + Redis
- GitHub Actions CI: lint → typecheck → test → security scan → Docker build
- Helm charts for Kubernetes (API, workers, frontend, LiteLLM, vLLM)
- README, ARCHITECTURE.md (625 lines), RUNBOOK.md (1185 lines)
- Security debt resolved: PyJWT migration, JWT audience validation, configurable CORS

### Phase 6: Advanced Features ✅

- Feedback loop (thumbs up/down → fine-tuning dataset)
- Analytics dashboard data
- Fine-tuning job management (PersistentFineTuningManager)
- Document ingestion pipeline

### Phases 7–10: Production Readiness + Polish ✅

- **7**: CI/CD pipeline, persist in-memory stores to DB, wire SAP/MES write execution, email notifications, integration tests, security scanning
- **8**: Grafana dashboards + Prometheus, load testing (Locust + k6), backup/restore automation, Loki log aggregation
- **9**: Fix broken imports, real TOTP MFA, ConnectorRegistry wiring, escalation notifications, WriteOperationRecord persistence
- **10**: Fine-tuning job queue, compliance dashboard DB queries, test suite fixes, code cleanup (60+ files)

---

## Current: Phase 11 — Deployment Readiness + Intelligence Layer

**Goal**: (1) Make the platform deployable at customer site. (2) Wire together the intelligence loop — the platform gets smarter with every interaction, per user.

### 11A: Air-Gapped Production Config (P0 — blocks deployment)

| # | Item | Description |
|---|------|-------------|
| 11A1 | Production LiteLLM config | Replace cloud model references (OpenAI/Anthropic) with vLLM/Ollama endpoints in `litellm_config.yaml` |
| 11A2 | OIDC offline mode | Support pre-loaded JWKS for environments without IdP network access. Add `JWKS_LOCAL_PATH` config option |
| 11A3 | Offline Docker build | Script for building with pre-cached pip wheels (private PyPI mirror or vendored deps) |
| 11A4 | Seed data script | `scripts/seed.py` — create initial tenant, admin user, sample documents for first deployment |
| 11A5 | Deployment checklist | In RUNBOOK.md: secrets rotation, TLS cert setup, network policy verification, pgvector validation |
| 11A6 | GDPR request persistence | Add migration + table for `gdpr_requests` (requests currently not stored in DB) |

### 11B: Memory → Prompt Injection (P1 — the platform remembers)

Wire the existing memory system into agent execution so agents actually use what they know about the user.

| # | Item | Description |
|---|------|-------------|
| 11B1 | Recall memories in runtime | In `AgentRuntime.run()`, call `recall_memories(agent_id, tenant_id, query)` and inject results into system prompt as user context |
| 11B2 | Extract preferences from conversations | Post-response, LLM extracts domain facts and user preferences → stores as PREFERENCE/FACT memories |
| 11B3 | Memory-aware specialist selection | Orchestrator checks user's memory before routing — if user has history with Quality Inspector, prefer that specialist for ambiguous queries |

**What exists**: Memory service with 5 types (FACT, PREFERENCE, SKILL, CONTEXT, EPISODIC), semantic search, decay, compaction. **Gap**: Not wired into prompts. Agents don't read it.

### 11C: Feedback → Learning Loop (P1 — the platform improves)

Close the feedback loop so thumbs-up/down actually changes future behavior.

| # | Item | Description |
|---|------|-------------|
| 11C1 | Feedback updates memory | Negative feedback → extract what went wrong → store as FACT memory ("user dislikes verbose responses" / "wrong SOP referenced") |
| 11C2 | LEARN step after response | Post-VERIFY reflection: what approach was used, did it work, store lesson. Lightweight LLM call or rule-based extraction |
| 11C3 | Feedback-weighted retrieval | RAG retrieval boosts chunks from documents that received positive feedback, deprioritizes negatively-rated sources |

**What exists**: Feedback service (thumbs up/down, 1-5 rating, export for fine-tuning). **Gap**: Write-only. Never read back to influence responses.

### 11D: Auto-Composition Selection (P1 — multi-agent becomes automatic)

Make the 4 composition patterns (Pipeline, Fan-out, Gate, TDD Loop) fire automatically instead of requiring manual code.

| # | Item | Description |
|---|------|-------------|
| 11D1 | Complexity classifier | After intent classification, assess query complexity: SIMPLE (direct agent) / DEEP (Pipeline) / MULTI-PERSPECTIVE (Fan-out) / QUALITY-CRITICAL (Gate) |
| 11D2 | Orchestrator auto-composition | If complexity > SIMPLE, orchestrator auto-selects and executes the right pattern. User sees richer answer, not implementation detail |
| 11D3 | Model escalation on failure | If 7B returns low-confidence answer, auto-retry on 72B before returning to user |

**What exists**: 4 patterns fully implemented (955 lines). **Gap**: Never triggered automatically. No API exposes them.

### 11E: Persistent User Goals (P2 — goal-oriented, not task-oriented)

Users have ongoing objectives that span multiple conversations.

| # | Item | Description |
|---|------|-------------|
| 11E1 | User goals table | Migration + model for persistent goals per user. Fields: goal text, status, progress notes, created/updated timestamps |
| 11E2 | Goal tracking in conversations | At conversation start, recall user's active goals. Agent considers them when answering. At conversation end, update goal progress |
| 11E3 | Goal-informed decomposition | Goal Planner checks existing goals — if user asks something related to an active goal, context from previous plan executions is included |

**What exists**: Goal Planner with DAG decomposition. **Gap**: Goals don't persist. Each conversation starts from zero.

### Implementation Order
```
11A (air-gapped)  ─── blocks deployment, do first
      │
11B (memory)      ─── small effort, high impact, do second
      │
11C (feedback)    ─── builds on 11B (needs memory injection working)
      │
11D (auto-compose)─── independent, can parallel with 11C
      │
11E (goals)       ─── builds on 11B+11C (needs memory + learning working)
```

### The Intelligence Loop (after Phase 11)
```
User sends message
      │
      ▼
  ┌─ OBSERVE ─── recall memories + active goals ◄── 11B, 11E
  │
  ├─ THINK ───── classify complexity ◄── 11D
  │              auto-select composition pattern
  │
  ├─ VERIFY ──── agent(s) produce response
  │
  └─ LEARN ───── extract lessons ◄── 11C
                 update memory from feedback
                 update goal progress ◄── 11E
                       │
                       ▼
              Next response is smarter
```

---

## Future: Phase 12 — Customer-Driven Expansion

**Goal**: Features built only when requested by actual customer deployments.

### 12A: Proactive Monitoring & Alerts (high value for manufacturing)

Infrastructure exists: MES polling, webhook system, background workers. Just needs wiring.

- Scheduled MES polling tasks (machine status every 60s, quality every 5min)
- Configurable alert thresholds per tenant (downtime > 30min, defect rate > 5%, inventory < minimum)
- Automatic webhook dispatch to Teams/Slack/email on threshold breach
- Agent auto-generates root cause analysis when alert fires
- **When**: Customer wants shift supervisors notified in real-time
- **Effort**: Small — existing infrastructure, new task types + threshold config

### 12B: Thinking Tools in Main Flow (smarter answers on complex queries)

Council, RedTeam, FirstPrinciples exist but only RedTeam is used (for compliance only).

- Orchestrator assesses query complexity → opt-OUT thinking tools for complex queries
- Council for multi-perspective decisions ("should we reschedule production?")
- FirstPrinciples for root cause analysis ("why are we losing yield on batch X?")
- Thinking tool output included in response with collapsible reasoning trace
- **When**: Users report that complex queries get shallow answers
- **Effort**: Small — tools implemented, need orchestrator integration

### 12C: Agent Quality Evaluation (golden dataset)

No way to measure if agent answers are correct. Critical for manufacturing safety.

- Golden dataset: 50–100 typical manufacturing queries + verified correct answers
- Automated benchmark: run nightly, track answer quality over time
- Regression detection: alert if model update or config change degrades quality
- Per-specialist scoring (Quality Inspector accuracy vs. Maintenance Advisor accuracy)
- **When**: Before any model update or fine-tuning deployment
- **Effort**: Medium — needs domain expert to create golden dataset

### 12D: Multi-Modal Support (manufacturing killer feature)

- Vision model for quality inspection images (defect detection, schematics)
- Image upload API with classification enforcement
- PDF/image OCR pipeline feeding into RAG
- **When**: Customer has quality inspection use case with photos

### 12E: Tenant Administration Portal

- Tenant self-service: manage users, roles, API keys
- Usage dashboards per tenant
- Rate limit and model configuration per tenant
- **When**: Multiple tenants need self-management

### 12F: SSO Deep Integration

- SAML 2.0 support alongside existing OIDC
- Group-to-role mapping from IdP claims
- **When**: Customer IdP doesn't support OIDC (legacy AD FS)

### 12G: Multi-Site Support

- Lightweight edge agent for remote plant locations
- Sync protocol for edge ↔ central server
- Offline-capable with local 7B inference
- **When**: Customer has network-isolated manufacturing sites

---

## Non-Goals

- Consumer-facing chat interface
- Cloud-based inference in production (on-premise only)
- Custom model training from scratch (fine-tuning only)
- General-purpose code generation
- Multi-cloud deployment
- GraphQL gateway (REST is sufficient)
- Plugin marketplace / developer ecosystem (premature — Skill Registry covers extensibility)

---

## Architecture Summary

```
CURRENT (Phases 1-10)              AFTER PHASE 11
═══════════════════                ═══════════════

Agent Orchestrator                 Agent Orchestrator
├─ 5 specialists                   ├─ 5 specialists
├─ Composer (manual)          →    ├─ Auto-Composer (11D)
├─ Goal Planner (per-request) →    ├─ Goal Planner (persistent goals, 11E)
├─ Thinking tools                  ├─ Thinking tools
├─ Model router                    ├─ Model router (+ auto-escalation, 11D3)
│                                  │
├─ Memory (stored, unused)    →    ├─ Memory (injected into prompts, 11B)
├─ Feedback (collected, unused)→   ├─ Feedback → Memory → Learning loop (11C)
│                                  │
└─ OBSERVE → THINK → VERIFY       └─ OBSERVE → THINK → VERIFY → LEARN (11C2)
         │                                  │                        │
         │ (no learning)                    │    (learns per user)   │
         ▼                                  └────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  React 19 UI (Chat, Docs, Admin, Agents)                    │
├─────────────────────────────────────────────────────────────┤
│  FastAPI (Auth, RBAC, Rate Limiting, Audit)                 │
├──────────────┬──────────────┬───────────────────────────────┤
│  Agent       │  RAG         │  Compliance                   │
│  Orchestrator│  Pipeline    │  Engine                       │
│  ├─ 5 specs  │  ├─ Hybrid   │  ├─ PII sanitizer            │
│  ├─ Composer │  │  search   │  ├─ Classification            │
│  ├─ Goal     │  ├─ Reranker │  ├─ Export control            │
│  │  Planner  │  └─ pgvector │  ├─ SOC 2 / GDPR             │
│  ├─ Thinking │              │  └─ Audit log                 │
│  │  tools    │              │                               │
│  ├─ Model    │              │                               │
│  │  router   │              │                               │
│  ├─ Memory ◄─┤── Feedback ──┤── Learning loop (Phase 11)    │
│  └─ Goals    │              │                               │
├──────────────┴──────────────┴───────────────────────────────┤
│  Enterprise Connectors                                      │
│  ├─ SAP (OData v2, read+write)                             │
│  ├─ MES (REST, read+write)                                 │
│  └─ HITL Approval Workflow                                  │
├─────────────────────────────────────────────────────────────┤
│  PostgreSQL 16 + pgvector │ Redis 7 │ LiteLLM → vLLM/Ollama│
└─────────────────────────────────────────────────────────────┘

Deployment: Docker Compose (dev) │ Helm/K8s (prod) │ CI/CD (GitHub Actions)
Monitoring: Grafana + Prometheus │ Loki + Promtail │ Structured JSON logs
```

---

*Unified Roadmap v4.3 — PAI principles finalized (proactive monitoring, thinking tools, evals)*
*Previous: v4.1 (critical review), v4.0 (unification), v3.0 (V2 merge), v2.0 (Phase 5-6), v1.0 (Phase 1-4)*
*All technical debt items from original tracker: RESOLVED*
