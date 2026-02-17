"""Agent composition patterns for multi-agent workflows.

Implements four core composition patterns:
1. PIPELINE: Sequential execution, each stage feeds next stage
2. FAN_OUT: Parallel execution with LLM-based result synthesis
3. GATE: Agent produces → verifier checks → pass or retry loop
4. TDD_LOOP: Builder creates → tester verifies → iterate until pass

All patterns produce CompositionResult with full audit trail per stage.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.registry import AgentSpec
from src.agent.specialists.base import AgentContext, AgentResponse
from src.agent.tools import ToolGateway
from src.config import Settings
from src.database import AsyncSession

log = structlog.get_logger(__name__)

# Maximum recursion depth for nested composition patterns.  Prevents
# runaway Orchestrator spawning when compositions invoke sub-compositions.
MAX_COMPOSITION_DEPTH = 3


def _get_orchestrator_class():
    """Lazy import AgentOrchestrator to avoid circular import.

    patterns.py is imported by orchestrator.py at method call time;
    AgentOrchestrator is imported here at method call time.  This breaks
    the circular dependency between the two modules.
    """
    from src.agent.orchestrator import AgentOrchestrator
    return AgentOrchestrator


class CompositionPattern(StrEnum):
    """Available agent composition patterns."""

    PIPELINE = "pipeline"
    FAN_OUT = "fan_out"
    GATE = "gate"
    TDD_LOOP = "tdd_loop"


@dataclass
class StageResult:
    """Result from a single stage in a composition.

    Captures:
    - Which agent executed
    - The response produced
    - Success/failure status
    - Duration in milliseconds
    - Metadata for debugging
    """

    agent_id: str
    response: AgentResponse
    status: str  # "success", "failure", "retry"
    duration_ms: int
    stage_number: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompositionResult:
    """Complete result from a composition execution.

    Includes:
    - All stage results (full audit trail)
    - Final synthesized response
    - Pattern used
    - Total duration
    - Execution metadata
    """

    stages: list[StageResult]
    final_response: str
    pattern_used: CompositionPattern
    total_duration_ms: int
    success: bool
    metadata: dict[str, Any] = field(default_factory=dict)


class PipelineExecutor:
    """Execute agents in sequence, each stage feeds next stage.

    Pipeline pattern: A → B → C
    - Stage 1 processes original message
    - Stage 2 receives Stage 1's output as context
    - Stage 3 receives Stage 2's output as context
    - Etc.

    Use case: Sequential domain expertise handoff
    Example: Explore → Architect → Engineer
    """

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
    ) -> None:
        """Initialize pipeline executor.

        Args:
            db: Database session for orchestrator
            settings: Application settings
            llm_client: LLM client for agent calls
            tool_gateway: Tool execution gateway
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._tools = tool_gateway

    async def execute(
        self,
        stages: list[AgentSpec],
        message: str,
        context: AgentContext,
        depth: int = 0,
    ) -> CompositionResult:
        """Execute pipeline of agents sequentially.

        Each stage receives the previous stage's output as additional context.

        Args:
            stages: List of agent specs to execute in order
            message: Original user message
            context: Agent context (tenant, user, permissions)
            depth: Current composition nesting depth (default 0)

        Returns:
            CompositionResult with full stage audit trail

        Raises:
            ValueError: If max composition depth exceeded
        """
        if depth >= MAX_COMPOSITION_DEPTH:
            log.warning("composition.max_depth_reached", depth=depth, pattern="PipelineExecutor")
            raise ValueError(f"Maximum composition depth ({MAX_COMPOSITION_DEPTH}) exceeded")

        if not stages:
            raise ValueError("Pipeline requires at least one stage")

        log.info(
            "pipeline.execute_start",
            stages=[s.agent_id for s in stages],
            tenant_id=str(context.tenant_id),
        )

        start_time = time.time()
        stage_results: list[StageResult] = []
        current_message = message
        accumulated_context = context.rag_context

        for idx, agent_spec in enumerate(stages, start=1):
            stage_start = time.time()

            log.debug(
                "pipeline.stage_start",
                stage=idx,
                agent_id=agent_spec.agent_id,
            )

            # Create orchestrator for this stage
            orchestrator = _get_orchestrator_class()(
                db=self._db,
                settings=self._settings,
                llm_client=self._llm,
                tool_gateway=self._tools,
            )

            # Update context with accumulated results from previous stages
            stage_context = AgentContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                user_role=context.user_role,
                conversation_id=context.conversation_id,
                rag_context=accumulated_context,
                conversation_history=context.conversation_history,
                data_classification_level=context.data_classification_level,
            )

            try:
                # Create agent instance directly (bypass orchestrator routing)
                agent_instance = await orchestrator._create_agent_instance(agent_spec)
                agent_response = await agent_instance.process(current_message, stage_context)

                stage_duration = int((time.time() - stage_start) * 1000)

                stage_result = StageResult(
                    agent_id=agent_spec.agent_id,
                    response=agent_response,
                    status="success",
                    duration_ms=stage_duration,
                    stage_number=idx,
                    metadata={"stage_name": agent_spec.name},
                )

                stage_results.append(stage_result)

                # Update context for next stage
                current_message = agent_response.content
                accumulated_context += f"\n\nPrevious stage ({agent_spec.name}) output:\n{agent_response.content}"

                log.info(
                    "pipeline.stage_complete",
                    stage=idx,
                    agent_id=agent_spec.agent_id,
                    duration_ms=stage_duration,
                )

            except Exception as exc:
                stage_duration = int((time.time() - stage_start) * 1000)
                log.error(
                    "pipeline.stage_failed",
                    stage=idx,
                    agent_id=agent_spec.agent_id,
                    error=str(exc),
                )

                # Create failure response
                failure_response = AgentResponse(
                    content=f"Stage {idx} failed: {exc}",
                    agent_id=agent_spec.agent_id,
                    reasoning_trace=[f"Error: {exc}"],
                )

                stage_result = StageResult(
                    agent_id=agent_spec.agent_id,
                    response=failure_response,
                    status="failure",
                    duration_ms=stage_duration,
                    stage_number=idx,
                    metadata={"error": str(exc)},
                )

                stage_results.append(stage_result)

                # Pipeline stops on failure
                total_duration = int((time.time() - start_time) * 1000)
                return CompositionResult(
                    stages=stage_results,
                    final_response=f"Pipeline failed at stage {idx}: {exc}",
                    pattern_used=CompositionPattern.PIPELINE,
                    total_duration_ms=total_duration,
                    success=False,
                    metadata={"failed_stage": idx, "total_stages": len(stages)},
                )

        # Pipeline complete - final response is last stage's output
        total_duration = int((time.time() - start_time) * 1000)

        log.info(
            "pipeline.execute_complete",
            stages=len(stage_results),
            total_duration_ms=total_duration,
        )

        return CompositionResult(
            stages=stage_results,
            final_response=current_message,
            pattern_used=CompositionPattern.PIPELINE,
            total_duration_ms=total_duration,
            success=True,
            metadata={"stages_executed": len(stage_results)},
        )


