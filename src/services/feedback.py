"""Feedback service for managing user response ratings.

Provides methods to submit, query, and export user feedback on agent responses.
All operations are tenant-scoped for multi-tenant isolation.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_memory import MemoryType
from src.models.feedback import FeedbackRating, ResponseFeedback
from src.services.memory import AgentMemoryService

# Markers indicating a prompt-injection attempt in user-supplied text.
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all",
    "disregard",
    "forget the above",
    "system:",
    "assistant:",
)


def _is_injection_attempt(text: str) -> bool:
    """Return True if text contains a known prompt-injection pattern."""
    lower = text.lower()
    return any(marker in lower for marker in _INJECTION_MARKERS)

log = structlog.get_logger(__name__)

# Stable orchestrator agent ID used for cross-session skill memories
_ORCHESTRATOR_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Ratings that classify as negative feedback
_NEGATIVE_RATINGS = {FeedbackRating.THUMBS_DOWN, FeedbackRating.RATING_1, FeedbackRating.RATING_2}
# Ratings that classify as positive feedback
_POSITIVE_RATINGS = {FeedbackRating.THUMBS_UP, FeedbackRating.RATING_4, FeedbackRating.RATING_5}


class FeedbackService:
    """Service for managing user feedback on agent responses."""

    def __init__(self, db: AsyncSession):
        """Initialize feedback service with database session.

        Args:
            db: Async database session
        """
        self.db = db

    async def submit_feedback(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        rating: str,
        prompt_text: str,
        response_text: str,
        model_used: str,
        comment: str | None = None,
        tags: list[str] | None = None,
        conversation_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        trace_id: str | None = None,
    ) -> uuid.UUID:
        """Submit user feedback on an agent response.

        Args:
            tenant_id: Tenant UUID
            user_id: User UUID who submitted feedback
            rating: Rating value (thumbs_up/down or rating_1-5)
            prompt_text: User's original prompt
            response_text: Agent's response
            model_used: Model identifier
            comment: Optional feedback comment
            tags: Optional feedback tags
            conversation_id: Optional conversation link
            message_id: Optional message link
            trace_id: Optional distributed trace ID

        Returns:
            UUID of created feedback record
        """
        feedback = ResponseFeedback(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            rating=FeedbackRating(rating),
            prompt_text=prompt_text,
            response_text=response_text,
            model_used=model_used,
            comment=comment,
            tags=tags or [],
            conversation_id=conversation_id,
            message_id=message_id,
            trace_id=trace_id,
        )

        self.db.add(feedback)
        await self.db.flush()

        log.info(
            "feedback_submitted",
            feedback_id=str(feedback.id),
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            rating=rating,
            model=model_used,
        )

        # 11C1: Store memories derived from this feedback (fail-open)
        try:
            await self._store_feedback_memories(
                feedback=feedback,
                agent_id=_ORCHESTRATOR_AGENT_ID,
            )
        except Exception as exc:
            log.warning("feedback.memory_store_failed", error=str(exc))

        return feedback.id

    async def _store_feedback_memories(
        self,
        feedback: ResponseFeedback,
        agent_id: uuid.UUID,
    ) -> None:
        """Store agent memories derived from this feedback record.

        Negative feedback (rating <= 2 / thumbs_down):
          - FACT memory: what went wrong
          - PREFERENCE memory: inferred user preference (if identifiable)

        Positive feedback (rating >= 4 / thumbs_up):
          - SKILL memory: what approach worked well

        Args:
            feedback: The submitted ResponseFeedback record
            agent_id: Agent to scope these memories to (typically orchestrator)
        """
        memory_service = AgentMemoryService(self.db)
        rating_val = feedback.rating

        if rating_val in _NEGATIVE_RATINGS:
            # Derive a short description from the comment or prompt
            description = (feedback.comment or feedback.prompt_text[:120]).strip()
            if _is_injection_attempt(description):
                log.warning(
                    "feedback.injection_attempt_blocked",
                    feedback_id=str(feedback.id),
                    field="description",
                )
                fact_content = "User gave feedback — content filtered for security"
            else:
                fact_content = f"User gave negative feedback: {description}"
            await memory_service.store_memory(
                tenant_id=feedback.tenant_id,
                agent_id=agent_id,
                memory_type=MemoryType.FACT,
                content=fact_content,
                importance=0.7,
                metadata={
                    "source": "feedback",
                    "feedback_id": str(feedback.id),
                    "rating": rating_val,
                },
            )

            # Infer preference from comment if present
            if feedback.comment:
                if _is_injection_attempt(feedback.comment):
                    log.warning(
                        "feedback.injection_attempt_blocked",
                        feedback_id=str(feedback.id),
                        field="comment",
                    )
                    pref_content = "User gave feedback — content filtered for security"
                else:
                    pref_content = f"User preference (from negative feedback): {feedback.comment[:200]}"
                await memory_service.store_memory(
                    tenant_id=feedback.tenant_id,
                    agent_id=agent_id,
                    memory_type=MemoryType.PREFERENCE,
                    content=pref_content,
                    importance=0.65,
                    metadata={
                        "source": "feedback",
                        "feedback_id": str(feedback.id),
                        "rating": rating_val,
                    },
                )

            log.info(
                "feedback.negative_memories_stored",
                feedback_id=str(feedback.id),
                rating=rating_val,
            )

        elif rating_val in _POSITIVE_RATINGS:
            # Extract topic from prompt for skill memory
            topic = feedback.prompt_text[:120].strip()
            skill_content = (
                f"agent_id:{feedback.model_used} worked well for topic: {topic}"
            )
            await memory_service.store_memory(
                tenant_id=feedback.tenant_id,
                agent_id=agent_id,
                memory_type=MemoryType.SKILL,
                content=skill_content,
                importance=0.6,
                metadata={
                    "source": "feedback",
                    "feedback_id": str(feedback.id),
                    "rating": rating_val,
                    "model_used": feedback.model_used,
                },
            )

            log.info(
                "feedback.positive_memory_stored",
                feedback_id=str(feedback.id),
                rating=rating_val,
            )

    async def list_feedback(
        self,
        tenant_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        rating: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """List feedback for a tenant with optional filters.

        Args:
            tenant_id: Tenant UUID
            limit: Maximum number of results
            offset: Pagination offset
            rating: Filter by specific rating
            date_from: Filter by start date
            date_to: Filter by end date

        Returns:
            List of feedback dictionaries
        """
        stmt = select(ResponseFeedback).where(ResponseFeedback.tenant_id == tenant_id)

        # Apply filters
        if rating:
            stmt = stmt.where(ResponseFeedback.rating == FeedbackRating(rating))
        if date_from:
            stmt = stmt.where(ResponseFeedback.created_at >= date_from)
        if date_to:
            stmt = stmt.where(ResponseFeedback.created_at <= date_to)

        # Order and paginate
        stmt = stmt.order_by(desc(ResponseFeedback.created_at)).offset(offset).limit(limit)

        result = await self.db.execute(stmt)
        feedback_records = result.scalars().all()

        return [
            {
                "id": f.id,
                "tenant_id": f.tenant_id,
                "user_id": f.user_id,
                "conversation_id": f.conversation_id,
                "message_id": f.message_id,
                "trace_id": f.trace_id,
                "rating": f.rating.value,
                "comment": f.comment,
                "tags": f.tags,
                "prompt_text": f.prompt_text,
                "response_text": f.response_text,
                "model_used": f.model_used,
                "created_at": f.created_at,
            }
            for f in feedback_records
        ]

    async def get_feedback_stats(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Get aggregated feedback statistics for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Dictionary with stats: total_count, positive_rate, top_tags, by_model
        """
        # Total count
        total_stmt = select(func.count(ResponseFeedback.id)).where(
            ResponseFeedback.tenant_id == tenant_id
        )
        total_result = await self.db.execute(total_stmt)
        total_count = total_result.scalar() or 0

        # Positive count (thumbs_up or rating_4/5)
        positive_ratings = [
            FeedbackRating.THUMBS_UP,
            FeedbackRating.RATING_4,
            FeedbackRating.RATING_5,
        ]
        positive_stmt = select(func.count(ResponseFeedback.id)).where(
            and_(
                ResponseFeedback.tenant_id == tenant_id,
                ResponseFeedback.rating.in_(positive_ratings),
            )
        )
        positive_result = await self.db.execute(positive_stmt)
        positive_count = positive_result.scalar() or 0

        positive_rate = positive_count / total_count if total_count > 0 else 0.0

        # Top tags (using JSONB array elements)
        # This is a simplified approach; in production might use more complex SQL
        tags_stmt = select(ResponseFeedback.tags).where(
            ResponseFeedback.tenant_id == tenant_id
        )
        tags_result = await self.db.execute(tags_stmt)
        all_tags: dict[str, int] = {}
        for (tags_list,) in tags_result:
            for tag in tags_list:
                all_tags[tag] = all_tags.get(tag, 0) + 1

        top_tags = [
            {"tag": tag, "count": count}
            for tag, count in sorted(all_tags.items(), key=lambda x: x[1], reverse=True)[:10]
        ]

        # By model breakdown
        model_stmt = (
            select(
                ResponseFeedback.model_used,
                func.count(ResponseFeedback.id).label("count"),
                func.sum(
                    case(
                        (ResponseFeedback.rating.in_(positive_ratings), 1),
                        else_=0,
                    )
                ).label("positive_count"),
            )
            .where(ResponseFeedback.tenant_id == tenant_id)
            .group_by(ResponseFeedback.model_used)
        )
        model_result = await self.db.execute(model_stmt)
        by_model = {}
        for row in model_result:
            model = row.model_used
            count = row.count
            pos_count = row.positive_count or 0
            by_model[model] = {
                "count": count,
                "positive_rate": pos_count / count if count > 0 else 0.0,
            }

        return {
            "total_count": total_count,
            "positive_rate": positive_rate,
            "top_tags": top_tags,
            "by_model": by_model,
        }

    async def export_feedback(
        self,
        tenant_id: uuid.UUID,
        format: str = "jsonl",
        rating: str | None = None,
    ) -> str:
        """Export feedback data in specified format.

        Args:
            tenant_id: Tenant UUID
            format: Export format (currently only "jsonl" supported)
            rating: Optional filter by rating

        Returns:
            JSONL string with one JSON object per line
        """
        stmt = select(ResponseFeedback).where(ResponseFeedback.tenant_id == tenant_id)

        if rating:
            stmt = stmt.where(ResponseFeedback.rating == FeedbackRating(rating))

        stmt = stmt.order_by(ResponseFeedback.created_at)

        result = await self.db.execute(stmt)
        feedback_records = result.scalars().all()

        # Export as JSONL
        lines = []
        for f in feedback_records:
            obj = {
                "prompt": f.prompt_text,
                "response": f.response_text,
                "rating": f.rating.value,
                "model": f.model_used,
                "tags": f.tags,
                "comment": f.comment,
                "created_at": f.created_at.isoformat(),
            }
            lines.append(json.dumps(obj))

        return "\n".join(lines) + "\n" if lines else ""
