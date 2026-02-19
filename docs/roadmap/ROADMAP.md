# Enterprise Agent Platform — Unified Roadmap

**Author:** Jan Střechovský (Honza)
**Last Updated:** 2026-02-19
**Status:** Phases 1–11 Complete | Phase 11F (DevOps Hardening) Next | Phases 13–16 Vision (Work Partner → Hierarchical Intelligence → Operational Intelligence → Discovery & Onboarding)

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

## Current: Phase 11F — DevOps Hardening (Production Blockers)

**Goal**: Close the remaining infrastructure gaps identified in the senior DevOps review (2026-02-19). These are **production blockers** — must be resolved before first customer deployment.

**DevOps Review Score: 7.5/10** — Kubernetes layer is production-grade, Docker builds clean, dev workflow polished. Gaps in CI/CD, alerting, and secrets management.

### 11F-P0: Production Blockers (CRITICAL)

| # | Item | Severity | Description | Domain |
|---|------|----------|-------------|--------|
| 11F1 | CI/CD pipeline | CRITICAL | No automated testing on push/PR. Carry over v2's GitHub Actions and adapt. PR gate: lint → typecheck → test. Build gate: container image on tag. | CI/CD |
| 11F2 | Alerting rules | CRITICAL | `prometheus.yml` has `rule_files: []`. Zero alerts. Add minimum 5: error rate >1%, P99 latency >2s, disk >85%, CPU >90%, OOM kills. Wire Alertmanager. | Observability |
| 11F3 | Secrets startup validation | HIGH | App must FAIL to start if `SECRET_KEY` or `POSTGRES_PASSWORD` contains a known default value when `ENVIRONMENT=prod`. Add check in `lifespan()`. | Security |
| 11F4 | External secrets integration | HIGH | Helm `secret.yaml` has `useExternalSecret` stub but no `ExternalSecret` CRD template. Implement for Azure Key Vault (TE Connectivity standard). | Security |
| 11F5 | Pin all production image tags | HIGH | `litellm: main-latest`, `vllm: latest`, `python:3.12-slim` — all floating. Pin to digest or specific version. Supply chain risk. | Docker |

### 11F-P1: High Priority Improvements

| # | Item | Severity | Description | Domain |
|---|------|----------|-------------|--------|
| 11F6 | Container image scanning | HIGH | Add Trivy scan to CI pipeline. `SECURITY.md` claims scanning exists but no evidence. | CI/CD |
| 11F7 | Dependency vulnerability scanning | HIGH | `pip-audit` in dev deps but no automated schedule. Add weekly cron job in GitHub Actions. | CI/CD |
| 11F8 | Hardcoded passwords in values.yaml | HIGH | `postgresql.auth.password: app_password` — change to empty string with validation that `--set` was used. | Kubernetes |
| 11F9 | Edge encryption at rest | MEDIUM | Edge SQLite at `/data/edge.db` unencrypted. Factory floor devices get stolen. Add LUKS volume or encrypted SQLite. | Edge |
| 11F10 | SLO/SLI definitions | MEDIUM | No defined: request latency P99 target, error rate threshold, availability target. Without SLIs, dashboards are vanity metrics. | Observability |

### 11F-P2: Medium Priority Improvements

| # | Item | Severity | Description | Domain |
|---|------|----------|-------------|--------|
| 11F11 | Topology spread constraints | MEDIUM | API pods could land on same node. Add `topologySpreadConstraints` for zone-aware spreading. | Kubernetes |
| 11F12 | Postgres/Redis exporters | MEDIUM | Commented out in `prometheus.yml`. DB health is critical — connection pool, query latency, replication lag. | Observability |
| 11F13 | Telemetry enabled by default in prod | MEDIUM | `ENABLE_TELEMETRY=false` in Helm values. First prod deploy will have no traces until flipped. | Observability |
| 11F14 | Worker HPA | MEDIUM | Only API has autoscaling. Workers should scale based on queue depth metric. | Kubernetes |
| 11F15 | Startup probes on API | LOW | `initialDelaySeconds` is crude. `startupProbe` with `failureThreshold: 30` better for cold starts. | Kubernetes |
| 11F16 | Helm test templates | LOW | No `templates/tests/`. `helm test <release>` would validate post-deploy health. | Kubernetes |
| 11F17 | Pre-commit hooks | LOW | No `.pre-commit-config.yaml`. Linting is manual, format issues caught late. | Dev Workflow |
| 11F18 | Bitnami subchart dependencies | LOW | `Chart.yaml` missing `dependencies:` block for postgresql and redis Bitnami charts. | Kubernetes |
| 11F19 | Docker healthcheck without httpx | LOW | Main Dockerfile uses `python -c "import httpx"` for health — heavier than needed. Use `curl` or TCP check. | Docker |
| 11F20 | Compose version key deprecated | LOW | `version: "3.9"` — remove for Compose Specification format. | Dev Workflow |

### Implementation Order

```
11F1 (CI/CD pipeline)        ─── enables everything else, do FIRST
      │
11F2 (alerting rules)        ─── deploy without alerts = blind
      │
11F3 + 11F4 (secrets)        ─── security blockers, parallel with above
      │
11F5 + 11F6 + 11F7 (supply chain) ─── pin images, add scanning
      │
11F8–11F20 (improvements)    ─── prioritized cleanup, parallelizable
```

### What's Already Excellent (preserve, don't change)

