"""
Background worker pool for asynchronous document ingestion.

Provides an asyncio-based task queue that prevents in-request blocking
for expensive operations like document processing. Tasks are tracked with
full lifecycle management (PENDING → RUNNING → COMPLETED/FAILED/CANCELLED).

Key features:
- Configurable concurrency (max_workers)
- Dead letter queue for failed tasks (max_retries)
- Graceful shutdown with task draining
- Task status tracking for client polling
- Type-safe task payloads via dataclass

Design:
- Uses asyncio.Queue for work distribution
- Each worker is a long-running coroutine
- Task state is stored in-memory (replace with Redis for multi-instance)
- Provides submit_task(), get_task_status(), cancel_task() API
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class TaskStatus(StrEnum):
    """Task lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(StrEnum):
    """Known background task types."""
    DOCUMENT_INGESTION = "document_ingestion"


@dataclass
class Task:
    """Represents a background task with full lifecycle tracking."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: TaskType = TaskType.DOCUMENT_INGESTION
    payload: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3


class BackgroundWorkerPool:
    """
    Asyncio-based background task processor with concurrency control.

    Manages a pool of worker coroutines that pull tasks from a queue,
    execute them, and track their lifecycle. Failed tasks are retried
    up to max_retries times before moving to dead letter queue.

    Example usage:
        pool = BackgroundWorkerPool(max_workers=4)
        await pool.start()

        task_id = await pool.submit_task(
            task_type=TaskType.DOCUMENT_INGESTION,
            payload={"document_id": "123", "tenant_id": "456"}
        )

        status = pool.get_task_status(task_id)
        await pool.shutdown()
    """

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_retries: int = 3,
    ) -> None:
        """
        Initialize the worker pool.

        Args:
            max_workers: Maximum number of concurrent worker coroutines
            max_retries: Number of retry attempts for failed tasks
        """
        self._max_workers = max_workers
        self._max_retries = max_retries
        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._tasks: dict[str, Task] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._dead_letter: list[Task] = []
        self._shutdown_event = asyncio.Event()
        self._running = False

        log.info(
            "worker_pool.initialized",
            max_workers=max_workers,
            max_retries=max_retries,
        )

    async def start(self) -> None:
        """Start worker coroutines."""
        if self._running:
            log.warning("worker_pool.already_running")
            return

        self._running = True
        self._shutdown_event.clear()

        for i in range(self._max_workers):
            worker = asyncio.create_task(self._worker_loop(worker_id=i))
            self._workers.append(worker)

        log.info("worker_pool.started", worker_count=self._max_workers)

    async def shutdown(self, *, drain: bool = True) -> None:
        """
        Shutdown the worker pool.

        Args:
            drain: If True, wait for in-flight tasks to complete.
                   If False, cancel all workers immediately.
        """
        if not self._running:
            return

        log.info("worker_pool.shutdown_initiated", drain=drain)
        self._running = False
        self._shutdown_event.set()

        if drain:
            # Wait for queue to drain
            await self._queue.join()

        # Cancel all workers
        for worker in self._workers:
            worker.cancel()

        # Wait for cancellation to complete
        await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        log.info(
            "worker_pool.shutdown_complete",
            tasks_completed=len([t for t in self._tasks.values() if t.status == TaskStatus.COMPLETED]),
            tasks_failed=len([t for t in self._tasks.values() if t.status == TaskStatus.FAILED]),
            dead_letter_count=len(self._dead_letter),
        )

    async def submit_task(
        self,
        *,
        task_type: TaskType,
        payload: dict[str, Any],
        max_retries: int | None = None,
    ) -> str:
        """
        Submit a task to the background queue.

        Args:
            task_type: Type of task to execute
            payload: Task-specific data (must be JSON-serializable)
            max_retries: Override default max_retries for this task

        Returns:
            Task ID for status tracking
        """
        task = Task(
            type=task_type,
            payload=payload,
            max_retries=max_retries if max_retries is not None else self._max_retries,
        )

        self._tasks[task.id] = task
        await self._queue.put(task)

        log.info(
            "worker_pool.task_submitted",
            task_id=task.id,
            task_type=task_type,
            queue_size=self._queue.qsize(),
        )
        return task.id

    def get_task_status(self, task_id: str) -> Task | None:
        """
        Get current status of a task.

        Returns None if task ID not found.
        """
        return self._tasks.get(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a pending or running task.

        Returns True if task was cancelled, False if not found or already complete.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False

        task.status = TaskStatus.CANCELLED
        task.completed_at = datetime.now(UTC)

        log.info("worker_pool.task_cancelled", task_id=task_id)
        return True

    def get_dead_letter_queue(self) -> list[Task]:
        """Return tasks that exceeded max_retries."""
        return list(self._dead_letter)

    async def _worker_loop(self, worker_id: int) -> None:
        """
        Worker coroutine that processes tasks from the queue.

        Runs until shutdown_event is set. Handles task execution,
        retry logic, and dead letter queue management.
        """
        log.info("worker.started", worker_id=worker_id)

        while not self._shutdown_event.is_set():
            try:
                # Wait for task with timeout to check shutdown periodically
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue

            try:
                await self._execute_task(task, worker_id=worker_id)
            finally:
                self._queue.task_done()

        log.info("worker.stopped", worker_id=worker_id)

    async def _execute_task(self, task: Task, worker_id: int) -> None:
        """
        Execute a single task with error handling and retry logic.

        Args:
            task: Task to execute
            worker_id: ID of the worker executing this task
        """
        # Check if task was cancelled while in queue
        if task.status == TaskStatus.CANCELLED:
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(UTC)

        log.info(
            "worker.task_started",
            worker_id=worker_id,
            task_id=task.id,
            task_type=task.type,
            retry_count=task.retry_count,
        )

        try:
            # Dispatch based on task type
            if task.type == TaskType.DOCUMENT_INGESTION:
                await self._handle_document_ingestion(task)
            else:
                raise ValueError(f"Unknown task type: {task.type}")

            # Success
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(UTC)

            log.info(
                "worker.task_completed",
                worker_id=worker_id,
                task_id=task.id,
                duration_seconds=(task.completed_at - task.started_at).total_seconds(),
            )

        except Exception as exc:
            task.error = str(exc)
            task.retry_count += 1

            log.error(
                "worker.task_failed",
                worker_id=worker_id,
                task_id=task.id,
                error=str(exc),
                retry_count=task.retry_count,
                max_retries=task.max_retries,
            )

            # Retry logic
            if task.retry_count < task.max_retries:
                task.status = TaskStatus.PENDING
                await self._queue.put(task)
                log.info("worker.task_requeued", task_id=task.id)
            else:
                # Exceeded retries - move to dead letter queue
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now(UTC)
                self._dead_letter.append(task)
                log.error(
                    "worker.task_dead_letter",
                    task_id=task.id,
                    error=task.error,
                )

    async def _handle_document_ingestion(self, task: Task) -> None:
        """
        Handle document ingestion task.

        Expected payload:
            {
                "document_id": str,
                "tenant_id": str,
                "file_bytes_key": str,  # Key to retrieve file bytes from temp storage
            }

        Note: In production, file_bytes would be retrieved from object storage
        or temp cache, not passed directly in payload.
        """
        # Import here to avoid circular dependencies
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.agent.llm import LLMClient
        from src.config import get_settings
        from src.database import get_engine
        from src.models.document import Document
        from src.rag.ingest import IngestionPipeline

        settings = get_settings()
        engine = get_engine()

        async with AsyncSession(engine) as session:
            # Retrieve document
            doc_id = task.payload.get("document_id")
            if not doc_id:
                raise ValueError("Missing document_id in task payload")

            stmt = select(Document).where(Document.id == uuid.UUID(doc_id))
            result = await session.execute(stmt)
            document = result.scalar_one_or_none()

            if not document:
                raise ValueError(f"Document {doc_id} not found")

            # In production, retrieve file_bytes from object storage
            # For now, this is a placeholder - caller must handle file storage
            file_bytes = task.payload.get("file_bytes", b"")
            content_type = task.payload.get("content_type", "text/plain")

            if not file_bytes:
                raise ValueError("Missing file_bytes in task payload")

            # Run ingestion pipeline
            llm_client = LLMClient(settings=settings)
            pipeline = IngestionPipeline(
                db=session,
                settings=settings,
                llm_client=llm_client,
            )

            await pipeline.ingest_document(
                document=document,
                file_bytes=file_bytes,
                content_type=content_type,
            )

            await session.commit()