class FanOutExecutor:
    """Execute multiple agents in parallel and synthesize results.

    FanOut pattern: → [A, B, C] → synthesize
    - All agents process the same message concurrently
    - Results are collected
    - LLM synthesizes a unified response

    Use case: Multiple perspectives on same problem
    Example: Research from multiple LLMs, security analysis from different angles
    """

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
    ) -> None:
        """Initialize fan-out executor.

        Args:
            db: Database session
            settings: Application settings
            llm_client: LLM client for synthesis
            tool_gateway: Tool gateway
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._tools = tool_gateway

    async def execute(
        self,
        agents: list[AgentSpec],
        message: str,
        context: AgentContext,
        depth: int = 0,
    ) -> CompositionResult:
        """Execute agents in parallel and synthesize results.

        Args:
            agents: List of agent specs to execute concurrently
            message: User message (same for all agents)
            context: Agent context
            depth: Current composition nesting depth (default 0)

        Returns:
            CompositionResult with synthesized final response

        Raises:
            ValueError: If max composition depth exceeded
        """
        if depth >= MAX_COMPOSITION_DEPTH:
            log.warning("composition.max_depth_reached", depth=depth, pattern="FanOutExecutor")
            raise ValueError(f"Maximum composition depth ({MAX_COMPOSITION_DEPTH}) exceeded")

        if not agents:
            raise ValueError("FanOut requires at least one agent")

        log.info(
            "fanout.execute_start",
            agents=[a.agent_id for a in agents],
            tenant_id=str(context.tenant_id),
        )

        start_time = time.time()

        # Create orchestrator tasks for all agents
        tasks = []
        for agent_spec in agents:
            task = self._execute_single_agent(agent_spec, message, context)
            tasks.append(task)

        # Execute all in parallel
        stage_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to failure StageResults
        processed_results: list[StageResult] = []
        for idx, result in enumerate(stage_results):
            if isinstance(result, Exception):
                log.error(
                    "fanout.agent_failed",
                    agent_id=agents[idx].agent_id,
                    error=str(result),
                )
                failure_response = AgentResponse(
                    content=f"Agent failed: {result}",
                    agent_id=agents[idx].agent_id,
                    reasoning_trace=[f"Error: {result}"],
                )
                processed_results.append(
                    StageResult(
                        agent_id=agents[idx].agent_id,
                        response=failure_response,
                        status="failure",
                        duration_ms=0,
                        stage_number=idx + 1,
                        metadata={"error": str(result)},
                    )
                )
            else:
                processed_results.append(result)

        # Synthesize results using LLM
        successful_results = [r for r in processed_results if r.status == "success"]

        if not successful_results:
            total_duration = int((time.time() - start_time) * 1000)
            return CompositionResult(
                stages=processed_results,
                final_response="All agents failed during fan-out execution",
                pattern_used=CompositionPattern.FAN_OUT,
                total_duration_ms=total_duration,
                success=False,
                metadata={"failed_agents": len(agents)},
            )

        synthesized_response = await self._synthesize_responses(
            message, successful_results
        )

        total_duration = int((time.time() - start_time) * 1000)

        log.info(
            "fanout.execute_complete",
            agents=len(agents),
            successful=len(successful_results),
            total_duration_ms=total_duration,
        )

        return CompositionResult(
            stages=processed_results,
            final_response=synthesized_response,
            pattern_used=CompositionPattern.FAN_OUT,
            total_duration_ms=total_duration,
            success=True,
            metadata={
                "agents_executed": len(agents),
                "successful_agents": len(successful_results),
            },
        )

    async def _execute_single_agent(
        self,
        agent_spec: AgentSpec,
        message: str,
        context: AgentContext,
    ) -> StageResult:
        """Execute a single agent for fan-out pattern.

        Args:
            agent_spec: Agent to execute
            message: User message
            context: Agent context

        Returns:
            StageResult for this agent
        """
        start_time = time.time()

        orchestrator = _get_orchestrator_class()(
            db=self._db,
            settings=self._settings,
            llm_client=self._llm,
            tool_gateway=self._tools,
        )

        try:
            agent_instance = await orchestrator._create_agent_instance(agent_spec)
            agent_response = await agent_instance.process(message, context)

            duration_ms = int((time.time() - start_time) * 1000)

            return StageResult(
                agent_id=agent_spec.agent_id,
                response=agent_response,
                status="success",
                duration_ms=duration_ms,
                metadata={"agent_name": agent_spec.name},
            )

        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            raise exc  # Let gather() handle it

    async def _synthesize_responses(
        self,
        original_message: str,
        results: list[StageResult],
    ) -> str:
        """Synthesize multiple agent responses into unified answer.

        Uses LLM to merge perspectives, identify consensus, and resolve conflicts.

        Args:
            original_message: Original user message
            results: Successful stage results to synthesize

        Returns:
            Synthesized response text
        """
        if len(results) == 1:
            return results[0].response.content

        # Build synthesis prompt
        responses_text = "\n\n".join(
            f"Agent {r.agent_id} ({r.metadata.get('agent_name', 'unknown')}):\n{r.response.content}"
            for r in results
        )

        synthesis_prompt = f"""Multiple AI agents analyzed this user request from different perspectives.
