# Phase 4A: Advanced Orchestration

**Status:** Implemented
**Date:** 2026-02-16
**Python:** 3.12
**Framework:** FastAPI + SQLAlchemy 2.0 async

---

## Overview

Phase 4A introduces advanced multi-agent orchestration capabilities:

1. **Composition Patterns** - Four core patterns for combining agents
2. **Goal Planner** - LLM-based task decomposition with DAG execution
3. **Agent Memory** - Cross-agent context persistence
4. **Plans API** - REST endpoints for plan creation and approval

---

## Architecture

### Component Structure

```
src/agent/composition/
├── __init__.py              # Public API exports
├── patterns.py              # Composition pattern executors (400 LOC)
├── goal_planner.py          # Task graph planner (350 LOC)
└── agent_memory.py          # Memory store (250 LOC)

src/api/
└── plans.py                 # Plans REST API (150 LOC)

alembic/versions/
└── 002_add_agent_memory.py  # Database migration
```

### Key Classes

- **CompositionPattern** - Enum of available patterns
- **PipelineExecutor** - Sequential execution (A → B → C)
- **FanOutExecutor** - Parallel execution with synthesis
- **GateExecutor** - Production + verification loop
- **TDDLoopExecutor** - Builder ↔ Tester cycle
- **GoalPlanner** - Task decomposition and DAG execution
- **AgentMemoryStore** - Persistent context storage

---

## Composition Patterns

### 1. Pipeline Pattern

**Shape:** A → B → C

**Use Case:** Sequential domain expertise handoff

**Example:**
```python
from src.agent.composition import PipelineExecutor

executor = PipelineExecutor(db, settings, llm_client, tool_gateway)

result = await executor.execute(
    stages=[explore_agent, architect_agent, engineer_agent],
    message="Build a new authentication service",
    context=agent_context,
)

# Stage 1: Explore analyzes codebase
# Stage 2: Architect receives Explore's findings
# Stage 3: Engineer receives Architect's design
```

**Properties:**
- Each stage receives previous stage's output as context
- Stops on first failure
- Full audit trail per stage
- Total duration tracked

### 2. FanOut Pattern

**Shape:** → [A, B, C] → synthesize

**Use Case:** Multiple perspectives on same problem

**Example:**
```python
from src.agent.composition import FanOutExecutor

executor = FanOutExecutor(db, settings, llm_client, tool_gateway)

result = await executor.execute(
    agents=[claude_researcher, gemini_researcher, grok_researcher],
    message="Research best practices for distributed tracing",
    context=agent_context,
)

# All agents run concurrently
# LLM synthesizes unified response
```

**Properties:**
- Agents run in parallel (asyncio.gather)
- LLM-based synthesis of results
- Identifies consensus and conflicts
- Graceful failure handling

### 3. Gate Pattern

**Shape:** Agent → Verifier → Pass or Retry

**Use Case:** Quality gates, Export Control Policy verification

**Example:**
```python
from src.agent.composition import GateExecutor

executor = GateExecutor(db, settings, llm_client, tool_gateway)

result = await executor.execute(
    agent=engineer_agent,
    verifier=qa_agent,
    message="Implement user authentication",
    context=agent_context,
    max_retries=3,
)

# Engineer produces code
# QA verifies quality
# If fails, Engineer fixes with feedback
# Iterate until pass or max_retries
```

**Properties:**
- Agent → Verifier loop
- Feedback incorporated on retry
- Pass/fail detection from verifier response
- Max retries configurable

### 4. TDD Loop Pattern

**Shape:** Builder ↔ Tester

**Use Case:** Test-Driven Development, iterative refinement

**Example:**
```python
from src.agent.composition import TDDLoopExecutor

executor = TDDLoopExecutor(db, settings, llm_client, tool_gateway)

result = await executor.execute(
    builder=engineer_agent,
    tester=qa_agent,
    message="Build login form with validation",
    context=agent_context,
    max_iterations=5,
)

# Engineer writes code
# QA runs tests
# If tests fail, Engineer fixes
# Iterate until tests pass
```

**Properties:**
- Build → Test → Fix cycle
- Test feedback incorporated
- Stops on test pass or max_iterations
- Full iteration history tracked

---

## Goal Planner

### Task Decomposition

The GoalPlanner uses an LLM to break down high-level goals into dependency graphs:

```python
from src.agent.composition.goal_planner import GoalPlanner

planner = GoalPlanner(db, settings, llm_client, tool_gateway, registry)

graph = await planner.decompose(
    goal="Deploy new authentication service with OAuth2",
    user_role=UserRole.OPERATOR,
)

# Returns TaskGraph with nodes and dependencies
```

**Generated Graph Example:**
```
Task 1: Security review of OAuth2 implementation
  Agent: security_agent
  Dependencies: []

Task 2: Architecture design
  Agent: architect_agent
  Dependencies: [Task 1]

Task 3: Backend implementation
  Agent: engineer_agent
  Dependencies: [Task 2]

Task 4: Frontend integration
  Agent: frontend_agent
  Dependencies: [Task 2]

Task 5: Integration testing
  Agent: qa_agent
  Dependencies: [Task 3, Task 4]

Task 6: Deployment
  Agent: devops_agent
  Dependencies: [Task 5]
```

