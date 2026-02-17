# Enterprise Agent Platform v3 — Development Guidelines

## Project Context

Production-grade multi-tenant AI agent orchestration platform.
**Author:** Jan Střechovský (Honza)
**Stack:** Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL 16 + pgvector / Redis / React 19 / Helm
**Scale:** 207 Python src files (~57K lines) + 81 test files (~27K lines) + 14 migrations
**Status:** Phases 1-10 complete. Phase 11 planning in progress.

---

## Agent Delegation Rules (Opus / Sonnet)

### Opus (main orchestrator, architecture, security review)
- Architecture decisions and design
- Security review of critical code (auth, tenant isolation, PII, HITL)
- Complex debugging and root cause analysis
- Code review of Sonnet output
- Final verification before commits

### Sonnet (implementation workhorse)
- Feature implementation via Task tool (subagent_type=Engineer)
- Test writing and expansion
- Boilerplate, migrations, config files
- Documentation generation
- CI/CD pipeline YAML
- Helm chart modifications

### Delegation Pattern
```
Opus: plan + review + commit
Sonnet agents (parallel): implement + test
Opus: verify + push
```

- Launch multiple Sonnet agents in parallel for independent tasks
- Opus reviews all Sonnet output before committing
- Security-critical code (auth, tenant isolation, PII) requires Opus review

---

## Git Commit Conventions

### Format
```
<type>: <description> (Phase <N><letter>)

[optional body with details]

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```

### Types
- `feat:` — New feature or capability
- `fix:` — Bug fix
- `refactor:` — Code restructuring without behavior change
- `test:` — Test additions or improvements
- `ci:` — CI/CD pipeline changes
- `docs:` — Documentation only
- `chore:` — Maintenance (deps, config, cleanup)
- `security:` — Security hardening

### Rules
- Commit after each completed phase item (e.g., 7A, 7B)
- Never commit secrets, .env files, or credentials
- Always run tests before committing
- Use `git config user.name "Jan Strechovský"` and noreply email
- Temp workflow removal pattern: remove workflows → push → restore (for large pushes)

---

## Coding Standards

### Python
- **Formatter/Linter:** ruff (line-length=100, target py312)
- **Type checking:** mypy (strict mode)
- **Security:** bandit for SAST
- **Tests:** pytest + pytest-asyncio (asyncio_mode=auto)
- **ALL database queries** MUST use `apply_tenant_filter()` — no exceptions
- **ALL write operations** MUST create audit log entries
- **NO hardcoded secrets** — environment variables only
- **PII** must be redacted before logging

### Frontend
- React 19 + TypeScript + Vite + Tailwind
- SSE streaming for chat responses
- shadcn/ui components

### Docker
- PostgreSQL 16 + pgvector, Redis 7, Ollama, LiteLLM
- `docker-compose.yml` (prod) + `docker-compose.dev.yml` (dev with Ollama)

---

## Completed Phases

### Phase 7: Production Readiness ✅
| 7A | CI/CD Pipeline | DONE | GH Actions: lint, typecheck, test, security scan, Docker build |
| 7B | Persist in-memory stores | DONE | Budget, metrics, plans → PostgreSQL tables |
| 7C | Wire write execution | DONE | Connect execute() to SAP/MES connectors |
| 7D | Email/notification | DONE | Real SMTP or webhook for approval notifications |
| 7E | Integration tests | DONE | pytest against Docker PostgreSQL |
| 7F | Security scan in CI | DONE | Bandit + pip-audit + Trivy |

### Phase 8: Enterprise Polish ✅
| 8A | Grafana dashboards | DONE | LLM Performance, Agent Ops, Tenant Budgets + Prometheus stack |
| 8B | Runbook & architecture docs | DONE | RUNBOOK.md (1185 lines) + ARCHITECTURE.md (625 lines) |
| 8C | Load testing | DONE | Locust + k6 scripts with staged scenarios |
| 8D | Backup & restore | DONE | pg_dump/restore automation with verification |
| 8E | Log aggregation | DONE | Loki + Promtail config for structured JSON logs |

### Phase 9: Critical Fixes ✅
| 9A | Fix broken imports | DONE | src.auth.jwt → dependencies, starlette.dataclasses fix |
| 9B | MFA validation | DONE | Real TOTP with pyotp, configurable enable/disable |
| 9C | ConnectorRegistry wiring | DONE | SAP/MES config from env, 19 new tests |
| 9D | Escalation notifications | DONE | Fire-and-forget email/webhook on escalation |
| 9E | Operations DB persistence | DONE | WriteOperationRecord + migration 014 |

### Phase 10: Feature Completion ✅
| 10A | Fine-tuning job queue | DONE | PersistentFineTuningManager, migration 015, 25 tests |
| 10B | Compliance dashboard data | DONE | Real DB queries for all 6 placeholder values |
| 10D | Test suite fixes | DONE | Auth dependency tests updated for Request-based API |
| 10E | Code cleanup | DONE | 80+ Phase comments, TODO/FIXME, stale refs removed (60+ files) |

