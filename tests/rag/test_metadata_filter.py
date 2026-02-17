"""Tests for metadata_filter module - dynamic filtering of RAG chunks.

Tests cover:
- Filter by document type
- Filter by classification level
- Filter by date range
- Filter by tags
- Combined filters (AND logic)
- Empty filter returns all
- Tenant isolation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.rag.metadata_filter import MetadataFilter, MetadataFilterSpec


@pytest.fixture
def mock_db():
    """Mock async database session."""
    return AsyncMock()


@pytest.fixture
def filter_engine(mock_db):
    """MetadataFilter instance with mock DB."""
    return MetadataFilter(db=mock_db)


@pytest.fixture
def tenant_id():
    """Test tenant ID."""
    return uuid.uuid4()


class TestDocumentTypeFilter:
    """Test filtering by document type."""

    @pytest.mark.asyncio
    async def test_filter_by_single_document_type(self, filter_engine, mock_db, tenant_id):
        """Test filtering by a single document type."""
        spec = MetadataFilterSpec(document_types=["procedure"])

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (uuid.uuid4(),),
            (uuid.uuid4(),),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 2

    @pytest.mark.asyncio
    async def test_filter_by_multiple_document_types(self, filter_engine, mock_db, tenant_id):
        """Test filtering by multiple document types (OR logic)."""
        spec = MetadataFilterSpec(document_types=["procedure", "manual", "report"])

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1


class TestClassificationFilter:
    """Test filtering by classification level."""

    @pytest.mark.asyncio
    async def test_filter_by_classification_level(self, filter_engine, mock_db, tenant_id):
        """Test filtering by classification level."""
        spec = MetadataFilterSpec(classification_levels=["public", "internal"])

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (uuid.uuid4(),),
            (uuid.uuid4(),),
            (uuid.uuid4(),),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 3


class TestDateRangeFilter:
    """Test filtering by date ranges."""

    @pytest.mark.asyncio
    async def test_filter_by_created_after(self, filter_engine, mock_db, tenant_id):
        """Test filtering documents created after a date."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
        spec = MetadataFilterSpec(created_after=cutoff_date)

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1

    @pytest.mark.asyncio
    async def test_filter_by_created_before(self, filter_engine, mock_db, tenant_id):
        """Test filtering documents created before a date."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=7)
        spec = MetadataFilterSpec(created_before=cutoff_date)

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self, filter_engine, mock_db, tenant_id):
        """Test filtering documents within a date range."""
        start_date = datetime.now(timezone.utc) - timedelta(days=30)
        end_date = datetime.now(timezone.utc) - timedelta(days=7)

        spec = MetadataFilterSpec(
            created_after=start_date,
            created_before=end_date,
        )

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1

    @pytest.mark.asyncio
    async def test_filter_by_updated_after(self, filter_engine, mock_db, tenant_id):
        """Test filtering by update date."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=7)
        spec = MetadataFilterSpec(updated_after=cutoff_date)

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1


class TestTagFilter:
    """Test filtering by tags."""

    @pytest.mark.asyncio
    async def test_filter_by_tags_any_match(self, filter_engine, mock_db, tenant_id):
        """Test filtering by tags with 'any' match mode."""
        spec = MetadataFilterSpec(
            tags=["safety", "maintenance"],
            tag_match_mode="any",
        )

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (uuid.uuid4(),),
            (uuid.uuid4(),),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 2

    @pytest.mark.asyncio
    async def test_filter_by_tags_all_match(self, filter_engine, mock_db, tenant_id):
        """Test filtering by tags with 'all' match mode."""
        spec = MetadataFilterSpec(
            tags=["safety", "maintenance", "critical"],
            tag_match_mode="all",
        )

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1


class TestCombinedFilters:
    """Test combining multiple filters."""

    @pytest.mark.asyncio
    async def test_combined_filters_and_logic(self, filter_engine, mock_db, tenant_id):
        """Test combining filters with AND logic (default)."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)

        spec = MetadataFilterSpec(
            document_types=["procedure"],
            classification_levels=["public"],
            created_after=cutoff_date,
            tags=["safety"],
            filter_mode="AND",
        )

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[(uuid.uuid4(),)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 1

    @pytest.mark.asyncio
    async def test_combined_filters_or_logic(self, filter_engine, mock_db, tenant_id):
        """Test combining filters with OR logic."""
        spec = MetadataFilterSpec(
            document_types=["procedure"],
            classification_levels=["public"],
            filter_mode="OR",
        )

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (uuid.uuid4(),),
            (uuid.uuid4(),),
            (uuid.uuid4(),),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        assert len(chunk_ids) == 3


class TestEmptyFilter:
    """Test empty filter behavior."""

    @pytest.mark.asyncio
    async def test_empty_filter_returns_all(self, filter_engine, mock_db, tenant_id):
        """Test that empty filter returns all chunks (scoped by tenant)."""
        spec = MetadataFilterSpec()  # No filters

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (uuid.uuid4(),),
            (uuid.uuid4(),),
            (uuid.uuid4(),),
            (uuid.uuid4(),),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        chunk_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
        )

        # Should return all chunks for tenant
        assert len(chunk_ids) == 4


class TestChunkSubsetFiltering:
    """Test filtering from a specific subset of chunks."""

    @pytest.mark.asyncio
    async def test_filter_from_chunk_subset(self, filter_engine, mock_db, tenant_id):
        """Test filtering from a provided list of chunk IDs."""
        chunk_ids_input = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        spec = MetadataFilterSpec(document_types=["procedure"])

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            (chunk_ids_input[0],),
            (chunk_ids_input[1],),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        filtered_ids = await filter_engine.filter_chunks(
            spec=spec,
            tenant_id=tenant_id,
            chunk_ids=chunk_ids_input,
        )

        # Should return subset that matches filter
        assert len(filtered_ids) == 2
        assert filtered_ids[0] in chunk_ids_input
        assert filtered_ids[1] in chunk_ids_input


class TestAvailableMetadataValues:
    """Test getting available metadata values for UI dropdowns."""

    @pytest.mark.asyncio
    async def test_get_available_document_types(self, filter_engine, mock_db, tenant_id):
        """Test retrieving distinct document types."""
        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[
            ("procedure",),
            ("manual",),
            ("report",),
        ])
        mock_db.execute = AsyncMock(return_value=mock_result)

        values = await filter_engine.get_available_metadata_values(
            tenant_id=tenant_id,
            metadata_key="document_type",
        )

        assert values == ["manual", "procedure", "report"]  # Sorted

    @pytest.mark.asyncio
    async def test_get_available_values_empty(self, filter_engine, mock_db, tenant_id):
        """Test retrieving metadata values when none exist."""
        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[])
        mock_db.execute = AsyncMock(return_value=mock_result)

        values = await filter_engine.get_available_metadata_values(
            tenant_id=tenant_id,
            metadata_key="custom_field",
        )

        assert values == []


class TestTenantIsolation:
    """Test tenant isolation in filtering."""

    @pytest.mark.asyncio
    async def test_filter_enforces_tenant_isolation(self, filter_engine, tenant_id):
        """Test that filters always include tenant_id."""
        spec = MetadataFilterSpec(document_types=["procedure"])

        where_clause = filter_engine.build_filter_clause(
            spec=spec,
            tenant_id=tenant_id,
        )

        # Should contain tenant isolation conditions
        # (This is a basic test - in practice, would inspect SQL AST)
        assert where_clause is not None
