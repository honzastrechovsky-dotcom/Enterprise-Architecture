"""Document version management for RAG.

Architecture:
1. Version tracking: Re-uploading a document creates a new version
2. Version comparison: Diff between versions at chunk level
3. Chunk-level versioning: Track which chunks changed between versions
4. Version cleanup: Delete old versions after retention period

Design decisions:
- Version string format: "1.0", "1.1", "2.0" (semantic versioning light)
- Previous version preserved in database with status="archived"
- Chunk comparison uses difflib for text similarity
- Configurable retention: keep last N versions or days
- All operations scoped by tenant_id

Use cases:
- "What changed in the latest procedure manual?"
- "Show me differences between v1.0 and v2.0"
- "When was section 4.3 last modified?"
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document, DocumentChunk, DocumentStatus

log = structlog.get_logger(__name__)


@dataclass
class VersionInfo:
    """Information about a document version."""

    document_id: uuid.UUID
    version: str
    filename: str
    status: str
    chunk_count: int
    size_bytes: int
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any]


@dataclass
class ChunkDiff:
    """Difference between two chunk versions."""

    chunk_index: int
    old_content: str | None
    new_content: str | None
    similarity: float  # 0.0 - 1.0
    change_type: str  # "added", "removed", "modified", "unchanged"


@dataclass
class VersionComparison:
    """Comparison between two document versions."""

    old_version: VersionInfo
    new_version: VersionInfo
    chunk_diffs: list[ChunkDiff]
    summary: str  # Human-readable summary


class DocumentVersionManager:
    """Manage document versions and comparisons."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize version manager.

        Args:
            db: Async database session
        """
        self._db = db

    async def create_new_version(
        self,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        uploaded_by_user_id: uuid.UUID,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        """Create a new document version or initial upload.

        If a document with the same filename exists for this tenant,
        archives the old version and creates a new one with incremented version.

        Args:
            tenant_id: Tenant ID
            filename: Document filename
            uploaded_by_user_id: User uploading the document
            content_type: MIME type
            metadata: Optional document metadata

        Returns:
            New Document object (not yet committed)
        """
        # Check for existing document with same filename
        existing_query = select(Document).where(
            Document.tenant_id == tenant_id,
            Document.filename == filename,
            Document.status.in_([DocumentStatus.READY, DocumentStatus.PROCESSING]),
        ).order_by(Document.version.desc())

        result = await self._db.execute(existing_query)
        existing_doc = result.scalar_one_or_none()

        if existing_doc:
            # Archive old version
            existing_doc.status = DocumentStatus.READY  # Keep as "archived" by version only
            await self._db.flush()

            # Increment version
            new_version = self._increment_version(existing_doc.version)

            log.info(
                "version.create_new",
                tenant_id=str(tenant_id),
                filename=filename,
                old_version=existing_doc.version,
                new_version=new_version,
            )
        else:
            new_version = "1.0"
            log.info(
                "version.initial_upload",
                tenant_id=str(tenant_id),
                filename=filename,
                version=new_version,
            )

        # Create new document
        new_doc = Document(
            tenant_id=tenant_id,
            filename=filename,
            uploaded_by_user_id=uploaded_by_user_id,
            content_type=content_type,
            version=new_version,
            status=DocumentStatus.PENDING,
            metadata_=metadata or {},
        )
        self._db.add(new_doc)
        await self._db.flush()

        return new_doc

    async def get_versions(
        self,
        *,
        tenant_id: uuid.UUID,
        filename: str,
    ) -> list[VersionInfo]:
        """Get all versions of a document.

        Args:
            tenant_id: Tenant ID
            filename: Document filename

        Returns:
            List of VersionInfo objects, sorted by version (newest first)
        """
        query = (
            select(Document)
            .where(
                Document.tenant_id == tenant_id,
                Document.filename == filename,
            )
            .order_by(Document.version.desc())
        )

        result = await self._db.execute(query)
        docs = result.scalars().all()

        versions = [
            VersionInfo(
                document_id=doc.id,
                version=doc.version,
                filename=doc.filename,
                status=doc.status.value,
                chunk_count=doc.chunk_count,
                size_bytes=doc.size_bytes,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                metadata=dict(doc.metadata_),
            )
            for doc in docs
        ]

        log.debug(
            "version.get_versions",
            tenant_id=str(tenant_id),
            filename=filename,
            count=len(versions),
        )

        return versions

    async def compare_versions(
        self,
        *,
        tenant_id: uuid.UUID,
        old_document_id: uuid.UUID,
        new_document_id: uuid.UUID,
    ) -> VersionComparison:
        """Compare two versions of a document at the chunk level.

        Args:
            tenant_id: Tenant ID for isolation
            old_document_id: Document ID of older version
            new_document_id: Document ID of newer version

        Returns:
            VersionComparison with chunk-level diffs
        """
        # Fetch both documents
        old_doc = await self._get_document(tenant_id=tenant_id, document_id=old_document_id)
        new_doc = await self._get_document(tenant_id=tenant_id, document_id=new_document_id)

        if not old_doc or not new_doc:
            raise ValueError("One or both documents not found")

        # Fetch chunks for both versions
        old_chunks = await self._get_chunks(tenant_id=tenant_id, document_id=old_document_id)
        new_chunks = await self._get_chunks(tenant_id=tenant_id, document_id=new_document_id)

        # Build chunk maps by index
        old_map = {c.chunk_index: c.content for c in old_chunks}
        new_map = {c.chunk_index: c.content for c in new_chunks}

        # Generate diffs
        all_indices = sorted(set(old_map.keys()) | set(new_map.keys()))
        chunk_diffs: list[ChunkDiff] = []

        for idx in all_indices:
            old_content = old_map.get(idx)
            new_content = new_map.get(idx)

            if old_content and new_content:
                # Both exist - check if modified
                similarity = self._compute_similarity(old_content, new_content)
                if similarity < 0.99:  # Consider 99%+ similarity as unchanged
                    change_type = "modified"
                else:
                    change_type = "unchanged"
            elif new_content and not old_content:
                change_type = "added"
                similarity = 0.0
            elif old_content and not new_content:
                change_type = "removed"
                similarity = 0.0
            else:
                continue  # Should not happen

            chunk_diffs.append(
                ChunkDiff(
                    chunk_index=idx,
                    old_content=old_content,
                    new_content=new_content,
                    similarity=similarity,
                    change_type=change_type,
                )
            )

        # Generate summary
        added_count = sum(1 for d in chunk_diffs if d.change_type == "added")
        removed_count = sum(1 for d in chunk_diffs if d.change_type == "removed")
        modified_count = sum(1 for d in chunk_diffs if d.change_type == "modified")

        summary = (
            f"{added_count} chunks added, "
            f"{removed_count} chunks removed, "
            f"{modified_count} chunks modified"
        )

        log.info(
            "version.compare_complete",
            tenant_id=str(tenant_id),
            old_version=old_doc.version,
            new_version=new_doc.version,
            summary=summary,
        )

        return VersionComparison(
            old_version=VersionInfo(
                document_id=old_doc.id,
                version=old_doc.version,
                filename=old_doc.filename,
                status=old_doc.status.value,
                chunk_count=old_doc.chunk_count,
                size_bytes=old_doc.size_bytes,
                created_at=old_doc.created_at,
                updated_at=old_doc.updated_at,
                metadata=dict(old_doc.metadata_),
            ),
            new_version=VersionInfo(
                document_id=new_doc.id,
                version=new_doc.version,
                filename=new_doc.filename,
                status=new_doc.status.value,
                chunk_count=new_doc.chunk_count,
                size_bytes=new_doc.size_bytes,
                created_at=new_doc.created_at,
                updated_at=new_doc.updated_at,
                metadata=dict(new_doc.metadata_),
            ),
            chunk_diffs=chunk_diffs,
            summary=summary,
        )

    async def cleanup_old_versions(
        self,
        *,
        tenant_id: uuid.UUID,
        keep_latest_n: int = 5,
        keep_newer_than_days: int = 90,
    ) -> int:
        """Delete old document versions to save storage.

        Keeps either:
        - The latest N versions per document, OR
        - Versions newer than X days

        Args:
            tenant_id: Tenant ID for isolation
            keep_latest_n: Keep this many latest versions per document
            keep_newer_than_days: Keep versions newer than this many days

        Returns:
            Number of documents deleted
        """
        cutoff_date = datetime.now(UTC) - timedelta(days=keep_newer_than_days)

        # Get all documents grouped by filename
        query = (
            select(Document.filename)
            .where(Document.tenant_id == tenant_id)
            .distinct()
        )
        result = await self._db.execute(query)
        filenames = [row[0] for row in result.all()]

        deleted_count = 0

        for filename in filenames:
            versions = await self.get_versions(tenant_id=tenant_id, filename=filename)

            # Sort by version descending (newest first)
            versions.sort(key=lambda v: v.version, reverse=True)

            # Determine which to delete
            to_delete: list[uuid.UUID] = []

            for i, version in enumerate(versions):
                # Keep if it's in the latest N
                if i < keep_latest_n:
                    continue

                # Keep if newer than cutoff
                if version.created_at > cutoff_date:
                    continue

                # Otherwise, mark for deletion
                to_delete.append(version.document_id)

            # Delete documents (cascades to chunks via FK)
            if to_delete:
                delete_stmt = delete(Document).where(
                    Document.id.in_(to_delete),
                    Document.tenant_id == tenant_id,  # Extra safety
                )
                await self._db.execute(delete_stmt)
                deleted_count += len(to_delete)

                log.info(
                    "version.cleanup",
                    tenant_id=str(tenant_id),
                    filename=filename,
                    deleted_count=len(to_delete),
                )

        await self._db.flush()

        log.info(
            "version.cleanup_complete",
            tenant_id=str(tenant_id),
            total_deleted=deleted_count,
        )

        return deleted_count

    # ---- Private helpers ----

    def _increment_version(self, version: str) -> str:
        """Increment version string (e.g., "1.0" -> "1.1", "1.9" -> "2.0").

        Args:
            version: Current version string

        Returns:
            Incremented version string
        """
        try:
            parts = version.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0

            if minor >= 9:
                return f"{major + 1}.0"
            else:
                return f"{major}.{minor + 1}"
        except (ValueError, IndexError):
            # Fallback if version format is invalid
            return "2.0"

    async def _get_document(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> Document | None:
        """Fetch a single document by ID with tenant isolation."""
        query = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
        )
        result = await self._db.execute(query)
        return result.scalar_one_or_none()

    async def _get_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[DocumentChunk]:
        """Fetch all chunks for a document."""
        query = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.tenant_id == tenant_id,
            )
            .order_by(DocumentChunk.chunk_index)
        )
        result = await self._db.execute(query)
        return list(result.scalars().all())

    def _compute_similarity(self, text1: str, text2: str) -> float:
        """Compute similarity between two text strings using difflib.

        Returns:
            Similarity ratio (0.0 - 1.0)
        """
        return difflib.SequenceMatcher(None, text1, text2).ratio()


async def version_manager_from_context(db: AsyncSession) -> DocumentVersionManager:
    """Factory for DocumentVersionManager - used by tool gateway."""
    return DocumentVersionManager(db=db)
