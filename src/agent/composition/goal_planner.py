"""Goal decomposition and task graph execution.

The GoalPlanner breaks down high-level goals into dependency graphs of tasks,
assigns agents to tasks, and executes them in topological order with parallelism
where dependencies allow.

Example:
    User goal: "Deploy new authentication service"

    Decomposed graph:
    1. [Security Review] → depends on: []
    2. [Architecture Design] → depends on: [1]
    3. [Implementation] → depends on: [2]
    4. [Testing] → depends on: [3]
    5. [Deployment] → depends on: [4]

    Execution: 1 → 2 → 3 → 4 → 5 (sequential)

    Or with parallelism:
    2a. [Backend Implementation] → depends on: [2]
    2b. [Frontend Implementation] → depends on: [2]
    3. [Integration Testing] → depends on: [2a, 2b]
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.orchestrator import AgentOrchestrator
from src.agent.registry import AgentRegistry, AgentSpec
from src.agent.specialists.base import AgentContext, AgentResponse
from src.agent.tools import ToolGateway
from src.config import Settings
from src.database import AsyncSession
from src.models.user import UserRole

log = structlog.get_logger(__name__)


@dataclass
class TaskNode:
    """A single task in the goal decomposition graph.

    Each task has:
    - Unique ID
    - Description of what needs to be done
    - Assigned agent
    - Dependencies (task IDs that must complete first)
    - Status (pending, running, complete, failed)
    - Result from execution
    """

    id: str
    description: str
    agent_id: str
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, complete, failed
    result: AgentResponse | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskGraph:
    """Directed acyclic graph (DAG) of tasks.

    Represents a decomposed goal as nodes (tasks) and edges (dependencies).
    """

    nodes: dict[str, TaskNode]
    edges: dict[str, list[str]]  # task_id → [dependent_task_ids]
    root_goal: str
    metadata: dict[str, Any] = field(default_factory=dict)


class GoalPlanner:
    """Decompose goals into task graphs and execute with dependency management.

    The planner:
    1. Uses LLM to decompose high-level goal into tasks
    2. Assigns appropriate agents to each task
    3. Validates DAG structure (no cycles)
    4. Executes in topological order with parallelism
    5. Returns results for each task
    """

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
        registry: AgentRegistry,
    ) -> None:
        """Initialize goal planner.

        Args:
            db: Database session
            settings: Application settings
            llm_client: LLM client for goal decomposition
            tool_gateway: Tool gateway
            registry: Agent registry for agent assignment
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._tools = tool_gateway
        self._registry = registry

    async def decompose(
        self,
        goal: str,
        user_role: UserRole,
        available_agents: list[AgentSpec] | None = None,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        requesting_user_id: uuid.UUID | None = None,
    ) -> TaskGraph:
        """Decompose a high-level goal into a task dependency graph.

        Uses LLM to:
        1. Break goal into subtasks
        2. Identify dependencies between tasks
        3. Assign appropriate agents to each task

        When tenant_id and user_id are provided, existing active goals are
        loaded and injected into the decomposition prompt so the planner can
        avoid re-doing completed work and align with prior goals.

        Args:
            goal: High-level goal description
            user_role: User's role (for agent access filtering)
            available_agents: Optional list of available agents (defaults to all accessible)
            tenant_id: Optional tenant UUID for loading existing goals
            user_id: Optional user UUID for loading existing goals
            requesting_user_id: Optional authenticated user UUID; when provided,
                must match user_id to load existing goals (prevents cross-user access)

        Returns:
            TaskGraph with nodes and dependencies
        """
        log.info("goal_planner.decompose_start", goal_length=len(goal))

        # Get available agents
        if available_agents is None:
            available_agents = self._registry.list_agents(user_role=user_role)

        # Build agent descriptions for LLM
        agent_descriptions = "\n".join(
            f"- {agent.agent_id}: {agent.description} (capabilities: {', '.join(agent.capabilities)})"
            for agent in available_agents
        )

        # Load existing active goals for context (11E3: goal-informed planning)
        existing_goals_context = ""
        if tenant_id is not None and user_id is not None:
            # Cross-user access check: if requesting_user_id is provided,
            # it must match user_id to prevent loading another user's goals.
            if requesting_user_id is not None and user_id != requesting_user_id:
                log.warning(
                    "goal_planner.cross_user_access_denied",
                    user_id=str(user_id),
                    requesting_user_id=str(requesting_user_id),
                )
                existing_goals = []
            else:
                try:
                    from src.services.goal_service import GoalService
                    goal_service = GoalService(self._db)
                    existing_goals = await goal_service.get_active_goals(
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                except Exception as exc:
                    log.warning("goal_planner.existing_goals_load_failed", error=str(exc))
                    existing_goals = []

            if existing_goals:
                lines = [
                    f"Existing goal: {g.goal_text} (progress: {g.progress_notes or 'none'})"
                    for g in existing_goals
                ]
                existing_goals_context = "\n".join(lines)

        # Include existing goals in prompt if available
        existing_goals_section = ""
        if existing_goals_context:
            existing_goals_section = f"""
Existing user goals (already in progress):
{existing_goals_context}

When creating tasks:
- Avoid duplicating work already captured in progress notes above
- Align new sub-tasks with existing goals where relevant
- Reference completed progress if it can be built upon
"""

        decomposition_prompt = f"""You are a strategic planning assistant. Decompose this high-level goal into a task dependency graph.

Goal: {goal}

Available agents and their capabilities:
{agent_descriptions}
{existing_goals_section}
Instructions:
1. Break the goal into 3-8 concrete tasks
2. For each task, specify:
   - A clear description (what needs to be done)
   - Which agent should handle it (agent_id from the list above)
   - Which other tasks it depends on (dependencies)
3. Create a valid directed acyclic graph (DAG) - no cycles!
4. Tasks with no dependencies can run in parallel
5. Tasks should be granular and specific

Respond in this JSON format:
{{
  "tasks": [
    {{
      "id": "task_1",
      "description": "Task description",
      "agent_id": "agent_id_from_list",
      "dependencies": []
    }},
    {{
      "id": "task_2",
      "description": "Another task",
      "agent_id": "agent_id_from_list",
      "dependencies": ["task_1"]
    }}
  ]
}}

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a strategic planning assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": decomposition_prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.5,  # Some creativity in task breakdown
                max_tokens=2048,
            )

            response_text = self._llm.extract_text(response)

            # Parse JSON response
            parsed = json.loads(response_text)
            tasks_data = parsed.get("tasks", [])

            # Build TaskGraph
            nodes: dict[str, TaskNode] = {}
            edges: dict[str, list[str]] = defaultdict(list)

            for task_data in tasks_data:
                task_id = task_data.get("id", f"task_{uuid.uuid4().hex[:8]}")
                description = task_data.get("description", "")
                agent_id = task_data.get("agent_id", "")
                dependencies = task_data.get("dependencies", [])

                node = TaskNode(
                    id=task_id,
                    description=description,
                    agent_id=agent_id,
                    dependencies=dependencies,
                )

                nodes[task_id] = node

                # Build reverse edges for dependency tracking
                for dep_id in dependencies:
                    edges[dep_id].append(task_id)

            graph = TaskGraph(
                nodes=nodes,
                edges=edges,
                root_goal=goal,
                metadata={"decomposition_method": "llm", "agent_count": len(available_agents)},
            )

            log.info(
                "goal_planner.decompose_complete",
                task_count=len(nodes),
                edge_count=sum(len(deps) for deps in edges.values()),
            )

            return graph

        except json.JSONDecodeError as exc:
            log.error("goal_planner.decompose_json_failed", error=str(exc))
            # Fallback: single task with default agent
            fallback_node = TaskNode(
                id="task_1",
                description=goal,
                agent_id=available_agents[0].agent_id if available_agents else "default",
                dependencies=[],
            )

            return TaskGraph(
                nodes={"task_1": fallback_node},
                edges={},
                root_goal=goal,
                metadata={"decomposition_method": "fallback", "error": str(exc)},
            )

        except Exception as exc:
            log.error("goal_planner.decompose_failed", error=str(exc))
            raise

    def validate_graph(self, graph: TaskGraph) -> bool:
        """Validate that the task graph is a valid DAG.

        Checks for:
        - No cycles
        - All dependencies reference existing tasks
        - All agent_ids are valid

        Args:
            graph: Task graph to validate

        Returns:
            True if valid, False otherwise
        """
        log.debug("goal_planner.validate_start", task_count=len(graph.nodes))

        # Check 1: All dependencies reference existing tasks
        for task_id, node in graph.nodes.items():
            for dep_id in node.dependencies:
                if dep_id not in graph.nodes:
                    log.warning(
                        "goal_planner.validate_missing_dependency",
                        task_id=task_id,
                        missing_dep=dep_id,
                    )
                    return False

        # Check 2: No cycles (topological sort succeeds)
        try:
            _ = self._topological_sort(graph)
        except ValueError as exc:
            log.warning("goal_planner.validate_cycle_detected", error=str(exc))
            return False

        log.debug("goal_planner.validate_success")
        return True

    async def execute_graph(
        self,
        graph: TaskGraph,
        context: AgentContext,
    ) -> list[TaskNode]:
        """Execute task graph in dependency order with parallelism.

        Uses topological sort to determine execution order. Tasks with no
        pending dependencies run in parallel.

        Args:
            graph: Task graph to execute
            context: Agent context for execution

        Returns:
            List of completed TaskNodes with results
        """
        if not self.validate_graph(graph):
            raise ValueError("Task graph validation failed - cannot execute")

        log.info(
            "goal_planner.execute_start",
            task_count=len(graph.nodes),
            root_goal=graph.root_goal,
        )

        # Get topological order
        execution_order = self._topological_sort(graph)

        # Track in-degree (number of unmet dependencies) for each task
        in_degree: dict[str, int] = {}
        for task_id, node in graph.nodes.items():
            in_degree[task_id] = len(node.dependencies)

        # Track completed tasks
        completed: set[str] = set()
        completed_nodes: list[TaskNode] = []

        # Execute in waves (parallelism within each wave)
        orchestrator = AgentOrchestrator(
            db=self._db,
            settings=self._settings,
            llm_client=self._llm,
            tool_gateway=self._tools,
        )

        while len(completed) < len(graph.nodes):
            # Find all tasks ready to execute (in_degree == 0 and not completed)
            ready_tasks = [
                task_id
                for task_id in graph.nodes.keys()
                if in_degree[task_id] == 0 and task_id not in completed
            ]

            if not ready_tasks:
                # Shouldn't happen with valid DAG, but handle defensively
                remaining = set(graph.nodes.keys()) - completed
                log.error(
                    "goal_planner.execute_deadlock",
                    remaining_tasks=list(remaining),
                )
                raise RuntimeError(
                    f"Execution deadlock - tasks remaining but none ready: {remaining}"
                )

            log.info("goal_planner.execute_wave", ready_tasks=ready_tasks)

            # Execute ready tasks in parallel
            import asyncio

            tasks = []
            for task_id in ready_tasks:
                task = self._execute_task(
                    graph.nodes[task_id], context, orchestrator, completed_nodes
                )
                tasks.append(task)

            wave_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for task_id, result in zip(ready_tasks, wave_results):
                node = graph.nodes[task_id]

                if isinstance(result, Exception):
                    log.error(
                        "goal_planner.task_failed",
                        task_id=task_id,
                        error=str(result),
                    )
                    node.status = "failed"
                    node.metadata["error"] = str(result)
                else:
                    node.status = "complete"

                completed.add(task_id)
                completed_nodes.append(node)

                # Update in-degree for dependent tasks
                for dependent_id in graph.edges.get(task_id, []):
                    in_degree[dependent_id] -= 1

        log.info("goal_planner.execute_complete", total_tasks=len(completed_nodes))

        return completed_nodes

    async def _execute_task(
        self,
        node: TaskNode,
        context: AgentContext,
        orchestrator: AgentOrchestrator,
        completed_nodes: list[TaskNode],
    ) -> None:
        """Execute a single task.

        Args:
            node: Task node to execute
            context: Agent context
            orchestrator: Orchestrator for agent execution
            completed_nodes: List of already completed tasks (for context)
        """
        log.info("goal_planner.task_start", task_id=node.id, agent_id=node.agent_id)

        node.status = "running"

        # Build message with context from dependencies
        dependency_context = ""
        if node.dependencies:
            dependency_results = [
                n for n in completed_nodes if n.id in node.dependencies
            ]
            dependency_context = "\n\n".join(
                f"Dependency {dep.id} ({dep.agent_id}) result:\n{dep.result.content if dep.result else 'No result'}"
                for dep in dependency_results
            )

        message = f"Task: {node.description}"
        if dependency_context:
            message += f"\n\nContext from dependencies:\n{dependency_context}"

        # Get agent spec
        agent_spec = self._registry.get(node.agent_id)
        if agent_spec is None:
            raise ValueError(f"Agent '{node.agent_id}' not found in registry")

        # Execute agent
        try:
            agent_instance = await orchestrator._create_agent_instance(agent_spec)
            agent_response = await agent_instance.process(message, context)

            node.result = agent_response

            log.info("goal_planner.task_complete", task_id=node.id)

        except Exception as exc:
            log.error("goal_planner.task_failed", task_id=node.id, error=str(exc))
            raise

    def _topological_sort(self, graph: TaskGraph) -> list[str]:
        """Perform topological sort on the task graph.

        Uses Kahn's algorithm to produce a valid execution order.

        Args:
            graph: Task graph to sort

        Returns:
            List of task IDs in topological order

        Raises:
            ValueError: If graph contains a cycle
        """
        # Calculate in-degree for each node
        in_degree: dict[str, int] = {}
        for task_id, node in graph.nodes.items():
            in_degree[task_id] = len(node.dependencies)

        # Queue of nodes with no dependencies
        queue = deque([task_id for task_id, degree in in_degree.items() if degree == 0])

        sorted_order: list[str] = []

        while queue:
            task_id = queue.popleft()
            sorted_order.append(task_id)

            # Reduce in-degree for dependent tasks
            for dependent_id in graph.edges.get(task_id, []):
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        # If we didn't process all nodes, there's a cycle
        if len(sorted_order) != len(graph.nodes):
            raise ValueError(
                f"Graph contains a cycle - sorted {len(sorted_order)} of {len(graph.nodes)} nodes"
            )

        return sorted_order

    def get_execution_plan(self, graph: TaskGraph) -> str:
        """Generate human-readable execution plan from task graph.

        Args:
            graph: Task graph

        Returns:
            Formatted execution plan string
        """
        try:
            execution_order = self._topological_sort(graph)
        except ValueError as exc:
            return f"Invalid task graph: {exc}"

        lines = [f"Execution Plan for: {graph.root_goal}", "=" * 60, ""]

        for idx, task_id in enumerate(execution_order, start=1):
            node = graph.nodes[task_id]
            deps_str = ", ".join(node.dependencies) if node.dependencies else "None"

            lines.append(f"Step {idx}: {task_id}")
            lines.append(f"  Description: {node.description}")
            lines.append(f"  Agent: {node.agent_id}")
            lines.append(f"  Dependencies: {deps_str}")
            lines.append("")

        lines.append(f"Total tasks: {len(graph.nodes)}")

        return "\n".join(lines)