- **Kubernetes SecurityContext**: `runAsNonRoot`, `readOnlyRootFilesystem`, `drop ALL`, `seccompProfile: RuntimeDefault` — textbook
- **NetworkPolicies**: Per-component, least-privilege, DNS explicit. Better than 90% of Helm charts
- **HPA tuning**: scaleDown stabilization 300s, aggressive scaleUp with Max policy
- **Dev workflow**: `make dev` one-command startup, mock LLM, seed data, DB operations
- **Edge architecture**: SQLite fallback + sync daemon — right pattern for factory floor

---

## 🚀 FIRST DEPLOYMENT CHECKPOINT — What Must Be Done Before First Client Test

> **Princip:** Platforma nesmí být prázdná skořápka. První den u klienta musí operátor otevřít chat a dostat HODNOTU. To vyžaduje minimum funkčnosti + minimum dat. Vše ostatní se přidává iterativně na základě reálného feedbacku.

### Tier 1: MUST HAVE (bez tohoto nejeď) 🔴

Bez těchto položek platforma buď nefunguje, nebo je nebezpečná.

| Oblast | Co konkrétně | Odkud | Stav |
|--------|-------------|-------|------|
| **Core platform** | Chat, RAG, 5 specialistů, RBAC | Phase 1-4 | ✅ Done |
| **Production hardening** | Tests, CI/CD, security | Phase 5 | ✅ Done |
| **CI/CD pipeline** | Automated test + build on push | 11F1 | ❌ TODO |
| **Alerting** | Min 5 alerts (error rate, latency, disk, CPU, OOM) | 11F2 | ❌ TODO |
| **Secrets management** | No defaults in prod, external secrets | 11F3-4 | ❌ TODO |
| **Image pinning** | All Docker images pinned to digest | 11F5 | ❌ TODO |
| **Air-gapped config** | LiteLLM → vLLM only, OIDC offline, offline Docker | 11A1-3 | ✅ Done |
| **Seed data** | Initial tenant, admin user, sample docs | 11A4 | ✅ Done |
| **Deployment checklist** | RUNBOOK.md secrets, TLS, network, pgvector | 11A5 | ✅ Done |
| **GDPR persistence** | gdpr_requests table | 11A6 | ✅ Done |

**→ Zbývá: 11F1-5 (DevOps hardening P0)**

### Tier 2: SHOULD HAVE (výrazně zvyšuje hodnotu prvního testu) 🟡

Platform funguje i bez toho, ale s tím je test mnohem přesvědčivější.

| Oblast | Co konkrétně | Odkud | Proč |
|--------|-------------|-------|------|
| **Memory injection** | Agent si pamatuje kontext uživatele | 11B | Bez toho každý chat začíná od nuly — neprofesionální dojem |
| **Feedback → learning** | Thumbs up/down mění budoucí chování | 11C | Ukazuje že platforma se učí — wow efekt pro stakeholdery |
| **Auto-composition** | Automatický výběr simple/deep/multi pattern | 11D | Lepší kvalita odpovědí bez manuální konfigurace |
| **Container scanning** | Trivy + pip-audit v CI | 11F6-7 | Security compliance check pro klientův IT |
| **SLO/SLI definice** | Latency targets, error thresholds | 11F10 | Klient se zeptá "jaké máte SLA?" — musíš mít odpověď |
| **Proactive monitoring** | MES polling + alert thresholds | 12A | Killer feature pro shift supervisory — reálná hodnota od dne 1 |
| **Agent quality baseline** | Golden dataset 50-100 otázek | 12C | Jak víš že agent odpovídá správně? Klient se zeptá. |

**→ Phase 11B-D + vybrané 11F + 12A + 12C**

### Tier 3: LATER (přidej až po prvním testu na základě feedbacku) 🟢

Cenné features, ale vyžadují buď data z provozu nebo specifický klientský požadavek.

| Oblast | Proč až později | Odkud |
|--------|----------------|-------|
| Goals (persistent, OKR) | Potřebuješ uživatele kteří chtějí trackovat cíle — ne pro první test | 11E, 13A-C |
| Proactive intelligence | Potřebuje paměťová data z reálného provozu (min. 2-4 týdny) | 13B |
| Project context | Pokročilá feature, ne pro první dojem | 13D |
| Organizational knowledge | Potřebuje víc uživatelů + opt-in consent flow | 13E |
| Hierarchical intelligence | Potřebuje fungující user level + min. 1 oddělení s daty | Phase 14 |
| Shift handoff | Potřebuje min. 2 směny s reálnými konverzacemi | 15A |
| ROI measurement | Potřebuje měsíce dat pro statistickou relevanci | 15B |
| Discovery & onboarding | Build after 2-3 manual deployments — extract the pattern | Phase 16 |
| Offline/edge mode | Důležité ale ne pro první test (ten bude na dobré síti) | 12G |
| Knowledge continuity | Potřebuje fungující hierarchii + reálné departures | 14G |
| Multi-modal (images) | Pokud klient nemá quality inspection use case, nepotřebuješ | 12D |
| Voice-first | Future option po Phase 15 | — |

### Data Bootstrapping Checklist (CRITICAL — den -14 až den 0)

> **Největší riziko prvního deploymentu:** Platforma je nasazená, ale agent nemá co prohledávat. Operátor se zeptá a dostane "Nenašel jsem relevantní informace." = okamžitá ztráta důvěry.