### Graph Validation

```python
# Validate DAG structure
is_valid = planner.validate_graph(graph)

# Checks:
# - No cycles
# - All dependencies exist
# - All agents are valid
```

### Graph Execution

```python
# Execute with parallelism
completed_tasks = await planner.execute_graph(graph, context)

# Execution order:
# Wave 1: Task 1 (no dependencies)
# Wave 2: Task 2 (depends on 1)
# Wave 3: Task 3, Task 4 (parallel - both depend on 2)
# Wave 4: Task 5 (depends on 3 and 4)
# Wave 5: Task 6 (depends on 5)
```

**Properties:**
- Topological sort determines order
- Tasks with no pending dependencies run in parallel
- Dependency results passed as context
- Stops on task failure (or continues with partial results)

---

## Agent Memory

### Memory Storage

Agents can store and retrieve context across conversations:

```python
from src.agent.composition.agent_memory import AgentMemoryStore

memory_store = AgentMemoryStore(db, llm_client)

# Store a memory
await memory_store.store(
    agent_id="engineer",
    tenant_id=user.tenant_id,
    key="user_prefers_typescript",
    value="User consistently requests TypeScript over JavaScript",
    metadata={"confidence": 0.9},
)

# Retrieve specific memory
memory = await memory_store.retrieve(
    agent_id="engineer",
    tenant_id=user.tenant_id,
    key="user_prefers_typescript",
)

print(memory.value)  # "User consistently requests..."
print(memory.access_count)  # Incremented on each retrieval
```

### Relevance Search

```python
# Search by relevance to query
memories = await memory_store.search(
    agent_id="engineer",
    tenant_id=user.tenant_id,
    query="What language should I use for this project?",
    limit=3,
)

# Returns top 3 most relevant memories
# LLM scores relevance 0.0-1.0
for memory in memories:
    print(f"{memory.key}: {memory.value}")
    print(f"Relevance: {memory.metadata['relevance_score']}")
```

### Context Injection

```python
# Get formatted context for agent prompt
context = await memory_store.get_context_for_agent(
    agent_id="engineer",
    tenant_id=user.tenant_id,
    query="Build a web service",
    max_memories=3,
)

# Returns:
# "Relevant context from previous interactions:
#
# - user_prefers_typescript: User consistently requests TypeScript
# - last_framework: Recent project used FastAPI with Python
# - coding_style: User prefers functional programming patterns"
```

### Cleanup

```python
# Remove memories older than 90 days
deleted_count = await memory_store.cleanup(older_than_days=90)

print(f"Deleted {deleted_count} old memories")
```

**Properties:**
- Tenant-scoped (isolation guaranteed)
- LLM-based relevance scoring
- Access count tracking
- Metadata support (JSON)

---

## Plans API

### Endpoints

#### POST /api/v1/plans

Create an execution plan from a goal.

**Request:**
```json
{
  "goal": "Deploy new authentication service with OAuth2 support",
  "context": "Existing system uses JWT tokens"
}
```

**Response:**
```json
{
  "plan_id": "uuid",
  "goal": "Deploy new authentication service...",
  "status": "draft",
  "created_at": "2026-02-16T12:00:00Z",
  "tasks": [
    {
      "id": "task_1",
      "description": "Security review",
      "agent_id": "security_agent",
      "dependencies": [],
      "status": "pending"
    },
    ...
  ],
  "execution_plan": "Step 1: task_1\n  Description: Security review...",
  "metadata": {}
}
```

**Permissions:** OPERATOR or higher

#### GET /api/v1/plans/{plan_id}

Get plan details.

**Response:** Same as create response, with updated task statuses

**Permissions:** User must own plan (same tenant)

#### POST /api/v1/plans/{plan_id}/approve

Approve a plan for execution.

**Request:**
```json
{
  "comment": "Reviewed and approved"
}
```

**Response:** Updated plan with status="approved"

**Permissions:** OPERATOR or higher

#### POST /api/v1/plans/{plan_id}/reject

Reject a plan.

**Request:**
```json
{
  "comment": "Security review needed first"
}
```

**Response:** Updated plan with status="rejected"

**Permissions:** OPERATOR or higher

#### GET /api/v1/plans/{plan_id}/status

Get execution status.

**Response:**
```json
{
  "plan_id": "uuid",
  "status": "executing",
  "progress": {
    "tasks": {
      "task_1": {"status": "complete", "description": "...", "agent_id": "..."},
      "task_2": {"status": "running", "description": "...", "agent_id": "..."}
    }
  },
  "completed_tasks": 3,
  "total_tasks": 6
}
```

**Permissions:** User must own plan (same tenant)

---

## Database Schema

### agent_memory Table

```sql
CREATE TABLE agent_memory (
    id UUID PRIMARY KEY,
    agent_id VARCHAR(128) NOT NULL,
    tenant_id UUID NOT NULL,
    key VARCHAR(256) NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    access_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT
);

CREATE INDEX idx_agent_memory_lookup ON agent_memory (agent_id, tenant_id, key);
CREATE INDEX idx_agent_memory_created ON agent_memory (created_at);
```

