# Model Router Usage Guide

## Overview

Phase 4E introduces intelligent model routing with token budgeting. The system automatically selects the appropriate model tier (LIGHT/STANDARD/HEAVY) based on task complexity, saving GPU resources and tokens.

## Architecture

```
User Request
    ↓
ComplexityEstimator (analyzes task)
    ↓
ModelRouter (selects tier)
    ↓
BudgetManager (checks limits)
    ↓
FallbackChain (executes with retry)
    ↓
ModelMetricsCollector (records outcome)
```

## Quick Start

### 1. Initialize Components

```python
from src.agent.model_router import (
    ModelRouter,
    ComplexityEstimator,
    BudgetManager,
    ModelMetricsCollector,
    FallbackChain,
)
from src.agent.llm import LLMClient
from src.config import get_settings

settings = get_settings()
llm_client = LLMClient(settings)

# Initialize router with default models
router = ModelRouter(settings)

# Initialize complexity estimator
estimator = ComplexityEstimator()

# Initialize budget manager
budget_manager = BudgetManager(
    default_daily_limit=settings.token_budget_daily,
    default_monthly_limit=settings.token_budget_monthly,
)

# Initialize metrics collector
metrics = ModelMetricsCollector()
```

### 2. Route a Request

```python
import uuid
from datetime import datetime, timezone

# Estimate complexity
complexity = await estimator.estimate(
    message="Analyze the security implications of this architecture design",
    context_length=1024,
    agent_capabilities=["security_analysis", "architecture"],
    history_length=5,
)

# Select model
model_config = router.route(
    task_type="security_analysis",
    complexity_score=complexity.score,
    agent_spec=None,  # or pass AgentSpec
)

print(f"Selected: {model_config.tier.value} - {model_config.model_id}")
print(f"Complexity: {complexity.score:.2f}")
print(f"Factors: {complexity.factors}")

# Check budget
tenant_id = uuid.uuid4()
estimated_tokens = 2000

if not budget_manager.check_budget(tenant_id, estimated_tokens):
    raise RuntimeError("Token budget exceeded")

# Execute with fallback
fallback_chain = FallbackChain(
    tiers=[
        router._models[ModelTier.STANDARD],
        router._models[ModelTier.HEAVY],
    ]
)

messages = [
    {"role": "user", "content": "Analyze this architecture..."}
]

response, actual_tier = await fallback_chain.execute_with_fallback(
    llm_client=llm_client,
    messages=messages,
    preferred_tier=model_config,
)

# Record usage
usage = response.usage
budget_manager.record_usage(
    tenant_id=tenant_id,
    model_tier=actual_tier.tier,
    input_tokens=usage.prompt_tokens,
    output_tokens=usage.completion_tokens,
    complexity_score=complexity.score,
)

# Record metrics
from src.agent.model_router import RoutingDecision

decision = RoutingDecision(
    timestamp=datetime.now(timezone.utc),
    tenant_id=tenant_id,
    task_type="security_analysis",
    selected_tier=actual_tier.tier.value,
    estimated_complexity=complexity.score,
    tokens_used=usage.total_tokens,
    latency_ms=1234.5,  # measure actual latency
)

metrics.record_decision(decision)
```

### 3. Monitor and Report

```python
# Get savings report
savings = budget_manager.get_savings_report(tenant_id)
print(f"Tokens saved: {savings['tokens_saved']}")
print(f"Cost reduction: {savings['cost_reduction_pct']}%")

# Get tier distribution
distribution = metrics.get_tier_distribution(tenant_id, period_hours=24)
print(f"Tier distribution: {distribution}")

# Get savings estimate
savings_estimate = metrics.get_savings_estimate(tenant_id, period_hours=24)
print(f"GPU hours saved: {savings_estimate['gpu_hours_saved']}")

# Export metrics for dashboard
export_data = metrics.export_metrics(tenant_id, period_hours=24)
```

## Model Tiers

### LIGHT: ollama/qwen2.5:7b
- **Use for**: Intent classification, PII detection, simple Q&A
- **Cost weight**: 1.0x (baseline)
- **GPU memory**: 8GB
- **Complexity**: 0.0-0.3