**2 týdny PŘED prvním testem:**

| # | Úkol | Kdo | Výstup |
|---|------|-----|--------|
| B1 | Získej 20-50 klientových SOP dokumentů | Klientský champion | PDF/DOCX v dohodnuté struktuře |
| B2 | Ingestuj dokumenty do RAG pipeline | Deployment team | Dokumenty chunknuté, embeddované, prohledatelné |
| B3 | Nakonfiguruj SAP connector | Deployment team + klient IT | Read access k relevantním SAP modulům (MM, PP, QM) |
| B4 | Nakonfiguruj MES connector | Deployment team + klient IT | Read access k production orders, machine status, quality |
| B5 | Vytvoř tenant + uživatele | Admin | Tenant s OIDC na klientův Entra ID, role assignment |
| B6 | Otestuj 10 typických dotazů | Deployment team | Agent odpovídá smysluplně na klientovy use cases |
| B7 | Připrav golden dataset | Domain expert + deployment | 20-50 otázek s ověřenými odpověďmi pro benchmark |
| B8 | Nastav RBAC roles | Admin + klient | Operator/engineer/manager role s odpovídajícími oprávněními |

**1 den PŘED prvním testem:**

| # | Úkol | Kdo | Výstup |
|---|------|-----|--------|
| B9 | Smoke test: celý flow od loginu po odpověď | QA | Screenshot/video proof |
| B10 | Ověř SAP/MES connectivity z produkční sítě | Klient IT | Connector healthcheck green |
| B11 | Ověř alerting funguje (kill pod, pozoruj alert) | DevOps | Alert doručen do správného kanálu |
| B12 | Backup DB + ověř restore | DevOps | Restore test successful |

### First Test Success Criteria

Co musí FUNGOVAT aby první test u klienta byl úspěch:

1. **Operátor se zeptá na postup** → agent najde správný SOP v RAG → dá srozumitelnou odpověď
2. **Process engineer se zeptá na data** → agent stáhne z SAP/MES → prezentuje v kontextu role
3. **Manažer se zeptá na stav** → agent agreguje dostupná data → executive summary
4. **Uživatel dá thumbs down** → systém zaznamená (a v Tier 2: upraví budoucí chování)
5. **Systém je stabilní** → žádné 500ky, latence < 2s, alerting funguje

