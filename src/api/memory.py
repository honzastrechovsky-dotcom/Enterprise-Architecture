"""Agent Memory API endpoints.

POST   /api/v1/agents/{agent_id}/memories              - Store a memory
GET    /api/v1/agents/{agent_id}/memories              - List memories (with filters)
GET    /api/v1/agents/{agent_id}/memories/search       - Semantic/text search
DELETE /api/v1/agents/{agent_id}/memories/{memory_id}  - Soft-delete (forget)
POST   /api/v1/agents/{agent_id}/memories/compact      - Trigger compaction
GET    /api/v1/agents/{agent_id}/memories/stats        - Memory statistics

All endpoints are tenant-scoped; agent_id is treated as a stable identifier
within the tenant. Viewers can read; operators and above can write/delete.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.models.agent_memory import AgentMemory, MemoryType
from src.services.memory import AgentMemoryService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agent-memory"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class StoreMemoryRequest(BaseModel):
    memory_type: MemoryType = Field(..., description="Type of memory to store")
    content: str = Field(..., min_length=1, max_length=8192, description="Memory content")
    importance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Importance score (0.0-1.0)",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional expiry timestamp (ISO 8601)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Freeform metadata dict",
    )


class MemoryResponse(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    memory_type: MemoryType
    content: str
    importance_score: float
    access_count: int
    last_accessed_at: datetime | None
    expires_at: datetime | None
    metadata: dict[str, Any]
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, obj: AgentMemory) -> MemoryResponse:
        return cls(
            id=obj.id,
            agent_id=obj.agent_id,
            memory_type=obj.memory_type,
            content=obj.content,
            importance_score=obj.importance_score,
            access_count=obj.access_count,
            last_accessed_at=obj.last_accessed_at,
            expires_at=obj.expires_at,
            metadata=obj.metadata_ or {},
            is_deleted=obj.is_deleted,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )


class CompactRequest(BaseModel):
    max_memories: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum active memories to retain",
    )


class CompactResponse(BaseModel):
    deleted: int
    remaining: int


class MemoryStatsResponse(BaseModel):
    total: int
    avg_importance: float
    by_type: dict[str, int]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/memories",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a new memory for an agent",
)
async def store_memory(
    agent_id: uuid.UUID,
    body: StoreMemoryRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MemoryResponse:
    """Persist a new typed memory entry for the specified agent.

    All memories are scoped to the caller's tenant. Any authenticated
    operator or admin can store memories.
    """
    check_permission(current_user.role, Permission.CHAT_SEND)

    service = AgentMemoryService(db)
    memory = await service.store_memory(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        memory_type=body.memory_type,
        content=body.content,
        importance=body.importance,
        expires_at=body.expires_at,
        metadata=body.metadata,
    )

    log.info(
        "api.memory.stored",
        memory_id=str(memory.id),
        agent_id=str(agent_id),
        tenant_id=str(current_user.tenant_id),
    )

    return MemoryResponse.from_orm(memory)


@router.get(
    "/{agent_id}/memories",
    response_model=list[MemoryResponse],
    summary="List memories for an agent",
)
async def list_memories(
    agent_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    memory_type: MemoryType | None = Query(
        default=None,
        description="Filter by memory type",
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MemoryResponse]:
    """List active memories for an agent, optionally filtered by type.

    Results are ordered by importance_score descending.
    """
    check_permission(current_user.role, Permission.CHAT_SEND)

    service = AgentMemoryService(db)

    if memory_type is not None:
        memories = await service.recall_by_type(
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
            memory_type=memory_type,
            limit=limit,
        )
    else:
        # Return all active memories ordered by importance
        memories = await service.recall_memories(
            tenant_id=current_user.tenant_id,
            agent_id=agent_id,
            query="",
            limit=limit,
        )

    return [MemoryResponse.from_orm(m) for m in memories]


@router.get(
    "/{agent_id}/memories/search",
    response_model=list[MemoryResponse],
    summary="Semantic/text search across agent memories",
)
async def search_memories(
    agent_id: uuid.UUID,
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[MemoryResponse]:
    """Search agent memories using text similarity.

    Uses pgvector semantic search when embeddings are available,
    falls back to ilike text search otherwise.
    """
    check_permission(current_user.role, Permission.CHAT_SEND)

    service = AgentMemoryService(db)
    memories = await service.recall_memories(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        query=q,
        limit=limit,
    )

    return [MemoryResponse.from_orm(m) for m in memories]


@router.delete(
    "/{agent_id}/memories/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete (forget) a memory",
)
async def forget_memory(
    agent_id: uuid.UUID,
    memory_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Soft-delete a specific memory (sets is_deleted=True).

    The memory record is preserved in the database for audit purposes
    but excluded from all future recall operations.
    """
    check_permission(current_user.role, Permission.CONVERSATION_DELETE)

    service = AgentMemoryService(db)
    deleted = await service.forget(
        memory_id=memory_id,
        tenant_id=current_user.tenant_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found",
        )

    log.info(
        "api.memory.forgotten",
        memory_id=str(memory_id),
        agent_id=str(agent_id),
        tenant_id=str(current_user.tenant_id),
    )


@router.post(
    "/{agent_id}/memories/compact",
    response_model=CompactResponse,
    summary="Compact agent memory - prune old low-importance entries",
)
async def compact_memories(
    agent_id: uuid.UUID,
    body: CompactRequest = CompactRequest(),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> CompactResponse:
    """Trigger memory compaction for an agent.

    Soft-deletes the oldest, lowest-importance memories when the total
    count exceeds max_memories. Returns the number deleted and remaining.
    """
    check_permission(current_user.role, Permission.CONVERSATION_DELETE)

    service = AgentMemoryService(db)
    result = await service.compact_memories(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
        max_memories=body.max_memories,
    )

    log.info(
        "api.memory.compacted",
        agent_id=str(agent_id),
        tenant_id=str(current_user.tenant_id),
        deleted=result["deleted"],
        remaining=result["remaining"],
    )

    return CompactResponse(**result)


@router.get(
    "/{agent_id}/memories/stats",
    response_model=MemoryStatsResponse,
    summary="Get memory statistics for an agent",
)
async def memory_stats(
    agent_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MemoryStatsResponse:
    """Return aggregate statistics for an agent's memory store.

    Includes total active count, average importance score, and a
    breakdown of memory counts by type.
    """
    check_permission(current_user.role, Permission.CHAT_SEND)

    service = AgentMemoryService(db)
    stats = await service.get_memory_stats(
        tenant_id=current_user.tenant_id,
        agent_id=agent_id,
    )

    return MemoryStatsResponse(**stats)
