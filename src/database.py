"""
Database engine and session management (SQLAlchemy 2.0 async).

All database access goes through the async session returned by
get_db_session(). Never use synchronous sessions in this codebase.

Design decisions:
- Pool size tuned for a containerized API (not a massive machine)
- All models import Base from here to keep metadata centralized
- Session is committed/rolled back by the FastAPI dependency, not by
  individual service functions - this makes transaction boundaries explicit
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from src.config import Settings, get_settings

log = structlog.get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    Centralizing the metadata here ensures Alembic can discover all tables
    by importing this module.
    """

    # Type annotation map for SQLAlchemy 2.0 Mapped[] columns
    type_annotation_map: dict[Any, Any] = {}


def _build_engine(settings: Settings, *, for_test: bool = False) -> AsyncEngine:
    """Create an async SQLAlchemy engine from settings.

    Uses NullPool in test mode to avoid connection leaks between test cases.
    """
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo_sql,
        "future": True,
    }
    if for_test:
        # NullPool ensures each test gets a clean connection; no shared pool state
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            {
                "pool_size": 5,
                "max_overflow": 10,
                "pool_pre_ping": True,
                "pool_recycle": 300,  # Recycle connections every 5 minutes
            }
        )
    return create_async_engine(settings.database_url, **kwargs)


# Module-level singletons, initialized in lifespan
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(settings: Settings | None = None, *, for_test: bool = False) -> None:
    """Initialize the database engine and session factory.

    Called once during application startup (or test setup).
    """
    global _engine, _session_factory
    cfg = settings or get_settings()
    _engine = _build_engine(cfg, for_test=for_test)
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,  # Avoid lazy-load issues after commit
        autoflush=True,
        autocommit=False,
    )
    log.info("database.initialized", url=cfg.database_url.split("@")[-1])


async def close_db() -> None:
    """Dispose the engine and release all connections."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        log.info("database.closed")
        _engine = None


def get_engine() -> AsyncEngine:
    """Return the initialized engine (raises if not initialized)."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    Commits on success, rolls back on any exception.

    Usage:
        @router.get("/foo")
        async def endpoint(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