```
DEPLOYMENT TIMELINE:
═══════════════════

Day -30: 11F1-5 complete (DevOps hardening)
Day -21: Klientská kickoff schůzka, získání dokumentů (B1)
Day -14: 11B-D complete (intelligence loop) ← Tier 2
         Ingestion + connector setup (B2-B4)
Day -7:  Tenant setup, RBAC, golden dataset (B5-B8)
         12A partial (basic MES monitoring)
Day -1:  Smoke tests, connectivity verification (B9-B12)
Day 0:   🚀 FIRST CLIENT TEST
Day +7:  Collect feedback, identify gaps
Day +30: Iterate based on real usage data
Day +90: Evaluate Phase 13+ based on actual demand
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

### 12G: Multi-Site + Offline Resilience

Manufacturing reality: network goes down. The platform must degrade gracefully, not die.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 12G1 | Edge agent for remote sites | HIGH | Lightweight agent for network-isolated plant locations. Local 7B inference (Ollama). |
| 12G2 | Sync protocol | HIGH | Edge ↔ central server bidirectional sync. Conflict resolution for concurrent edits. |
| 12G3 | Cached procedures | HIGH | Most-used procedures and answers cached locally. Offline RAG over local doc cache. |
| 12G4 | Queue & sync on reconnect | MEDIUM | Queries without cache stored in local queue. When network returns → processed and responses delivered. |
| 12G5 | Edge LLM fallback | MEDIUM | Central LLM unreachable → automatic fallback to local small model. Degraded quality, but functional. |
| 12G6 | Shift handoff offline | LOW | Phase 15A shift briefs generated from local cache when network is down. |
| 12G7 | Edge encryption at rest | HIGH | Factory devices get stolen. SQLite at `/data/edge.db` must be encrypted (LUKS or encrypted SQLite). (= 11F9) |

- **When**: Customer has network-isolated manufacturing sites OR unreliable factory floor connectivity
- **Effort**: Large — edge runtime, sync protocol, local inference, conflict resolution

---

## Future: Phase 13 — Enterprise Work Partner

**Goal**: Transform the platform from a reactive Q&A assistant into a **proactive work-life partner** that knows user goals, tracks progress, and makes corporate life easier. Builds on Phase 11's intelligence loop (memory, feedback, goals, learning) by adding strategic layers.

**PAI Principle**: "Unified platform for extending human capabilities with persistent memory, user goals, and continuous learning" — applied to enterprise work context.

### 13A: Goal Taxonomy + Lifecycle

Phase 11E created flat-text goals with progress notes. Phase 13A structures them for real work planning.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 13A1 | Goal categories | HIGH | Add `category` field: `project`, `kpi`, `career`, `process`, `learning`, `team` |
| 13A2 | Priority levels | HIGH | Add `priority` field: `critical`, `high`, `normal`, `low` |
| 13A3 | Deadline tracking | HIGH | Add `deadline` field + reminder logic when approaching/overdue |
| 13A4 | Parent-child goals (OKR) | MEDIUM | Objective → Key Results hierarchy. UserGoal gains `parent_id` FK |
| 13A5 | Goal templates | LOW | Pre-defined templates per category ("Reduce scrap rate", "Complete training X") |
| 13A6 | Stale goal detection | MEDIUM | Background job flags goals active >90 days without progress, nudges user |

- **Depends on**: Phase 11E (persistent goals) — ✅ already done
- **Effort**: Medium — model extension + API + UI for goal management

### 13B: Proactive Intelligence

Current agent only responds. Phase 13B makes it initiate — suggest goals, surface patterns, predict needs.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 13B1 | Pattern detection | HIGH | Analyze episodic memories: "You asked about X topic 5 times this month" |
| 13B2 | Goal suggestion engine | HIGH | From detected patterns → "Would you like to create a goal for X?" |
| 13B3 | Stale goal reminders | MEDIUM | "Your goal Y has no progress in 30 days. Want to update or revise?" |
| 13B4 | Weekly insight digest | MEDIUM | Scheduled summary: goals progress, key learnings, suggested actions |
| 13B5 | Contextual nudges | LOW | During conversation: "This relates to your goal Z — shall I log progress?" |

- **Depends on**: Phase 11B (memory injection), 11C (feedback learning), 13A (goal taxonomy)
- **Effort**: Medium — needs scheduled jobs + proactive message channel (push/notification)

### 13C: Quantitative Goal Tracking

Flat text goals can't measure "reduce scrap rate from 5% to 2%". Phase 13C adds metrics.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 13C1 | Metric-type goals | HIGH | New goal type with `target_value`, `current_value`, `unit`, `direction` (increase/decrease) |
| 13C2 | Progress timeline | HIGH | Record value changes over time as structured measurements |
| 13C3 | Auto-progress from conversations | MEDIUM | Extend 11E2: detect numeric values in responses and log to metric goals |
| 13C4 | KPI dashboard widget | MEDIUM | Visual progress bars/charts for metric goals in React UI |
| 13C5 | Threshold alerts | LOW | Notify when goal metric crosses target or regresses significantly |

- **Depends on**: Phase 11E (persistent goals), 13A (goal taxonomy)
- **Effort**: Medium — new model fields + measurement history table + UI components

### 13D: Project Context Layer

Users work on projects, not isolated queries. Phase 13D gives the agent project awareness.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 13D1 | Project model | HIGH | New entity: name, description, members, status, milestones, deadline |
| 13D2 | Goal-project linking | HIGH | Goals can belong to a project. Agent shows project-level progress |
| 13D3 | Active project context | MEDIUM | Agent knows which project the user is working on (from conversation topic or explicit selection) |
| 13D4 | Cross-project memory | MEDIUM | Learnings from project A surfaced when relevant to project B (same user) |
| 13D5 | Project timeline view | LOW | UI: Gantt-style view of milestones + linked goals + current status |
| 13D6 | Meeting/action item tracking | LOW | Extract action items from conversations, link to project goals |

- **Depends on**: Phase 11B (memory), 13A (goal taxonomy), 13C (metrics)
- **Effort**: Large — new model + API + cross-entity queries + UI components

### 13E: Organizational Knowledge (opt-in, privacy-preserving)

Each user is isolated (correct for security). Phase 13E allows controlled cross-user knowledge sharing.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 13E1 | Anonymized pattern library | MEDIUM | Aggregate successful approaches across users (with explicit tenant admin consent) |
| 13E2 | Best practices from feedback | MEDIUM | Positive feedback → anonymized best practice entries visible tenant-wide |
| 13E3 | Team context model | LOW | Team membership, roles, shared goals. Agent understands org structure |
| 13E4 | Cross-user suggestions | LOW | "Other teams solved similar problems using approach X" (no user attribution) |
| 13E5 | Compliance gate | CRITICAL | All shared knowledge passes data classification + PII check before sharing |

- **Depends on**: Phase 11C (feedback → memory), Phase 5 compliance engine
- **Effort**: Large — needs privacy-preserving aggregation + admin consent workflow + compliance integration
- **⚠️ 13E5 is a prerequisite for all other 13E items**

### Implementation Order

```
Phase 13A (Goal Taxonomy)    ←── foundation, do first
    ↓
Phase 13B (Proactive)        ←── needs structured goals
Phase 13C (Metrics)          ←── needs goal categories
    ↓
Phase 13D (Projects)         ←── needs goals + metrics
    ↓
Phase 13E (Org Knowledge)    ←── needs everything above + compliance gate
```

### The Work Partner Loop (after Phase 13)

```
                    ┌──────────────────────────────┐
                    │      USER WORKS              │
                    │  (conversations, queries)     │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  OBSERVE + REMEMBER (11B/11C) │
                    │  ├─ Episodic memories         │
                    │  ├─ Preference extraction     │
                    │  └─ Learning from feedback    │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  TRACK GOALS (11E + 13A/13C)  │
                    │  ├─ Progress detection        │
                    │  ├─ KPI metric updates        │
                    │  └─ Project milestone tracking │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  PROACTIVE INTELLIGENCE (13B)  │
                    │  ├─ Pattern detection          │
                    │  ├─ Goal suggestions           │
                    │  ├─ Stale goal reminders       │
                    │  └─ Weekly insight digest      │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │  ORGANIZATIONAL LEARN (13E)    │
                    │  ├─ Anonymized patterns        │
                    │  ├─ Best practices library     │
                    │  └─ Cross-team suggestions     │
                    └──────────────────────────────┘
