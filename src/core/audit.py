"""Audit logging service.

Provides a simple, non-blocking interface for writing audit log entries.
All agent interactions, document operations, and admin actions are recorded.

Design:
- Audit writes happen AFTER the primary operation, in a separate session
  if needed, so that a failed audit write does not roll back the business
  operation. We log the failure and alert, but do not surface it to the user.
- Summaries are capped at 500 characters to avoid storing sensitive data
  at full fidelity in the audit table.
- The service is a plain class (not a singleton) to keep it testable.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.audit import AuditLog, AuditStatus

log = structlog.get_logger(__name__)

_SUMMARY_MAX_CHARS = 500


def _truncate(text: str | None, max_chars: int = _SUMMARY_MAX_CHARS) -> str | None:
    """Truncate text to max_chars, appending '...' if truncated."""
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


class AuditService:
    """Write-only audit log service.

    Usage:
        audit = AuditService(db)
        await audit.log(
            tenant_id=tenant_id,
            user_id=user_id,
            action="chat.message",
            ...
        )
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def log(
        self,
        *,
        tenant_id: uuid.UUID,
        action: str,
        user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        model_used: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        request_summary: str | None = None,
        response_summary: str | None = None,
        latency_ms: int | None = None,
        status: AuditStatus = AuditStatus.SUCCESS,
        error_detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Write an audit log entry and flush it to the DB.

        This does not commit - the calling code owns the transaction boundary.
        """
        entry = AuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            timestamp=datetime.now(UTC),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            model_used=model_used,
            tool_calls=tool_calls or [],
            request_summary=_truncate(request_summary),
            response_summary=_truncate(response_summary),
            latency_ms=latency_ms,
            status=status,
            error_detail=_truncate(error_detail, max_chars=1000),
            extra=extra or {},
        )
        self._db.add(entry)
        try:
            await self._db.flush()
        except Exception as exc:
            log.error("audit.write_failed", error=str(exc), action=action)
            # Do not re-raise - audit failure must not crash the request
        return entry


class RequestTimer:
    """Context manager to measure request latency in milliseconds.

    Usage:
        timer = RequestTimer()
        with timer:
            result = await do_work()
        latency = timer.elapsed_ms
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self.elapsed_ms: int = 0

    def __enter__(self) -> RequestTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
