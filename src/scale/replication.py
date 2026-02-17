"""Database replication with read/write splitting.

Provides load-balanced read replicas with automatic failover and lag detection.
All writes go to the primary. Reads are distributed across healthy replicas.

Architecture:
- PRIMARY → Write operations (always authoritative)
- REPLICA → Read operations (load-balanced, lag-aware)
- NEAREST → Intelligent routing based on latency

Replication lag handling:
- Check replication_delay_seconds on each replica
- Skip replicas behind by > threshold (default 5 seconds)
- Fall back to primary if all replicas lagged

Health checks:
- Periodic health checks on all replicas
- Mark unhealthy replicas unavailable
- Automatic retry after cooldown period
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = structlog.get_logger(__name__)


class ReadPreference(StrEnum):
    """Read preference for replica routing."""

    PRIMARY = "primary"  # Always read from primary
    REPLICA = "replica"  # Load-balanced across replicas (fallback to primary)
    NEAREST = "nearest"  # Route to lowest-latency replica


@dataclass
class ReplicationConfig:
    """Configuration for read replica routing.

    Attributes:
        primary_url: PostgreSQL connection URL for the primary database
        replica_urls: List of replica connection URLs (can be empty)
        read_preference: Routing strategy for read operations
        max_lag_seconds: Maximum acceptable replication lag (default 5s)
        health_check_interval: Seconds between health checks (default 30s)
        pool_size: Connection pool size per database (default 5)
        max_overflow: Max overflow connections per pool (default 10)
    """

    primary_url: str
    replica_urls: list[str] = field(default_factory=list)
    read_preference: ReadPreference = ReadPreference.REPLICA
    max_lag_seconds: float = 5.0
    health_check_interval: float = 30.0
    pool_size: int = 5
    max_overflow: int = 10


@dataclass
class ReplicaHealth:
    """Health status for a single replica."""

    url: str
    is_healthy: bool
    last_check: datetime
    replication_lag_seconds: float | None = None
    error_message: str | None = None


class ReplicatedSessionFactory:
    """Session factory with read/write splitting and replica failover.

    Manages separate connection pools for primary and each replica.
    Routes writes to primary, reads to healthy replicas.

    Usage:
        factory = ReplicatedSessionFactory(config)
        await factory.initialize()

        # Write operations (always primary)
        async with factory.get_write_session() as session:
            session.add(user)
            await session.commit()

        # Read operations (replica if available)
        async with factory.get_read_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

        await factory.close()
    """

    def __init__(self, config: ReplicationConfig) -> None:
        """Initialize the factory with configuration.

        Args:
            config: Replication configuration
        """
        self.config = config
        self._primary_engine: AsyncEngine | None = None
        self._replica_engines: dict[str, AsyncEngine] = {}
        self._replica_health: dict[str, ReplicaHealth] = {}
        self._health_check_task: asyncio.Task[None] | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize connection pools and start health checks.

        Must be called before using get_write_session() or get_read_session().
        """
        if self._initialized:
            log.warning("replication.already_initialized")
            return

        # Create primary engine
        self._primary_engine = create_async_engine(
            self.config.primary_url,
            pool_size=self.config.pool_size,
            max_overflow=self.config.max_overflow,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        log.info("replication.primary_connected", url=self._mask_password(self.config.primary_url))

        # Create replica engines
        for replica_url in self.config.replica_urls:
            engine = create_async_engine(
                replica_url,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_pre_ping=True,
                pool_recycle=300,
            )
            self._replica_engines[replica_url] = engine
            self._replica_health[replica_url] = ReplicaHealth(
                url=replica_url,
                is_healthy=False,
                last_check=datetime.now(UTC),
            )
            log.info("replication.replica_connected", url=self._mask_password(replica_url))

        # Run initial health check
        await self._check_all_replicas()

        # Start periodic health checks
        if self.config.replica_urls:
            self._health_check_task = asyncio.create_task(self._health_check_loop())

        self._initialized = True
        log.info(
            "replication.initialized",
            primary=self._mask_password(self.config.primary_url),
            replicas=len(self.config.replica_urls),
            healthy_replicas=sum(1 for h in self._replica_health.values() if h.is_healthy),
        )

    async def close(self) -> None:
        """Close all connection pools and stop health checks."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        if self._primary_engine:
            await self._primary_engine.dispose()
            log.info("replication.primary_closed")

        for url, engine in self._replica_engines.items():
            await engine.dispose()
            log.info("replication.replica_closed", url=self._mask_password(url))

        self._initialized = False

    async def get_write_session(self) -> AsyncSession:
        """Get a session for write operations (always uses primary).

        Returns:
            AsyncSession connected to primary database

        Raises:
            RuntimeError: If not initialized
        """
        if not self._initialized or self._primary_engine is None:
            raise RuntimeError("ReplicatedSessionFactory not initialized. Call initialize() first.")

        session_factory = async_sessionmaker(
            self._primary_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        return session_factory()

    async def get_read_session(self) -> AsyncSession:
        """Get a session for read operations (uses replica if available).

        Routes to a healthy replica based on read_preference. Falls back to
        primary if no healthy replicas are available.

        Returns:
            AsyncSession connected to replica or primary

        Raises:
            RuntimeError: If not initialized
        """
        if not self._initialized or self._primary_engine is None:
            raise RuntimeError("ReplicatedSessionFactory not initialized. Call initialize() first.")

        # Select target engine based on preference
        engine = await self._select_read_engine()

        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        return session_factory()

    async def health_check_replicas(self) -> dict[str, bool]:
        """Check health of all replicas.

        Returns:
            Dict mapping replica URL to health status (True = healthy)
        """
        await self._check_all_replicas()
        return {url: health.is_healthy for url, health in self._replica_health.items()}

    async def _select_read_engine(self) -> AsyncEngine:
        """Select an engine for read operations based on preference.

        Returns:
            AsyncEngine (replica if available, primary as fallback)
        """
        if self.config.read_preference == ReadPreference.PRIMARY:
            # Always use primary
            log.debug("replication.routing", target="primary", reason="preference")
            return self._primary_engine  # type: ignore[return-value]

        # Get healthy replicas with acceptable lag
        healthy_replicas = [
            (url, health)
            for url, health in self._replica_health.items()
            if health.is_healthy
            and (
                health.replication_lag_seconds is None
                or health.replication_lag_seconds <= self.config.max_lag_seconds
            )
        ]

        if not healthy_replicas:
            # No healthy replicas → fallback to primary
            log.debug("replication.routing", target="primary", reason="no_healthy_replicas")
            return self._primary_engine  # type: ignore[return-value]

        # NEAREST: select replica with lowest lag
        if self.config.read_preference == ReadPreference.NEAREST:
            url, _ = min(
                healthy_replicas,
                key=lambda x: x[1].replication_lag_seconds if x[1].replication_lag_seconds else 0,
            )
            log.debug("replication.routing", target="replica", url=self._mask_password(url), strategy="nearest")
            return self._replica_engines[url]

        # REPLICA: random load balancing
        url, _ = random.choice(healthy_replicas)
        log.debug("replication.routing", target="replica", url=self._mask_password(url), strategy="random")
        return self._replica_engines[url]

    async def _check_all_replicas(self) -> None:
        """Health check all replicas concurrently."""
        if not self._replica_engines:
            return

        tasks = [self._check_replica_health(url, engine) for url, engine in self._replica_engines.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_replica_health(self, url: str, engine: AsyncEngine) -> None:
        """Check health and replication lag for a single replica.

        Updates self._replica_health[url] with results.

        Args:
            url: Replica connection URL
            engine: SQLAlchemy async engine for the replica
        """
        try:
            async with engine.connect() as conn:
                # Check basic connectivity
                await conn.execute(text("SELECT 1"))

                # Check replication lag (PostgreSQL-specific)
                # pg_last_wal_receive_lsn() - pg_last_wal_replay_lsn() gives byte lag
                # Extract timestamp diff for lag in seconds
                result = await conn.execute(
                    text(
                        """
                        SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS lag_seconds
                        """
                    )
                )
                row = result.fetchone()
                lag_seconds = float(row[0]) if row and row[0] is not None else 0.0

                # Update health status
                self._replica_health[url] = ReplicaHealth(
                    url=url,
                    is_healthy=True,
                    last_check=datetime.now(UTC),
                    replication_lag_seconds=lag_seconds,
                )

                log.debug(
                    "replication.health_check",
                    url=self._mask_password(url),
                    healthy=True,
                    lag_seconds=lag_seconds,
                )

        except Exception as exc:
            self._replica_health[url] = ReplicaHealth(
                url=url,
                is_healthy=False,
                last_check=datetime.now(UTC),
                error_message=str(exc),
            )
            log.warning(
                "replication.health_check_failed",
                url=self._mask_password(url),
                error=str(exc),
            )

    async def _health_check_loop(self) -> None:
        """Periodic health check loop (runs in background task)."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._check_all_replicas()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("replication.health_check_loop_error", error=str(exc), exc_info=True)

    @staticmethod
    def _mask_password(url: str) -> str:
        """Mask password in connection URL for logging.

        Args:
            url: Database connection URL

        Returns:
            URL with password replaced by '***'
        """
        # postgresql://user:password@host/db → postgresql://user:***@host/db
        if "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        auth, hostdb = rest.split("@", 1)
        if ":" in auth:
            user, _ = auth.split(":", 1)
            return f"{scheme}://{user}:***@{hostdb}"
        return url