**Isolation:** All queries MUST filter by tenant_id

---

## Usage Examples

### Example 1: Pipeline for Code Generation

```python
# Define pipeline stages
explore = registry.get("explore_agent")
architect = registry.get("architect_agent")
engineer = registry.get("engineer_agent")

# Execute pipeline
pipeline = PipelineExecutor(db, settings, llm_client, tool_gateway)
result = await pipeline.execute(
    stages=[explore, architect, engineer],
    message="Add rate limiting to the API",
    context=agent_context,
)

# Result includes:
# - result.stages: List[StageResult] with full history
# - result.final_response: Engineer's implementation
# - result.total_duration_ms: Total time
# - result.success: True/False
```

### Example 2: Research with Multiple LLMs

```python
# Fan out to multiple research agents
claude = registry.get("claude_researcher")
gemini = registry.get("gemini_researcher")
grok = registry.get("grok_researcher")

fanout = FanOutExecutor(db, settings, llm_client, tool_gateway)
result = await fanout.execute(
    agents=[claude, gemini, grok],
    message="Best practices for microservice authentication",
    context=agent_context,
)

# LLM synthesizes consensus from all three researchers
print(result.final_response)
```

### Example 3: TDD Code Development

```python
# Set up TDD loop
engineer = registry.get("engineer_agent")
qa = registry.get("qa_agent")

tdd_loop = TDDLoopExecutor(db, settings, llm_client, tool_gateway)
result = await tdd_loop.execute(
    builder=engineer,
    tester=qa,
    message="Build user registration endpoint with validation",
    context=agent_context,
    max_iterations=5,
)

# Engineer writes code, QA tests, iterate until tests pass
# result.metadata["iterations"]: Number of iterations
# result.metadata["tests_passed"]: True/False
```

### Example 4: Goal Planning and Execution

```python
# Create planner
planner = GoalPlanner(db, settings, llm_client, tool_gateway, registry)

# Decompose goal
graph = await planner.decompose(
    goal="Implement OAuth2 authentication",
    user_role=user.role,
)

# Validate
if not planner.validate_graph(graph):
    raise ValueError("Invalid task graph")

# Get human-readable plan
plan_text = planner.get_execution_plan(graph)
print(plan_text)

# User approves...

# Execute
completed_tasks = await planner.execute_graph(graph, agent_context)

# Check results
for task in completed_tasks:
    if task.status == "failed":
        print(f"Task {task.id} failed: {task.metadata.get('error')}")
    else:
        print(f"Task {task.id} complete: {task.result.content[:100]}")
```

---

## Testing

### Unit Tests

```bash
pytest tests/agent/composition/test_patterns.py -v
pytest tests/agent/composition/test_goal_planner.py -v
pytest tests/agent/composition/test_agent_memory.py -v
pytest tests/api/test_plans.py -v
```

### Integration Tests

```bash
pytest tests/integration/test_composition_pipeline.py -v
pytest tests/integration/test_goal_execution.py -v
```

---

## Performance Considerations

### Pipeline
- **Latency:** Sequential - sum of all stage latencies
- **Optimization:** Use FanOut where dependencies allow

### FanOut
- **Latency:** Max latency of slowest agent
- **Optimization:** Keep agent count reasonable (3-7)

### Gate
- **Latency:** (attempts * agent_latency) + (attempts * verifier_latency)
- **Optimization:** Good initial agent performance reduces retries

### TDD Loop
- **Latency:** (iterations * (builder + tester latency))
- **Optimization:** Clear requirements reduce iterations

### Goal Planner
- **Decomposition:** Single LLM call (~2-5s)
- **Execution:** Depends on task graph complexity
- **Optimization:** Parallelism reduces total time significantly

---

## Security & Compliance

### Tenant Isolation
- All memory queries filter by tenant_id
- Plans scoped to tenant
- Cross-tenant access blocked at API layer

### RBAC
- CREATE_PLAN: OPERATOR or higher
- APPROVE_PLAN: OPERATOR or higher
- Read permissions: Owner (same tenant)

### Audit Trail
- All composition stages logged
- Task execution history preserved
- Agent memory access counted

---

## Future Enhancements

### Phase 4B Candidates
1. **Persistent Plan Storage** - Database table for plans (currently in-memory)
2. **Async Plan Execution** - Background job for long-running plans
3. **Plan Templates** - Reusable goal patterns
4. **Memory Embeddings** - Vector search for semantic relevance
5. **Composition Metrics** - Success rates, latencies per pattern
6. **Agent Collaboration** - Agents can directly message each other
7. **Hierarchical Planning** - Sub-goals with nested task graphs

---

## Migration

Run migration to create agent_memory table:

```bash
alembic upgrade head
```

Verify:
```bash
psql enterprise_agents -c "\d agent_memory"
```

---

## References

- Export Control Policy: Verification Requirements
- Phase 3: Multi-Agent Runtime
- Agent Registry: src/agent/registry.py
- Orchestrator: src/agent/orchestrator.py
