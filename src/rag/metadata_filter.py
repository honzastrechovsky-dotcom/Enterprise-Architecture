"""Metadata-based filtering for RAG retrieval.

Supports filtering chunks by document-level and chunk-level metadata:
- Document type (e.g., "procedure", "report", "manual")
- Classification level (e.g., "public", "internal", "confidential")
- Date range (document creation/update)
- Author/uploader
- Plant/facility ID
- Custom tags

Filters can be combined with AND/OR logic and applied before or after
vector similarity search.

Design decisions:
- Dynamic WHERE clause building using SQLAlchemy
- Type-safe filter definitions via dataclasses
- Supports JSONB metadata querying for custom fields
- All queries scoped by tenant_id
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from src.models.document import Document, DocumentChunk

log = structlog.get_logger(__name__)


@dataclass
class MetadataFilterSpec:
    """Specification for metadata-based filtering.

    All filters are optional and combined with AND by default.
    Use filter_mode="OR" for OR combination.
    """

    # Document-level filters
    document_types: list[str] = field(default_factory=list)
    classification_levels: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    plant_ids: list[str] = field(default_factory=list)

    # Date range filters
    created_after: datetime | None = None
    created_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None

    # Chunk-level metadata filters (JSONB queries)
    chunk_metadata_filters: dict[str, Any] = field(default_factory=dict)

    # Tag filtering
    tags: list[str] = field(default_factory=list)
    tag_match_mode: str = "any"  # "any" or "all"

    # Combination mode
    filter_mode: str = "AND"  # "AND" or "OR"


class MetadataFilter:
    """Filter document chunks by metadata before/after retrieval."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize metadata filter.

        Args:
            db: Async database session
        """
        self._db = db

    def build_filter_clause(
        self,
        *,
        spec: MetadataFilterSpec,
        tenant_id: uuid.UUID,
    ) -> Any:  # Returns SQLAlchemy BinaryExpression
        """Build SQLAlchemy WHERE clause from filter specification.

        Args:
            spec: Filter specification
            tenant_id: Tenant ID for isolation

        Returns:
            SQLAlchemy WHERE clause expression
        """
        conditions = []

        # Mandatory tenant isolation
        conditions.append(Document.tenant_id == tenant_id)
        conditions.append(DocumentChunk.tenant_id == tenant_id)

        # Document type filter
        if spec.document_types:
            # Assumes metadata_['document_type'] exists
            type_conditions = [
                Document.metadata_["document_type"].astext == dt
                for dt in spec.document_types
            ]
            if type_conditions:
                conditions.append(or_(*type_conditions))

        # Classification level filter
        if spec.classification_levels:
            level_conditions = [
                Document.metadata_["classification_level"].astext == cl
                for cl in spec.classification_levels
            ]
            if level_conditions:
                conditions.append(or_(*level_conditions))

        # Author filter
        if spec.authors:
            author_conditions = [
                Document.metadata_["author"].astext == author
                for author in spec.authors
            ]
            if author_conditions:
                conditions.append(or_(*author_conditions))

        # Plant ID filter
        if spec.plant_ids:
            plant_conditions = [
                Document.metadata_["plant_id"].astext == plant_id
                for plant_id in spec.plant_ids
            ]
            if plant_conditions:
                conditions.append(or_(*plant_conditions))

        # Date range filters
        if spec.created_after:
            conditions.append(Document.created_at >= spec.created_after)
        if spec.created_before:
            conditions.append(Document.created_at <= spec.created_before)
        if spec.updated_after:
            conditions.append(Document.updated_at >= spec.updated_after)
        if spec.updated_before:
            conditions.append(Document.updated_at <= spec.updated_before)

        # Tag filtering
        if spec.tags:
            if spec.tag_match_mode == "all":
                # All tags must be present
                for tag in spec.tags:
                    conditions.append(Document.metadata_["tags"].astext.contains(tag))
            else:
                # Any tag matches
                tag_conditions = [
                    Document.metadata_["tags"].astext.contains(tag)
                    for tag in spec.tags
                ]
                if tag_conditions:
                    conditions.append(or_(*tag_conditions))

        # Chunk-level metadata filters
        for key, value in spec.chunk_metadata_filters.items():
            conditions.append(DocumentChunk.chunk_metadata[key].astext == str(value))

        # Combine with AND or OR
        if spec.filter_mode == "OR":
            # Skip tenant conditions (always required)
            tenant_conds = conditions[:2]
            other_conds = conditions[2:]
            if other_conds:
                return and_(*tenant_conds, or_(*other_conds))
            else:
                return and_(*conditions)
        else:
            return and_(*conditions)

    async def filter_chunks(
        self,
        *,
        spec: MetadataFilterSpec,
        tenant_id: uuid.UUID,
        chunk_ids: list[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """Filter chunks by metadata, optionally from a subset.

        Args:
            spec: Filter specification
            tenant_id: Tenant ID for isolation
            chunk_ids: Optional list of chunk IDs to filter from (if None, filters all)

        Returns:
            List of chunk IDs that match the filter criteria
        """
        log.debug(
            "metadata_filter.start",
            tenant_id=str(tenant_id),
            input_count=len(chunk_ids) if chunk_ids else "all",
        )

        # Build base query
        query: Select = select(DocumentChunk.id).join(
            Document, Document.id == DocumentChunk.document_id
        )

        # Apply metadata filters
        where_clause = self.build_filter_clause(spec=spec, tenant_id=tenant_id)
        query = query.where(where_clause)

        # Optionally filter from subset
        if chunk_ids:
            query = query.where(DocumentChunk.id.in_(chunk_ids))

        # Execute
        result = await self._db.execute(query)
        filtered_ids = [row[0] for row in result.all()]

        log.debug(
            "metadata_filter.complete",
            tenant_id=str(tenant_id),
            output_count=len(filtered_ids),
        )

        return filtered_ids

    async def get_available_metadata_values(
        self,
        *,
        tenant_id: uuid.UUID,
        metadata_key: str,
    ) -> list[str]:
        """Get all distinct values for a metadata key (for UI dropdowns).

        Args:
            tenant_id: Tenant ID for isolation
            metadata_key: Metadata key to query (e.g., "document_type", "author")

        Returns:
            List of distinct values for the metadata key
        """
        # Query distinct values from Document.metadata_
        query = (
            select(Document.metadata_[metadata_key].astext.distinct())
            .where(Document.tenant_id == tenant_id)
            .where(Document.metadata_[metadata_key].isnot(None))
        )

        result = await self._db.execute(query)
        values = [row[0] for row in result.all() if row[0]]

        log.debug(
            "metadata_filter.available_values",
            tenant_id=str(tenant_id),
            metadata_key=metadata_key,
            count=len(values),
        )

        return sorted(values)


async def metadata_filter_from_context(db: AsyncSession) -> MetadataFilter:
    """Factory for MetadataFilter - used by tool gateway."""
    return MetadataFilter(db=db)