Synthesize their responses into a single, coherent answer.

Original Request: {original_message}

Agent Responses:
{responses_text}

Synthesize these perspectives into one answer. When agents agree, state the consensus.
When they disagree, explain the different viewpoints. Be clear and concise.

Synthesized Response:"""

        messages = [
            {
                "role": "system",
                "content": "You are a synthesis assistant. Merge multiple AI responses into coherent answers.",
            },
            {"role": "user", "content": synthesis_prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.5,
                max_tokens=2048,
            )

            return self._llm.extract_text(response)

        except Exception as exc:
            log.error("fanout.synthesis_failed", error=str(exc))
            # Fallback: concatenate responses
            return f"Multiple agents responded:\n\n{responses_text}"


class GateExecutor:
    """Execute agent with verification gate - retry on failure.

    Gate pattern: Agent → Verifier → Pass or Retry
    - Agent produces output
    - Verifier checks quality/correctness
    - If verification passes, return result
    - If verification fails, retry agent (up to max_retries)

    Use case: Quality gates, AI output verification requirements
    Example: Engineer produces code → QA verifies → Deploy or fix
    """

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
    ) -> None:
        """Initialize gate executor.

        Args:
            db: Database session
            settings: Application settings
            llm_client: LLM client
            tool_gateway: Tool gateway
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._tools = tool_gateway

    async def execute(
        self,
        agent: AgentSpec,
        verifier: AgentSpec,
        message: str,
        context: AgentContext,
        max_retries: int = 3,
        depth: int = 0,
    ) -> CompositionResult:
        """Execute agent with verification gate.

        Args:
            agent: Agent that produces output
            verifier: Agent that verifies output
            message: User message
            context: Agent context
            max_retries: Maximum retry attempts
            depth: Current composition nesting depth (default 0)

        Returns:
            CompositionResult with gate execution history

        Raises:
            ValueError: If max composition depth exceeded
        """
        if depth >= MAX_COMPOSITION_DEPTH:
            log.warning("composition.max_depth_reached", depth=depth, pattern="GateExecutor")
            raise ValueError(f"Maximum composition depth ({MAX_COMPOSITION_DEPTH}) exceeded")

        log.info(
            "gate.execute_start",
            agent_id=agent.agent_id,
            verifier_id=verifier.agent_id,
            max_retries=max_retries,
        )

        start_time = time.time()
        stage_results: list[StageResult] = []
        attempt = 0

        orchestrator = _get_orchestrator_class()(
            db=self._db,
            settings=self._settings,
            llm_client=self._llm,
            tool_gateway=self._tools,
        )

        while attempt < max_retries:
            attempt += 1

            # Execute agent
            agent_start = time.time()
            try:
                agent_instance = await orchestrator._create_agent_instance(agent)
                agent_response = await agent_instance.process(message, context)

                agent_duration = int((time.time() - agent_start) * 1000)

                agent_stage = StageResult(
                    agent_id=agent.agent_id,
                    response=agent_response,
                    status="success",
                    duration_ms=agent_duration,
                    stage_number=len(stage_results) + 1,
                    metadata={"attempt": attempt, "phase": "production"},
                )

                stage_results.append(agent_stage)

            except Exception as exc:
                log.error("gate.agent_failed", attempt=attempt, error=str(exc))
                failure_response = AgentResponse(
                    content=f"Agent failed: {exc}",
                    agent_id=agent.agent_id,
                    reasoning_trace=[f"Error: {exc}"],
                )

                agent_stage = StageResult(
                    agent_id=agent.agent_id,
                    response=failure_response,
                    status="failure",
                    duration_ms=int((time.time() - agent_start) * 1000),
                    stage_number=len(stage_results) + 1,
                    metadata={"attempt": attempt, "error": str(exc)},
                )

                stage_results.append(agent_stage)
                continue  # Retry

            # Execute verifier
            verifier_start = time.time()
            verification_message = f"Verify this output meets requirements:\n\n{agent_response.content}\n\nOriginal request: {message}"

            try:
                verifier_instance = await orchestrator._create_agent_instance(verifier)
                verifier_response = await verifier_instance.process(
                    verification_message, context
                )

                verifier_duration = int((time.time() - verifier_start) * 1000)

                # Check if verification passed (look for "VERIFIED" or "PASS" in response)
                verification_passed = self._check_verification_passed(
                    verifier_response.content
                )

                verifier_stage = StageResult(
                    agent_id=verifier.agent_id,
                    response=verifier_response,
                    status="success" if verification_passed else "retry",
                    duration_ms=verifier_duration,
                    stage_number=len(stage_results) + 1,
                    metadata={"attempt": attempt, "phase": "verification", "passed": verification_passed},
                )

                stage_results.append(verifier_stage)

                if verification_passed:
                    # Gate passed!
                    total_duration = int((time.time() - start_time) * 1000)

                    log.info(
                        "gate.execute_complete",
                        attempts=attempt,
                        total_duration_ms=total_duration,
                    )

                    return CompositionResult(
                        stages=stage_results,
                        final_response=agent_response.content,
                        pattern_used=CompositionPattern.GATE,
                        total_duration_ms=total_duration,
                        success=True,
                        metadata={"attempts": attempt, "verification_passed": True},
                    )

                else:
                    log.info("gate.verification_failed", attempt=attempt)
                    # Update message with feedback for next attempt
                    message = f"{message}\n\nPrevious attempt feedback:\n{verifier_response.content}"

            except Exception as exc:
                log.error("gate.verifier_failed", attempt=attempt, error=str(exc))
                failure_response = AgentResponse(
                    content=f"Verifier failed: {exc}",
                    agent_id=verifier.agent_id,
                    reasoning_trace=[f"Error: {exc}"],
                )

                verifier_stage = StageResult(
                    agent_id=verifier.agent_id,
                    response=failure_response,
                    status="failure",
                    duration_ms=int((time.time() - verifier_start) * 1000),
                    stage_number=len(stage_results) + 1,
                    metadata={"attempt": attempt, "error": str(exc)},
                )

                stage_results.append(verifier_stage)
                continue  # Retry

        # Max retries exceeded
        total_duration = int((time.time() - start_time) * 1000)

        log.warning("gate.max_retries_exceeded", attempts=attempt)

        return CompositionResult(
            stages=stage_results,
            final_response=f"Verification gate failed after {max_retries} attempts",
            pattern_used=CompositionPattern.GATE,
            total_duration_ms=total_duration,
            success=False,
            metadata={"attempts": attempt, "verification_passed": False},
        )

    def _check_verification_passed(self, verifier_response: str) -> bool:
        """Check if verifier response indicates pass.

        Uses word-boundary regex matching and checks fail indicators FIRST
        (higher priority) to avoid false positives like "doesn't pass"
        matching "pass".

        Args:
            verifier_response: Verifier's response text

        Returns:
            True if verification passed
        """
        import re
        response_lower = verifier_response.lower()

        # Check for explicit failure FIRST (higher priority)
        fail_patterns = [
            r"\bfail(?:ed|s|ure)?\b",
            r"\breject(?:ed|s)?\b",
            r"\bunacceptable\b",
            r"\bdoes\s+not\s+(?:meet|pass|satisfy)\b",
            r"\bnot\s+(?:acceptable|approved|verified|valid)\b",
            r"\binsufficient\b",
            r"\binadequate\b",
        ]
        for pattern in fail_patterns:
            if re.search(pattern, response_lower):
                return False

        # Then check for pass indicators
        pass_patterns = [
            r"\bverified\b",
            r"\bpass(?:ed|es)?\b",
            r"\bapproved\b",
            r"\bacceptable\b",
            r"\bmeets?\s+requirements?\b",
            r"\bsatisf(?:ied|ies|actory)\b",
        ]
        return any(re.search(p, response_lower) for p in pass_patterns)


