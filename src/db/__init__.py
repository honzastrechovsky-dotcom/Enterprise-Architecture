"""
src.db - Database package.

Provides:
  - create_engine_with_pool: production-grade AsyncEngine factory
  - Re-exports from src.database for backwards compatibility:
    Base, init_db, close_db, get_engine, get_db_session
"""

from src.database import Base, close_db, get_db_session, get_engine, init_db
from src.db.pool import (
    POOL_MAX_OVERFLOW,
    POOL_RECYCLE,
    POOL_SIZE,
    POOL_TIMEOUT,
    STATEMENT_CACHE_SIZE,
    create_engine_with_pool,
)

__all__ = [
    # Pool factory
    "create_engine_with_pool",
    # Pool constants
    "POOL_SIZE",
    "POOL_MAX_OVERFLOW",
    "POOL_TIMEOUT",
    "POOL_RECYCLE",
    "STATEMENT_CACHE_SIZE",
    # Session / engine management (from src.database)
    "Base",
    "init_db",
    "close_db",
    "get_engine",
    "get_db_session",
]
