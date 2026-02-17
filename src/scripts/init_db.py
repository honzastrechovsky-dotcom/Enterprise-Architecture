"""Database initialization script.

Run this once to:
1. Enable the pgvector extension in PostgreSQL
2. Create all tables from the ORM metadata

Usage:
    python -m src.scripts.init_db
    # or
    uv run python -m src.scripts.init_db
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


async def init_db() -> None:
    """Create all tables and enable pgvector."""
    # Import models to register them with Base.metadata
    import src.models  # noqa: F401 - registers all models
    from src.config import get_settings
    from src.database import Base, get_engine
    from src.database import init_db as _init_engine

    settings = get_settings()
    log.info("init_db.starting", db_url=settings.database_url.split("@")[-1])

    _init_engine(settings)
    engine = get_engine()

    async with engine.begin() as conn:
        # Enable pgvector extension
        await conn.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "CREATE EXTENSION IF NOT EXISTS vector"
            )
        )
        log.info("init_db.pgvector_enabled")

        # Create all tables
        await conn.run_sync(Base.metadata.create_all)
        log.info("init_db.tables_created")

    await engine.dispose()
    log.info("init_db.complete")


if __name__ == "__main__":
    asyncio.run(init_db())