---

## Current Roadmap — Phase 11: Structured Reasoning Experience

Based on comprehensive PAI Algorithm v0.2.24 gap analysis. See `docs/PAI_GAP_ANALYSIS.md` for full comparison.

### Phase 11A: Core Reasoning (CRITICAL)

| # | Item | Priority | Status | Description |
|---|------|----------|--------|-------------|
| 11A1 | ISC criteria system | CRITICAL | TODO | Decompose requests into binary-testable success criteria before execution. PostgreSQL-backed. |
| 11A2 | 7-phase reasoning loop | CRITICAL | TODO | Expand OBSERVE→THINK→VERIFY to OBSERVE→THINK→PLAN→BUILD→EXECUTE→VERIFY→LEARN |
| 11A3 | ISC-driven verification | CRITICAL | TODO | Verify agent output against ISC criteria with evidence (not just reasoning consistency) |
| 11A4 | Reverse-engineer intent | HIGH | TODO | Enhance OBSERVE: what user asked + implied + does NOT want |
| 11A5 | Thinking Tools Assessment | HIGH | TODO | Mandatory assessment of all thinking tools per request (opt-OUT, not opt-IN) |

### Phase 11B: Agent Collaboration (CRITICAL)

| # | Item | Priority | Status | Description |
|---|------|----------|--------|-------------|
| 11B1 | Auto-composition selection | CRITICAL | TODO | Auto-select multi-agent patterns based on ISC complexity (Primary/Support/Verify) |
| 11B2 | Two-pass capability selection | HIGH | TODO | Pass 1: lightweight intent → Pass 2: validate against ISC after OBSERVE |
| 11B3 | Escalation composition pattern | HIGH | TODO | Retry with higher model tier on failure (LIGHT→STANDARD→HEAVY) |
| 11B4 | Fan-in composition pattern | MEDIUM | TODO | Merge heterogeneous pipeline results (SAP + MES + docs → unified report) |

### Phase 11C: Streaming UX (CRITICAL)

| # | Item | Priority | Status | Description |
|---|------|----------|--------|-------------|
| 11C1 | Streaming reasoning phases | CRITICAL | TODO | SSE events at each phase transition with structured metadata |
| 11C2 | Phase progress metadata | HIGH | TODO | OBSERVE emits facts count, THINK emits capabilities, VERIFY emits ISC pass/fail |
| 11C3 | Reasoning trace visualization | MEDIUM | TODO | Expandable timeline UI component for full reasoning trace |

### Phase 11D: Learning & Memory

| # | Item | Priority | Status | Description |
|---|------|----------|--------|-------------|
| 11D1 | LEARN phase implementation | HIGH | TODO | Post-VERIFY reflection: what worked, what missed, stored as lesson memory |
| 11D2 | Semantic memory extraction | HIGH | TODO | Extract domain facts and preferences (not just episodic) from each turn |
| 11D3 | Cross-session learning | MEDIUM | TODO | Aggregate episodic memories into procedural patterns (background job) |
| 11D4 | Agent eval framework | MEDIUM | TODO | Golden dataset + automated scoring for reasoning quality |

### Phase 11E: Quality Assurance

| # | Item | Priority | Status | Description |
|---|------|----------|--------|-------------|
| 11E1 | Confidence-based escalation | HIGH | TODO | Auto-flag for human review when ISC criteria fail or confidence < 60% |
| 11E2 | Thinking tools in quality gates | MEDIUM | TODO | Use Council/FirstPrinciples (not just RedTeam) in quality gate checks |
| 11E3 | Science thinking tool | MEDIUM | TODO | Hypothesis→Test→Analyze cycles for troubleshooting scenarios |
| 11E4 | BeCreative thinking tool | MEDIUM | TODO | Generate 5 diverse approaches via extended thinking |

### Implementation Order
```
11A1 (ISC) → 11A2 (7-phase loop) → 11A3 (ISC verification) → 11B1 (auto-composition) → 11C1 (streaming)
```

---

## Key File Paths

| Purpose | Path |
|---------|------|
| App entry | `src/main.py` |
| Agent runtime | `src/agent/runtime.py` |
| Reasoning engine | `src/agent/reasoning.py` |
| RAG retrieval | `src/rag/retrieve.py` |
| SAP connector | `src/connectors/sap.py` |
| Auth/OIDC | `src/auth/oidc.py` |
| PII redaction | `src/core/pii.py` |
| Model router | `src/agent/model_router/router.py` |
| HITL workflow | `src/operations/write_framework.py` |
| Compliance export | `src/compliance/audit_export.py` |
| Helm chart | `deploy/helm/` |
| Migrations | `alembic/versions/` |
| Tests | `tests/` |