class TDDLoopExecutor:
    """Execute builder-tester loop until tests pass.

    TDD Loop pattern: Builder ↔ Tester
    - Builder creates artifact
    - Tester runs tests
    - If tests pass, done
    - If tests fail, builder fixes (using test feedback)
    - Iterate until tests pass or max_iterations

    Use case: Test-Driven Development, iterative refinement
    Example: Engineer writes code → QA tests → Fix bugs → Retest
    """

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
    ) -> None:
        """Initialize TDD loop executor.

        Args:
            db: Database session
            settings: Application settings
            llm_client: LLM client
            tool_gateway: Tool gateway
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._tools = tool_gateway

    async def execute(
        self,
        builder: AgentSpec,
        tester: AgentSpec,
        message: str,
        context: AgentContext,
        max_iterations: int = 5,
        depth: int = 0,
    ) -> CompositionResult:
        """Execute TDD loop until tests pass.

        Args:
            builder: Agent that builds/fixes artifacts
            tester: Agent that tests artifacts
            message: User message (build requirements)
            context: Agent context
            max_iterations: Maximum build-test cycles
            depth: Current composition nesting depth (default 0)

        Returns:
            CompositionResult with TDD loop history

        Raises:
            ValueError: If max composition depth exceeded
        """
        if depth >= MAX_COMPOSITION_DEPTH:
            log.warning("composition.max_depth_reached", depth=depth, pattern="TDDLoopExecutor")
            raise ValueError(f"Maximum composition depth ({MAX_COMPOSITION_DEPTH}) exceeded")

        log.info(
            "tdd_loop.execute_start",
            builder_id=builder.agent_id,
            tester_id=tester.agent_id,
            max_iterations=max_iterations,
        )

        start_time = time.time()
        stage_results: list[StageResult] = []
        iteration = 0
        current_message = message

        orchestrator = _get_orchestrator_class()(
            db=self._db,
            settings=self._settings,
            llm_client=self._llm,
            tool_gateway=self._tools,
        )

        while iteration < max_iterations:
            iteration += 1

            # Build phase
            build_start = time.time()
            try:
                builder_instance = await orchestrator._create_agent_instance(builder)
                builder_response = await builder_instance.process(current_message, context)

                build_duration = int((time.time() - build_start) * 1000)

                build_stage = StageResult(
                    agent_id=builder.agent_id,
                    response=builder_response,
                    status="success",
                    duration_ms=build_duration,
                    stage_number=len(stage_results) + 1,
                    metadata={"iteration": iteration, "phase": "build"},
                )

                stage_results.append(build_stage)

            except Exception as exc:
                log.error("tdd_loop.build_failed", iteration=iteration, error=str(exc))
                failure_response = AgentResponse(
                    content=f"Build failed: {exc}",
                    agent_id=builder.agent_id,
                    reasoning_trace=[f"Error: {exc}"],
                )

                build_stage = StageResult(
                    agent_id=builder.agent_id,
                    response=failure_response,
                    status="failure",
                    duration_ms=int((time.time() - build_start) * 1000),
                    stage_number=len(stage_results) + 1,
                    metadata={"iteration": iteration, "error": str(exc)},
                )

                stage_results.append(build_stage)

                # Build failure - stop loop
                total_duration = int((time.time() - start_time) * 1000)
                return CompositionResult(
                    stages=stage_results,
                    final_response=f"Build failed at iteration {iteration}: {exc}",
                    pattern_used=CompositionPattern.TDD_LOOP,
                    total_duration_ms=total_duration,
                    success=False,
                    metadata={"iterations": iteration, "tests_passed": False},
                )

            # Test phase
            test_start = time.time()
            test_message = f"Test this artifact:\n\n{builder_response.content}\n\nOriginal requirements: {message}"

            try:
                tester_instance = await orchestrator._create_agent_instance(tester)
                tester_response = await tester_instance.process(test_message, context)

                test_duration = int((time.time() - test_start) * 1000)

                # Check if tests passed
                tests_passed = self._check_tests_passed(tester_response.content)

                test_stage = StageResult(
                    agent_id=tester.agent_id,
                    response=tester_response,
                    status="success" if tests_passed else "retry",
                    duration_ms=test_duration,
                    stage_number=len(stage_results) + 1,
                    metadata={"iteration": iteration, "phase": "test", "passed": tests_passed},
                )

                stage_results.append(test_stage)

                if tests_passed:
                    # Tests passed - loop complete!
                    total_duration = int((time.time() - start_time) * 1000)

                    log.info(
                        "tdd_loop.execute_complete",
                        iterations=iteration,
                        total_duration_ms=total_duration,
                    )

                    return CompositionResult(
                        stages=stage_results,
                        final_response=builder_response.content,
                        pattern_used=CompositionPattern.TDD_LOOP,
                        total_duration_ms=total_duration,
                        success=True,
                        metadata={"iterations": iteration, "tests_passed": True},
                    )

                else:
                    log.info("tdd_loop.tests_failed", iteration=iteration)
                    # Update message with test feedback for next iteration
                    current_message = f"{message}\n\nIteration {iteration} test results:\n{tester_response.content}\n\nFix the issues and rebuild."

            except Exception as exc:
                log.error("tdd_loop.test_failed", iteration=iteration, error=str(exc))
                failure_response = AgentResponse(
                    content=f"Test failed: {exc}",
                    agent_id=tester.agent_id,
                    reasoning_trace=[f"Error: {exc}"],
                )

                test_stage = StageResult(
                    agent_id=tester.agent_id,
                    response=failure_response,
                    status="failure",
                    duration_ms=int((time.time() - test_start) * 1000),
                    stage_number=len(stage_results) + 1,
                    metadata={"iteration": iteration, "error": str(exc)},
                )

                stage_results.append(test_stage)

                # Test failure - stop loop
                total_duration = int((time.time() - start_time) * 1000)
                return CompositionResult(
                    stages=stage_results,
                    final_response=f"Test execution failed at iteration {iteration}: {exc}",
                    pattern_used=CompositionPattern.TDD_LOOP,
                    total_duration_ms=total_duration,
                    success=False,
                    metadata={"iterations": iteration, "tests_passed": False},
                )

        # Max iterations exceeded
        total_duration = int((time.time() - start_time) * 1000)

        log.warning("tdd_loop.max_iterations_exceeded", iterations=iteration)

        return CompositionResult(
            stages=stage_results,
            final_response=f"TDD loop did not converge after {max_iterations} iterations",
            pattern_used=CompositionPattern.TDD_LOOP,
            total_duration_ms=total_duration,
            success=False,
            metadata={"iterations": iteration, "tests_passed": False},
        )

    def _check_tests_passed(self, tester_response: str) -> bool:
        """Check if tester response indicates tests passed.

        Args:
            tester_response: Tester's response text

        Returns:
            True if all tests passed
        """
        response_lower = tester_response.lower()
        pass_indicators = ["all tests passed", "tests pass", "success", "✓", "✅"]
        fail_indicators = ["test failed", "tests fail", "failure", "✗", "❌"]

        # Check for explicit failure first
        if any(indicator in response_lower for indicator in fail_indicators):
            return False

        # Check for pass indicators
        return any(indicator in response_lower for indicator in pass_indicators)