```

---

## Future: Phase 14 — Hierarchical Intelligence

**Goal**: Scale the Work Partner intelligence (Phase 13) from individual users to **Department** and **Plant** levels. Same pattern — memory, goals, learning, proactive intelligence — applied at organizational scope. Creates emergent intelligence where each level sees patterns invisible to levels below.

**PAI Principle Extended**: "Unified platform for extending **organizational** capabilities" — User sees their work, Department sees team patterns, Plant sees cross-department trends.

**⚠️ Security Invariant**: All cross-level data sharing is **RBAC-governed**, **compliance-gated**, and **audit-logged**. Write operations to external systems (SAP, MES) require scope-appropriate RBAC permissions. No raw user data ever reaches Department or Plant level — only anonymized patterns.

### 14A: Scope Model (Foundation)

New organizational entities and scope-level abstraction layer.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14A1 | ScopeLevel enum | CRITICAL | `user`, `department`, `plant` — added to memory, goals, feedback models |
| 14A2 | Department entity | CRITICAL | Model: name, tenant_id, members (user_ids), manager_id, parent_plant_id |
| 14A3 | Plant entity | CRITICAL | Model: name, tenant_id, departments[], site_location, plant_manager_id |
| 14A4 | Membership model | HIGH | User ↔ Department mapping with role (member, lead, manager) |
| 14A5 | Scope-aware Goal model | HIGH | Generalize `UserGoal` → `Goal(scope_level, scope_id)` supporting all three levels |
| 14A6 | Scope-aware Memory model | HIGH | Extend `AgentMemory` with `scope_level` + `scope_id` fields |
| 14A7 | Scope RBAC policies | CRITICAL | New permissions: `dept:read`, `dept:write`, `plant:read`, `plant:write`, `system:sap:write`, `system:mes:write` per scope level |
| 14A8 | Role context profiles | HIGH | Per-role response configuration: detail level (hands-on / analytical / strategic), metric scope (station / process / business), action type (practical steps / data analysis / decision support). Injected into agent system prompt based on OIDC role claim. |
| 14A9 | Domain-based access control (DBAC) | CRITICAL | Information domains (finance, HR, operations, safety, management). User→domain membership mapping. RAG/Memory/Connector results filtered by domain ACL BEFORE reaching agent context. Agent cannot leak what it never receives. |
| 14A10 | Cross-domain query router | MEDIUM | When query spans domains user lacks full access to: answer with accessible data, note what's outside user's scope without revealing content. Graceful degradation, not hard block. |

- **Depends on**: Phase 4 (RBAC), Phase 11E (goals), Phase 13A (goal taxonomy)
- **Effort**: Medium-High — model extensions + migration + RBAC policy expansion + domain ACL engine

### 14B: Department Intelligence

Department-level agent with aggregated memory, team goals, and pattern detection.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14B1 | Department Advisor agent | HIGH | New specialist: sees dept-level memory, tracks dept goals, answers dept-wide questions |
| 14B2 | Dept-level memory store | HIGH | Aggregated memories from team members (post-anonymization). Separate `agent_id` per department. |
| 14B3 | Dept goal management | HIGH | Department OKRs: quality targets, project milestones, team KPIs |
| 14B4 | Team pattern detection | MEDIUM | Background job: analyze user-level patterns across department members, surface common themes |
| 14B5 | Dept weekly digest | MEDIUM | Scheduled summary: goal progress, common issues, team insights → dept manager |
| 14B6 | Dept-scoped connector writes | HIGH | SAP/MES write ops with `dept:write` RBAC — dept leads can approve batch operations |

- **Depends on**: Phase 14A (scope model), **Phase 14D (compliance gates — PREREQUISITE)**
- **Effort**: Large — new agent, aggregation pipeline, RBAC integration with connectors

### 14C: Plant Intelligence

Plant-level agent with cross-department visibility and strategic insights.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14C1 | Plant Advisor agent | HIGH | New specialist: sees plant-level memory, tracks plant KPIs, cross-dept correlation |
| 14C2 | Plant-level memory store | HIGH | Aggregated from departments (double-anonymized). Statistical patterns only. |
| 14C3 | Plant KPI dashboard | HIGH | OEE, scrap rate, throughput, downtime — real-time from MES + aggregated goal progress |
| 14C4 | Cross-dept pattern detection | MEDIUM | "Quality and Maintenance are solving the same root cause independently" |
| 14C5 | Plant monthly digest | MEDIUM | Strategic summary for plant management: trends, risks, opportunities |
| 14C6 | Plant-scoped connector writes | HIGH | SAP/MES write ops with `plant:write` RBAC — plant manager approval for plant-wide changes |
| 14C7 | Anomaly escalation | MEDIUM | Auto-detect when dept-level anomalies correlate across departments → alert plant manager |

- **Depends on**: Phase 14A (scope model), **Phase 14D (compliance gates — PREREQUISITE)**
- **Effort**: Large — cross-dept aggregation, correlation engine, strategic agent role

### 14D: Compliance Gates (PREREQUISITE for 14B + 14C)

Privacy-preserving data flow between scope levels. **Must complete before 14B and 14C.**

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14D1 | SharingPolicy model | CRITICAL | Defines rules per source→target scope transition: data class max, consent, min sources, anonymization level |
| 14D2 | User opt-in workflow | CRITICAL | Users explicitly consent to dept-level aggregation. Granular: per memory type, per goal type |
| 14D3 | PII anonymization pipeline | CRITICAL | Scrub personal identifiers before data crosses scope boundary. Integrated with existing PII sanitizer. |
| 14D4 | k-Anonymity enforcement | HIGH | Minimum 3 distinct sources required before any pattern surfaces at dept level. Minimum 3 departments for plant level. |
| 14D5 | Data classification gate | CRITICAL | Only Class I/II data crosses to dept level. Only Class I to plant level. Class III/IV never leaves user scope. |
| 14D6 | Cross-level audit trail | HIGH | Every data aggregation event logged: source scope, target scope, policy applied, anonymization method |
| 14D7 | Tenant admin approval | HIGH | Tenant admin must explicitly enable and configure sharing policies per department/plant |
| 14D8 | RBAC for external writes | CRITICAL | SAP/MES write permissions scoped per level: user can write own records, dept lead can batch-approve, plant manager can plant-wide. HITL approval workflow integrated. |

- **Depends on**: Phase 14A (scope model), Phase 5 (compliance engine), Phase 4 (RBAC + connectors)
- **Effort**: Large — critical security infrastructure, must be thoroughly tested before enabling 14B/14C
- **⚠️ This is the hardest sub-phase. Get it right or the whole hierarchy is a liability.**

### 14E: Cascade Goals (OKR Top-Down)

Goals flow top-down (Plant → Dept → User) while progress rolls up bottom-up.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14E1 | Goal cascade linking | HIGH | Plant goal → spawns dept sub-goals → spawns user tasks. Parent-child across scope levels. |
| 14E2 | Progress roll-up | HIGH | User goal progress automatically aggregates to dept goal, dept to plant. Real-time. |
| 14E3 | Cascade visualization | MEDIUM | UI: tree view showing Plant → Dept → User goal hierarchy with progress at each level |
| 14E4 | Misalignment detection | MEDIUM | Flag when user goals don't align with dept/plant objectives. Suggest alignment. |
| 14E5 | OKR cycle management | LOW | Quarterly/annual goal cycle: set plant targets, cascade, track, review, close |

- **Depends on**: Phase 14A (scope model), Phase 13A (goal taxonomy), Phase 13C (metrics)
- **Effort**: Medium — linking + aggregation + UI

### 14F: Cross-Level Insights

Higher levels proactively push insights to lower levels (the intelligence flows both ways).

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14F1 | Downward insight channel | HIGH | Plant agent can push anonymized insight to dept agents. Dept agents can push to user agents. |
| 14F2 | Context-aware suggestions | MEDIUM | User asks about problem X → agent checks if dept/plant level has relevant pattern → surfaces it |
| 14F3 | Best practice propagation | MEDIUM | Positive pattern at one dept → suggested (anonymized) to other depts facing similar challenge |
| 14F4 | Risk propagation | HIGH | Plant-level risk detected → relevant dept agents notified → affected user agents get context |
| 14F5 | Insight RBAC | CRITICAL | Downward insights also comply with data classification + RBAC. No information leakage in reverse direction. |

- **Depends on**: Phase 14B (dept intelligence), Phase 14C (plant intelligence), Phase 14D (compliance gates)
- **Effort**: Medium — message routing + compliance integration

### 14G: Knowledge Continuity (Institutional Memory Preservation)

When experienced employees leave (retirement, transfer, resignation), decades of accumulated knowledge risk being lost. Phase 14G preserves institutional knowledge through controlled, opt-in extraction into the organizational hierarchy.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 14G1 | Knowledge legacy extraction | HIGH | Opt-in workflow: departing employee consents → AI extracts expert patterns from their user-level memory → anonymizes → stores in department knowledge base. |
| 14G2 | Expert pattern tagging | HIGH | Identify "tribal knowledge" — solutions, workarounds, and insights unique to this person that no one else on the team has. Tag as high-importance. |
| 14G3 | Knowledge gap analysis | MEDIUM | Compare departing employee's knowledge base with remaining team. Identify areas where knowledge will be lost if not transferred. |
| 14G4 | Mentoring mode | MEDIUM | Before departure: platform suggests 1:1 knowledge transfer sessions with successor, based on identified knowledge gaps. Generates session agendas. |
| 14G5 | Institutional memory injection | MEDIUM | Department agent learns the preserved patterns and can surface them: "Zkušení kolegové doporučovali při tomto problému..." No attribution to individual. |
| 14G6 | Knowledge continuity metrics | LOW | Track: how much institutional knowledge was preserved, how often it's recalled, impact on onboarding time for replacements. |
| 14G7 | Compliance gate for legacy extraction | CRITICAL | All extraction passes Phase 14D compliance gates: PII scrub, data classification, explicit consent, audit trail. Employee can review and redact before transfer. |

- **Depends on**: Phase 14A (scope model), Phase 14D (compliance gates), Phase 14B (department intelligence)
- **Effort**: Medium — extraction pipeline + gap analysis + consent workflow
- **⚠️ 14G7 is a prerequisite for all other 14G items — no extraction without compliance**

### Implementation Order

```
Phase 13 (Work Partner) ←── MUST complete first
    ↓