### STANDARD: ollama/qwen2.5:32b
- **Use for**: Agent execution, skill invocation, most tasks
- **Cost weight**: 3.0x
- **GPU memory**: 32GB
- **Complexity**: 0.3-0.7

### HEAVY: vllm/qwen2.5:72b
- **Use for**: Thinking tools, security analysis, architecture design
- **Cost weight**: 10.0x
- **GPU memory**: 72GB
- **Complexity**: 0.7-1.0

## Complexity Factors

The ComplexityEstimator analyzes:

1. **Message complexity** (30% weight)
   - Word count, vocabulary richness, sentence structure

2. **Context requirement** (15% weight)
   - Size of RAG context needed

3. **Capability criticality** (25% weight)
   - Safety-critical tasks (security, compliance) → heavier models

4. **Conversation depth** (10% weight)
   - Longer conversations → more context understanding needed

5. **Keyword signals** (20% weight)
   - Presence of complex keywords (analyze, architecture, security, etc.)

## Integration with Orchestrator

Update `src/agent/orchestrator.py` to use routing:

```python
from src.agent.model_router import ModelRouter, ComplexityEstimator

class AgentOrchestrator:
    def __init__(self, ...):
        # ... existing init
        self._model_router = ModelRouter(settings)
        self._complexity_estimator = ComplexityEstimator()

    async def _classify_intent(self, message: str) -> IntentClassification:
        # Estimate complexity
        complexity = await self._complexity_estimator.estimate(
            message=message,
            context_length=0,
            agent_capabilities=None,
            history_length=0,
        )

        # Route to appropriate model
        model_config = self._model_router.route(
            task_type="intent_classification",
            complexity_score=complexity.score,
        )

        # Use model_config.model_id for LLM call
        response = await self._llm.complete(
            messages=messages,
            model=model_config.model_id,  # Use routed model
            temperature=0.1,
            max_tokens=256,
        )
        # ...
```

## Configuration

Update `.env` or environment variables:

```bash
# Enable routing (default: true)
MODEL_ROUTING_ENABLED=true

# Model tier identifiers (LiteLLM format)
MODEL_LIGHT=ollama/qwen2.5:7b
MODEL_STANDARD=ollama/qwen2.5:32b
MODEL_HEAVY=vllm/qwen2.5:72b

# Token budgets per tenant
TOKEN_BUDGET_DAILY=1000000
TOKEN_BUDGET_MONTHLY=20000000
```

## Phase 4 vs Phase 5

**Phase 4 (Current)**:
- In-memory storage (dict-based)
- Single-process only
- Manual metrics export

**Phase 5 (Future)**:
- Redis-backed storage
- Multi-process/distributed
- Prometheus integration
- Real-time dashboards

## Testing

```python
import pytest
from src.agent.model_router import ModelRouter, ComplexityEstimator

@pytest.mark.asyncio
async def test_complexity_estimation():
    estimator = ComplexityEstimator()

    # Simple task
    simple = await estimator.estimate("Hello", context_length=0)
    assert simple.score < 0.3
    assert simple.recommended_tier == "light"

    # Complex task
    complex_msg = """
    Analyze the security implications of implementing a
    zero-trust architecture with mutual TLS authentication...
    """
    complex = await estimator.estimate(
        message=complex_msg,
        context_length=2048,
        agent_capabilities=["security_analysis"],
    )
    assert complex.score > 0.7
    assert complex.recommended_tier == "heavy"
```

## Monitoring

Key metrics to track:
- Tier distribution (% requests per tier)
- Token savings (vs. all-HEAVY baseline)
- GPU hours saved
- Quality by tier (if feedback available)
- Fallback events (failures requiring retry)
- Budget alerts (80%, 95% thresholds)

## Best Practices

1. **Always use FallbackChain** for production requests
2. **Check budgets** before expensive operations
3. **Record metrics** for every routing decision
4. **Monitor savings** to validate routing effectiveness
5. **Adjust thresholds** based on quality/cost tradeoffs
6. **Test complexity heuristics** against real workloads
