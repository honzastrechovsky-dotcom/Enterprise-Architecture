"""Add ingestion_jobs table for document processing pipeline.

Revision ID: 008
Revises: 007
Create Date: 2026-02-17

Adds:
- ingestion_jobs table - tracks document ingestion lifecycle
- file_type enum - PDF, DOCX, PPTX, XLSX, HTML, MD, TXT
- ingestion_status enum - PENDING/PROCESSING/CHUNKING/EMBEDDING/INDEXING/COMPLETED/FAILED
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create ingestion pipeline tables and enums."""

    # Create file_type enum
    op.execute(
        "CREATE TYPE file_type AS ENUM "
        "('pdf', 'docx', 'pptx', 'xlsx', 'html', 'md', 'txt')"
    )

    # Create ingestion_status enum
    op.execute(
        "CREATE TYPE ingestion_status AS ENUM "
        "('pending', 'processing', 'chunking', 'embedding', 'indexing', 'completed', 'failed')"
    )

    # Create ingestion_jobs table
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # File information
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column(
            "file_type",
            sa.Enum(
                "pdf",
                "docx",
                "pptx",
                "xlsx",
                "html",
                "md",
                "txt",
                name="file_type",
            ),
            nullable=False,
        ),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=False),
        # Job status
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "chunking",
                "embedding",
                "indexing",
                "completed",
                "failed",
                name="ingestion_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        # Extracted metadata
        sa.Column(
            "metadata_extracted",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
        # Processing results
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        # Timing
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Foreign keys
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
    )

    # Indexes for performance
    op.create_index(
        "ix_ingestion_jobs_tenant_id",
        "ingestion_jobs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_ingestion_jobs_status",
        "ingestion_jobs",
        ["status"],
    )
    op.create_index(
        "ix_ingestion_jobs_tenant_status",
        "ingestion_jobs",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_ingestion_jobs_tenant_created",
        "ingestion_jobs",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    """Drop ingestion pipeline tables and enums."""

    # Drop indexes
    op.drop_index("ix_ingestion_jobs_tenant_created", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_tenant_status", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_status", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_tenant_id", table_name="ingestion_jobs")

    # Drop table
    op.drop_table("ingestion_jobs")

    # Drop enums (must be done after table drop)
    op.execute("DROP TYPE ingestion_status")
    op.execute("DROP TYPE file_type")