Phase 14A (Scope Model + RBAC + DBAC)  ←── foundation entities + domain access control
    ↓
Phase 14D (Compliance Gates)    ←── HARD PREREQUISITE — no data sharing without this
    ↓
Phase 14B (Department)  ║  Phase 14C (Plant)  ←── can run in parallel after 14D
    ↓                       ↓
    └───────┬───────────────┘
            ↓
Phase 14E (Cascade Goals)  ←── needs both dept + plant
    ↓
Phase 14F (Cross-Level Insights)  ←── capstone
    ↓
Phase 14G (Knowledge Continuity)  ←── needs dept intelligence + compliance gates
```

### The Hierarchical Intelligence Loop (after Phase 14)

```
┌─────────────────────────────────────────────────────────┐
│  👤 USER LEVEL                                           │
│  Work Partner (Phase 13)                                │
│  ├─ Personal memory, goals, learning                    │
│  ├─ Project tracking, KPIs, career                     │
│  └─ SAP/MES writes: own records (user RBAC)            │
└──────────┬───────────────────────┬──────────────────────┘
           │ opt-in + PII scrub    │ cascaded goals
           │ Class I/II only       │ (top-down)
           │ k≥3 anonymity         │
           ▼                       │
┌──────────────────────────────────┴──────────────────────┐
│  🏢 DEPARTMENT LEVEL                                     │
│  Department Advisor (Phase 14B)                         │
│  ├─ Aggregated team patterns                            │
│  ├─ Dept OKRs, team KPIs                               │
│  ├─ Common issue detection                              │
│  └─ SAP/MES writes: batch ops (dept:write RBAC)        │
└──────────┬───────────────────────┬──────────────────────┘
           │ k≥3 depts             │ cascaded goals
           │ Class I only          │ (top-down)
           │ statistical only      │
           ▼                       │
