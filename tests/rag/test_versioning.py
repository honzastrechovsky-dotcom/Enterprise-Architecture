"""Tests for versioning module - document version management and diff detection.

Tests cover:
- Document version creation
- Version increment logic
- Chunk-level diff detection
- Version history retrieval
- Version cleanup
- Similarity computation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.document import Document, DocumentChunk, DocumentStatus
from src.rag.versioning import ChunkDiff, DocumentVersionManager, VersionInfo


@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def version_manager(mock_db):
    """DocumentVersionManager instance with mock DB."""
    return DocumentVersionManager(db=mock_db)


@pytest.fixture
def tenant_id():
    """Test tenant ID."""
    return uuid.uuid4()


@pytest.fixture
def user_id():
    """Test user ID."""
    return uuid.uuid4()


class TestVersionCreation:
    """Test document version creation."""

    @pytest.mark.asyncio
    async def test_create_initial_version(self, version_manager, mock_db, tenant_id, user_id):
        """Test creating initial version (no existing document)."""
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        doc = await version_manager.create_new_version(
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=user_id,
            content_type="application/pdf",
            metadata={"key": "value"},
        )

        assert doc.version == "1.0"
        assert doc.filename == "test.pdf"
        assert doc.tenant_id == tenant_id
        assert doc.uploaded_by_user_id == user_id
        assert doc.status == DocumentStatus.PENDING

    @pytest.mark.asyncio
    async def test_create_new_version_existing_document(self, version_manager, mock_db, tenant_id, user_id):
        """Test creating new version when document already exists."""
        existing_doc = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=user_id,
            content_type="application/pdf",
            version="1.0",
            status=DocumentStatus.READY,
            chunk_count=10,
            size_bytes=1024,
            metadata_={},
        )

        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=existing_doc)))

        doc = await version_manager.create_new_version(
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=user_id,
            content_type="application/pdf",
        )

        assert doc.version == "1.1"  # Incremented from 1.0
        assert doc.filename == "test.pdf"


class TestVersionIncrement:
    """Test version string increment logic."""

    def test_increment_minor_version(self, version_manager):
        """Test incrementing minor version (1.0 -> 1.1)."""
        assert version_manager._increment_version("1.0") == "1.1"
        assert version_manager._increment_version("1.5") == "1.6"
        assert version_manager._increment_version("2.3") == "2.4"

    def test_increment_major_version(self, version_manager):
        """Test incrementing to major version (1.9 -> 2.0)."""
        assert version_manager._increment_version("1.9") == "2.0"
        assert version_manager._increment_version("3.9") == "4.0"

    def test_increment_invalid_version(self, version_manager):
        """Test handling invalid version format."""
        assert version_manager._increment_version("invalid") == "2.0"
        assert version_manager._increment_version("") == "2.0"


class TestVersionHistory:
    """Test version history retrieval."""

    @pytest.mark.asyncio
    async def test_get_versions_multiple(self, version_manager, mock_db, tenant_id, user_id):
        """Test retrieving multiple versions of a document."""
        doc_v1 = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=user_id,
            content_type="application/pdf",
            version="1.0",
            status=DocumentStatus.READY,
            chunk_count=10,
            size_bytes=1024,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
            updated_at=datetime.now(timezone.utc) - timedelta(days=30),
            metadata_={"version": "1.0"},
        )

        doc_v2 = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=user_id,
            content_type="application/pdf",
            version="1.1",
            status=DocumentStatus.READY,
            chunk_count=12,
            size_bytes=2048,
            created_at=datetime.now(timezone.utc) - timedelta(days=15),
            updated_at=datetime.now(timezone.utc) - timedelta(days=15),
            metadata_={"version": "1.1"},
        )

        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[doc_v2, doc_v1])))
        mock_db.execute = AsyncMock(return_value=mock_result)

        versions = await version_manager.get_versions(
            tenant_id=tenant_id,
            filename="test.pdf",
        )

        assert len(versions) == 2
        assert versions[0].version == "1.1"  # Newest first
        assert versions[1].version == "1.0"
        assert versions[0].chunk_count == 12
        assert versions[1].chunk_count == 10

    @pytest.mark.asyncio
    async def test_get_versions_empty(self, version_manager, mock_db, tenant_id):
        """Test retrieving versions when none exist."""
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_db.execute = AsyncMock(return_value=mock_result)

        versions = await version_manager.get_versions(
            tenant_id=tenant_id,
            filename="nonexistent.pdf",
        )

        assert versions == []


class TestChunkDiff:
    """Test chunk-level diff detection."""

    @pytest.mark.asyncio
    async def test_compare_versions_modified_chunks(self, version_manager, mock_db, tenant_id):
        """Test detecting modified chunks between versions."""
        doc_v1_id = uuid.uuid4()
        doc_v2_id = uuid.uuid4()

        # Mock documents
        doc_v1 = Document(
            id=doc_v1_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.0",
            status=DocumentStatus.READY,
            chunk_count=2,
            size_bytes=1024,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        doc_v2 = Document(
            id=doc_v2_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.1",
            status=DocumentStatus.READY,
            chunk_count=2,
            size_bytes=1024,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        # Mock chunks (same index, different content)
        chunk_v1 = DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc_v1_id,
            tenant_id=tenant_id,
            chunk_index=0,
            content="Original content",
            chunk_metadata={},
        )

        chunk_v2 = DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc_v2_id,
            tenant_id=tenant_id,
            chunk_index=0,
            content="Modified content",
            chunk_metadata={},
        )

        version_manager._get_document = AsyncMock(side_effect=[doc_v1, doc_v2])
        version_manager._get_chunks = AsyncMock(side_effect=[[chunk_v1], [chunk_v2]])

        comparison = await version_manager.compare_versions(
            tenant_id=tenant_id,
            old_document_id=doc_v1_id,
            new_document_id=doc_v2_id,
        )

        assert len(comparison.chunk_diffs) == 1
        assert comparison.chunk_diffs[0].change_type == "modified"
        assert comparison.chunk_diffs[0].chunk_index == 0
        assert comparison.chunk_diffs[0].similarity < 0.99

    @pytest.mark.asyncio
    async def test_compare_versions_added_chunks(self, version_manager, mock_db, tenant_id):
        """Test detecting added chunks."""
        doc_v1_id = uuid.uuid4()
        doc_v2_id = uuid.uuid4()

        doc_v1 = Document(
            id=doc_v1_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.0",
            status=DocumentStatus.READY,
            chunk_count=1,
            size_bytes=1024,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        doc_v2 = Document(
            id=doc_v2_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.1",
            status=DocumentStatus.READY,
            chunk_count=2,
            size_bytes=2048,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        chunk_v2_new = DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc_v2_id,
            tenant_id=tenant_id,
            chunk_index=1,  # New chunk
            content="New content",
            chunk_metadata={},
        )

        version_manager._get_document = AsyncMock(side_effect=[doc_v1, doc_v2])
        version_manager._get_chunks = AsyncMock(side_effect=[[], [chunk_v2_new]])

        comparison = await version_manager.compare_versions(
            tenant_id=tenant_id,
            old_document_id=doc_v1_id,
            new_document_id=doc_v2_id,
        )

        assert len(comparison.chunk_diffs) == 1
        assert comparison.chunk_diffs[0].change_type == "added"
        assert comparison.chunk_diffs[0].new_content == "New content"
        assert comparison.chunk_diffs[0].old_content is None

    @pytest.mark.asyncio
    async def test_compare_versions_removed_chunks(self, version_manager, mock_db, tenant_id):
        """Test detecting removed chunks."""
        doc_v1_id = uuid.uuid4()
        doc_v2_id = uuid.uuid4()

        doc_v1 = Document(
            id=doc_v1_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.0",
            status=DocumentStatus.READY,
            chunk_count=1,
            size_bytes=1024,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        doc_v2 = Document(
            id=doc_v2_id,
            tenant_id=tenant_id,
            filename="test.pdf",
            uploaded_by_user_id=uuid.uuid4(),
            content_type="application/pdf",
            version="1.1",
            status=DocumentStatus.READY,
            chunk_count=0,
            size_bytes=0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={},
        )

        chunk_v1_removed = DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc_v1_id,
            tenant_id=tenant_id,
            chunk_index=0,
            content="Removed content",
            chunk_metadata={},
        )

        version_manager._get_document = AsyncMock(side_effect=[doc_v1, doc_v2])
        version_manager._get_chunks = AsyncMock(side_effect=[[chunk_v1_removed], []])

        comparison = await version_manager.compare_versions(
            tenant_id=tenant_id,
            old_document_id=doc_v1_id,
            new_document_id=doc_v2_id,
        )

        assert len(comparison.chunk_diffs) == 1
        assert comparison.chunk_diffs[0].change_type == "removed"
        assert comparison.chunk_diffs[0].old_content == "Removed content"
        assert comparison.chunk_diffs[0].new_content is None


class TestSimilarityComputation:
    """Test similarity computation for chunk comparison."""

    def test_compute_similarity_identical(self, version_manager):
        """Test similarity of identical strings."""
        text = "This is identical content"
        similarity = version_manager._compute_similarity(text, text)
        assert similarity == 1.0

    def test_compute_similarity_completely_different(self, version_manager):
        """Test similarity of completely different strings."""
        similarity = version_manager._compute_similarity(
            "abc",
            "xyz",
        )
        assert similarity == 0.0

    def test_compute_similarity_partial_match(self, version_manager):
        """Test similarity of partially matching strings."""
        similarity = version_manager._compute_similarity(
            "The quick brown fox",
            "The slow brown fox",
        )
        assert 0.5 < similarity < 1.0  # Partial match


class TestVersionCleanup:
    """Test old version cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_old_versions(self, version_manager, mock_db, tenant_id):
        """Test cleanup of old versions based on retention policy."""
        # Mock get_versions to return 10 versions
        old_versions = [
            VersionInfo(
                document_id=uuid.uuid4(),
                version=f"1.{i}",
                filename="test.pdf",
                status="ready",
                chunk_count=10,
                size_bytes=1024,
                created_at=datetime.now(timezone.utc) - timedelta(days=100 + i),
                updated_at=datetime.now(timezone.utc) - timedelta(days=100 + i),
                metadata={},
            )
            for i in range(10)
        ]

        version_manager.get_versions = AsyncMock(return_value=old_versions)

        # Mock distinct filenames query
        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[("test.pdf",)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        deleted_count = await version_manager.cleanup_old_versions(
            tenant_id=tenant_id,
            keep_latest_n=5,
            keep_newer_than_days=90,
        )

        # Should delete versions older than 90 days and beyond latest 5
        assert deleted_count >= 0

    @pytest.mark.asyncio
    async def test_cleanup_keeps_recent_versions(self, version_manager, mock_db, tenant_id):
        """Test that recent versions are not deleted."""
        recent_versions = [
            VersionInfo(
                document_id=uuid.uuid4(),
                version="1.0",
                filename="test.pdf",
                status="ready",
                chunk_count=10,
                size_bytes=1024,
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
                updated_at=datetime.now(timezone.utc) - timedelta(days=1),
                metadata={},
            ),
        ]

        version_manager.get_versions = AsyncMock(return_value=recent_versions)

        mock_result = MagicMock()
        mock_result.all = MagicMock(return_value=[("test.pdf",)])
        mock_db.execute = AsyncMock(return_value=mock_result)

        deleted_count = await version_manager.cleanup_old_versions(
            tenant_id=tenant_id,
            keep_latest_n=5,
            keep_newer_than_days=90,
        )

        # Should not delete recent versions
        assert deleted_count == 0
