"""
Async SQLAlchemy engine factory with production-grade connection pool configuration.

Pool sizing rationale:
  - POOL_SIZE=10       baseline concurrent DB connections per process
  - POOL_MAX_OVERFLOW=20  burst headroom; total max = POOL_SIZE + overflow = 30
  - POOL_TIMEOUT=30    seconds to wait for a connection before raising
  - POOL_RECYCLE=3600  recycle connections hourly to survive PgBouncer/firewall timeouts
  - pool_pre_ping=True detect dead connections before checkout (adds ~1 ms round-trip)

Statement cache:
  asyncpg caches prepared statements per connection.  STATEMENT_CACHE_SIZE controls
  the per-connection LRU cache.  Set to 0 to disable (e.g., if you use PgBouncer in
  transaction-pooling mode which does not support prepared statements).

Pool event logging:
  SQLAlchemy pool events are emitted via structlog so they appear in the same
  structured log stream as the rest of the application.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from src.config import Settings

log = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Pool defaults (override via environment variables)
# ------------------------------------------------------------------ #
POOL_SIZE: int = 10
POOL_MAX_OVERFLOW: int = 20
POOL_TIMEOUT: int = 30   # seconds
POOL_RECYCLE: int = 3600  # seconds (1 hour)
STATEMENT_CACHE_SIZE: int = 100  # per-connection prepared-statement LRU cache


def _attach_pool_listeners(engine: AsyncEngine) -> None:
    """Register pool event listeners for structured logging."""

    # SQLAlchemy pool events fire on the *sync* underlying pool.
    sync_pool = engine.pool

    @event.listens_for(sync_pool, "checkout")
    def on_checkout(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
        log.debug(
            "db.pool.checkout",
            pool_size=sync_pool.size(),
            checked_out=sync_pool.checkedout(),
        )

    @event.listens_for(sync_pool, "checkin")
    def on_checkin(dbapi_connection: Any, connection_record: Any) -> None:
        log.debug(
            "db.pool.checkin",
            pool_size=sync_pool.size(),
            checked_out=sync_pool.checkedout(),
        )

    @event.listens_for(sync_pool, "overflow")
    def on_overflow(dbapi_connection: Any, connection_record: Any) -> None:
        log.warning(
            "db.pool.overflow",
            pool_size=sync_pool.size(),
            checked_out=sync_pool.checkedout(),
            overflow=sync_pool.overflow(),
        )

    @event.listens_for(sync_pool, "connect")
    def on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        log.info(
            "db.pool.new_connection",
            pool_size=sync_pool.size(),
        )


def create_engine_with_pool(
    settings: Settings,
    *,
    for_test: bool = False,
    pool_size: int = POOL_SIZE,
    max_overflow: int = POOL_MAX_OVERFLOW,
    pool_timeout: int = POOL_TIMEOUT,
    pool_recycle: int = POOL_RECYCLE,
    statement_cache_size: int = STATEMENT_CACHE_SIZE,
) -> AsyncEngine:
    """Create an AsyncEngine with production-grade pool configuration.

    Args:
        settings:             Application settings (source of truth for DATABASE_URL).
        for_test:             Use NullPool so each test gets a clean connection.
        pool_size:            Number of permanent connections in the pool.
        max_overflow:         Extra connections allowed beyond pool_size.
        pool_timeout:         Seconds to wait for a free connection.
        pool_recycle:         Seconds after which a connection is recycled.
        statement_cache_size: asyncpg per-connection prepared-statement LRU size.
                              Set to 0 when using PgBouncer in transaction mode.

    Returns:
        A configured AsyncEngine instance with pool events attached.
    """
    # Allow settings to override pool parameters via env vars
    effective_pool_size = int(getattr(settings, "pool_size", pool_size))
    effective_max_overflow = int(getattr(settings, "pool_max_overflow", max_overflow))
    effective_pool_timeout = int(getattr(settings, "pool_timeout", pool_timeout))
    effective_pool_recycle = int(getattr(settings, "pool_recycle", pool_recycle))

    if for_test:
        engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo_sql,
            future=True,
            poolclass=NullPool,
        )
        log.info("db.engine.created", mode="test", poolclass="NullPool")
        return engine

    # asyncpg connect_args for statement cache
    connect_args: dict[str, Any] = {
        "statement_cache_size": statement_cache_size,
        "server_settings": {
            # Sets application_name visible in pg_stat_activity
            "application_name": "enterprise-agent-platform",
        },
    }

    engine = create_async_engine(
        settings.database_url,
        echo=settings.db_echo_sql,
        future=True,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=effective_pool_size,
        max_overflow=effective_max_overflow,
        pool_timeout=effective_pool_timeout,
        pool_recycle=effective_pool_recycle,
        pool_pre_ping=True,   # sends "SELECT 1" before each checkout
        connect_args=connect_args,
    )

    _attach_pool_listeners(engine)

    log.info(
        "db.engine.created",
        mode="production",
        pool_size=effective_pool_size,
        max_overflow=effective_max_overflow,
        pool_timeout=effective_pool_timeout,
        pool_recycle=effective_pool_recycle,
        statement_cache_size=statement_cache_size,
        pre_ping=True,
    )
    return engine