┌──────────────────────────────────┴──────────────────────┐
│  🏭 PLANT LEVEL                                          │
│  Plant Advisor (Phase 14C)                              │
│  ├─ Cross-dept correlation                              │
│  ├─ Plant KPIs (OEE, throughput, scrap)                │
│  ├─ Strategic trend detection                           │
│  └─ SAP/MES writes: plant-wide (plant:write RBAC)      │
└─────────────────────────────────────────────────────────┘

Data flows UP (anonymized patterns)  │  Goals flow DOWN (OKR cascade)
Insights flow BOTH WAYS (with RBAC)  │  Writes scoped per level (RBAC)
```

---

### Phase 15 — Operational Intelligence

> **Vision:** Make the platform operationally aware — shift context flows seamlessly between workers, and the platform itself measures its own business impact. One chat interface, enriched by all scope levels invisibly.

**Architectural Decision — Single-Chat UX Principle:**
Users interact through ONE chat. The scope levels (user/department/plant from Phase 14) are invisible infrastructure. When a user asks a question, the platform combines personal memory, department patterns, and plant-level insights into a single enriched answer. No separate dashboards per level. No "switch scope" UI. The intelligence layers enrich responses transparently.

#### 15A: Shift Handoff Intelligence

Auto-generated shift briefs from the previous shift's conversations and actions. Workers start their shift with full context of what happened, what's pending, and what needs attention — without manual handoff notes.

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 15A1 | Shift context model | HIGH | Data model for shift periods (start/end, line, area, team). Link conversations + actions to shift windows. |
| 15A2 | Shift brief generator | HIGH | End-of-shift aggregation: open issues, decisions made, escalations, handoff notes. LLM-summarized from conversation history. |
| 15A3 | Incoming shift injection | HIGH | New shift user gets previous shift brief injected into system prompt context. Seamless — no manual action needed. |
| 15A4 | Multi-scope handoff | MEDIUM | Brief enriched with dept patterns (Phase 14B) and plant alerts (Phase 14C) relevant to that line/area. |
| 15A5 | Shift continuity tracking | MEDIUM | Track which issues from previous shift were resolved vs still open. Cross-shift issue lifecycle. |
| 15A6 | Handoff quality metrics | LOW | Measure: was the brief useful? Did incoming shift ask fewer repeat questions? Feedback loop on brief quality. |

- **Depends on**: Phase 11B (memory injection), Phase 13B (proactive intelligence), Phase 14 (scope levels for multi-scope handoff)
- **Effort**: Medium — shift model + aggregation + prompt injection

#### 15B: Platform ROI Measurement

The platform measures its own business impact. Meta-intelligence: how much value does the AI agent system actually deliver?

| # | Item | Priority | Description |
|---|------|----------|-------------|
| 15B1 | Resolution time tracking | HIGH | Measure time-to-resolution for issues handled with vs without platform assistance. Before/after comparison. |
| 15B2 | Adoption metrics | HIGH | Active users, session frequency, conversation depth, feature utilization. Per user/dept/plant scope. |
| 15B3 | Agent effectiveness scoring | MEDIUM | Per-agent quality: feedback ratings, first-response accuracy, escalation rate, memory hit rate. |
| 15B4 | Business impact KPIs | HIGH | Connect platform usage to business metrics: OEE improvement, scrap reduction, downtime reduction (correlation, not causation). |
| 15B5 | ROI dashboard | MEDIUM | Executive-facing dashboard: cost of platform vs measured business impact. Auto-generated monthly reports. |
| 15B6 | Self-improvement loop | LOW | Platform identifies its own weak areas (low-rated topics, high-escalation agents) and flags for improvement. |

- **Depends on**: Phase 11C (feedback→learning), Phase 14 (multi-scope metrics for aggregation)
- **Effort**: Medium — metrics collection + dashboard + reporting

#### Future Option: Voice-First Interface

> **Note:** Voice-based interaction (speech-to-text input, text-to-speech responses) is a natural evolution for manufacturing floor use. Hands-free operation near machinery, noisy environments with directional audio, quick verbal queries during physical work. Not scheduled as a full phase — will be evaluated after Phase 15 based on user demand and hardware availability (headsets, floor terminals).

### Implementation Order

```
Phase 14 (Hierarchical Intelligence) ←── MUST complete first (scope levels needed)
    ↓
Phase 15A (Shift Handoff)  ║  Phase 15B (Platform ROI)  ←── can run in parallel
    ↓                           ↓
    └───────┬───────────────────┘
            ↓
    Voice-First (future, if demand warrants)
```

### Future: Phase 16 — Enterprise Discovery & Onboarding Automation

**Goal**: Automated client environment discovery and system integration setup. When deploying at a new client site, the platform can crawl their existing data landscape (SharePoint, Google Drive, network shares), classify documents, map data models to platform entities, and generate connector configurations for SAP/ERP/MES systems.

**Why**: Every client deployment starts with the same manual discovery: audit their file mess, understand their data, map it to our platform. This is repetitive, error-prone, and time-consuming. Automating it = faster onboarding = more clients.

**Subphases** (to be refined after 2-3 real client deployments):

| ID | Feature | Priority | Description |
|----|---------|----------|-------------|
| 16A | Environment Discovery | HIGH | Crawl client SharePoint/Drive via MS Graph/Google Drive API. Classify files (contracts, processes, templates, archive, dead files). Generate discovery report. |
| 16B | Data Model Mapping | HIGH | Map client data structures to platform entities. SAP OData schema discovery. Field-level mapping with transformation rules. |
| 16C | Connector Auto-Config | MEDIUM | Generate connector configurations for SAP (OData v2), MES (REST), and other systems based on discovered schema. Connection strings, auth, retry policies. |
| 16D | Gap Analysis | MEDIUM | Compare discovered data against platform requirements. Identify what's missing, what needs migration, what can be left behind. |
| 16E | Migration Assistant | LOW | Semi-automated data migration from legacy systems. Human-in-the-loop approval for each batch. |

**Implementation approach**: MCP servers for external system access (SharePoint MCP, SAP OData MCP). AI-assisted classification and mapping. Human review checkpoints for all data decisions.

**Timing**: Do NOT implement speculatively. After 2-3 real client deployments, extract the repeatable pattern from manual discovery notes, then build the skill.

**Depends on**: Phase 14 (scope model for multi-tenant), Phase 5 (compliance engine for data classification)

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
PHASES 1-10          PHASE 11              PHASE 13               PHASE 14               PHASE 15
═══════════          ════════              ════════               ════════               ════════

Agent Orchestrator   Agent Orchestrator    Agent Orchestrator     Agent Orchestrator     Agent Orchestrator
├─ 5 specialists     ├─ 5 specialists      ├─ 5 specialists       ├─ 5 specialists       ├─ 5 specialists
├─ Composer     →    ├─ Auto-Composer(11D) ├─ Auto-Composer       ├─ Auto-Composer       ├─ Auto-Composer
├─ Goal Planner →    ├─ Goal Planner(11E)→ ├─ Goal (OKR,13A/C) → ├─ Goal (cascade,14E)  ├─ Goal (cascade)
├─ Thinking tools    ├─ Thinking tools     ├─ Thinking tools      ├─ Thinking tools      ├─ Thinking tools
├─ Model router      ├─ Model router(11D3) ├─ Model router        ├─ Model router        ├─ Model router
│                    │                     │                      │                      │
├─ Memory (unused)→  ├─ Memory (11B)    →  ├─ Memory (+project) → ├─ Memory (3 scopes) → ├─ Memory (+shift ctx)
├─ Feedback     →    ├─ Feedback→Learn(11C)├─ Learn→Org KB(13E) → ├─ Learn→Dept→Plant →  ├─ Learn (all levels)
│                    │                     │                      │                      │
│                    │                     ├─ Proactive (13B)  →  ├─ Proactive (3 lvls)  ├─ Proactive (+shift)
│                    │                     │                      ├─ Compliance (14D)    ├─ Compliance
│                    │                     │                      ├─ Cross-Level (14F)   ├─ Cross-Level
│                    │                     │                      │                      ├─ Shift Handoff (15A)
│                    │                     │                      │                      ├─ ROI Measurement(15B)
│                    │                     │                      │                      │
│ SCOPE: per-user    │ SCOPE: per-user     │ SCOPE: per-user  →   │ SCOPE: u│dept│plant  │ SCOPE: u│d│p + shift
│                    │                     │                      │ RBAC: scoped writes   │ + self-measurement
└─ Reactive only     └─ Learns per user    └─ Proactive partner   └─ Organizational brain└─ Ops-aware platform

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

*Unified Roadmap v4.9 — First Deployment Checkpoint, offline resilience (12G), knowledge continuity (14G) (2026-02-19)*
*Previous: v4.8 (role-aware relevance, Phase 16), v4.7 (Operational Intelligence), v4.6 (Hierarchical Intelligence), v4.5 (Work Partner), v4.4 (DevOps hardening), v4.0 (unification), v3.0 (V2 merge)*
*All technical debt items from original tracker: RESOLVED*
