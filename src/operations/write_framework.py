"""Write Operations Framework with HITL approval.

Provides the core infrastructure for write operations that require
Human-in-the-Loop approval before execution:

1. Propose operation → creates WriteOperation in PROPOSED state
2. Review → operator approves/rejects
3. Execute → only APPROVED operations can execute
4. Audit → full trail from proposal to completion

Risk-based approval requirements:
- LOW: Auto-approve
- MEDIUM: Require operator approval
- HIGH: Require operator approval
- CRITICAL: Require operator approval + MFA

All operations are tenant-isolated and fully audited.

execute() routes to real SAP/MES connectors via a ConnectorRegistry.
Connectors are instantiated with their configs and used as async context
managers for each execution. For persistent multi-instance deployments,
use PersistentWriteOperationExecutor instead of WriteOperationExecutor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from src.connectors.base import BaseConnector, ConnectorConfig, ConnectorResult
from src.connectors.mes import MESConnector
from src.connectors.sap import SAPConnector

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------

def build_connector_registry_from_settings(settings: Any) -> ConnectorRegistry:
    """Build a ConnectorRegistry populated with SAP and MES connectors.

    Reads connection details from *settings* (a ``src.config.Settings`` instance
    or any object with the same attributes) so that no credentials are
    hardcoded.

    The function is intentionally kept outside the class so it can be called
    from the FastAPI lifespan, from CLI scripts, and from tests — without
    importing the full application.

    Args:
        settings: Application settings object (``src.config.Settings``).

    Returns:
        ``ConnectorRegistry`` with ``"sap"`` and ``"mes"`` registered.
    """
    from src.connectors.base import AuthType, ConnectorConfig

    registry = ConnectorRegistry()

    # ------------------------------------------------------------------
    # SAP connector
    # ------------------------------------------------------------------
    try:
        sap_auth_type = AuthType(settings.sap_auth_type)
    except ValueError:
        log.warning(
            "connector_registry.unknown_sap_auth_type",
            auth_type=settings.sap_auth_type,
            fallback="none",
        )
        sap_auth_type = AuthType.NONE

    sap_auth_params: dict[str, Any] = {}
    if sap_auth_type == AuthType.BASIC:
        sap_auth_params = {
            "username": settings.sap_username,
            "password": settings.sap_password.get_secret_value(),
        }
    elif sap_auth_type == AuthType.API_KEY:
        sap_auth_params = {"api_key": settings.sap_api_key.get_secret_value()}

    sap_config = ConnectorConfig(
        name="sap",
        endpoint=settings.sap_endpoint,
        auth_type=sap_auth_type,
        timeout_seconds=settings.sap_timeout_seconds,
        auth_params=sap_auth_params,
    )
    registry.register("sap", SAPConnector, sap_config)

    # ------------------------------------------------------------------
    # MES connector
    # ------------------------------------------------------------------
    try:
        mes_auth_type = AuthType(settings.mes_auth_type)
    except ValueError:
        log.warning(
            "connector_registry.unknown_mes_auth_type",
            auth_type=settings.mes_auth_type,
            fallback="none",
        )
        mes_auth_type = AuthType.NONE

    mes_auth_params: dict[str, Any] = {}
    if mes_auth_type == AuthType.API_KEY:
        mes_auth_params = {"api_key": settings.mes_api_key.get_secret_value()}
    elif mes_auth_type == AuthType.BEARER:
        mes_auth_params = {"token": settings.mes_api_key.get_secret_value()}

    mes_config = ConnectorConfig(
        name="mes",
        endpoint=settings.mes_endpoint,
        auth_type=mes_auth_type,
        timeout_seconds=settings.mes_timeout_seconds,
        auth_params=mes_auth_params,
    )
    registry.register("mes", MESConnector, mes_config)

    log.info(
        "connector_registry.built",
        connectors=registry.known_connectors(),
    )
    return registry


class ConnectorRegistry:
    """Registry that holds per-connector configurations and creates instances.

    Connectors are identified by name string (e.g. ``"sap"``, ``"mes"``).
    The registry stores ``ConnectorConfig`` objects; actual ``BaseConnector``
    instances are created on-demand so each execution gets a fresh connection.

    Usage::

        registry = ConnectorRegistry()
        registry.register("sap", SAPConnector, sap_config)
        registry.register("mes", MESConnector, mes_config)
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[type[BaseConnector], ConnectorConfig]] = {}

    def register(
        self,
        name: str,
        connector_class: type[BaseConnector],
        config: ConnectorConfig,
    ) -> None:
        """Register a connector class + config under *name*."""
        self._entries[name] = (connector_class, config)

    def create(self, name: str) -> BaseConnector:
        """Instantiate and return a connector for *name*.

        Raises:
            KeyError: If no connector is registered under *name*.
        """
        if name not in self._entries:
            raise KeyError(f"No connector registered for '{name}'")
        cls, config = self._entries[name]
        return cls(config)

    def known_connectors(self) -> list[str]:
        """Return all registered connector names."""
        return list(self._entries.keys())


async def route_to_connector(
    operation: WriteOperation,
    registry: ConnectorRegistry | None,
) -> ConnectorResult:
    """Route a write operation to the appropriate connector.

    Standalone helper extracted from the executor classes so both
    ``WriteOperationExecutor`` and ``PersistentWriteOperationExecutor``
    share the same implementation.

    Args:
        operation: Approved ``WriteOperation`` ready to execute.
        registry: Optional ``ConnectorRegistry`` for connector lookup.

    Returns:
        ``ConnectorResult`` from the connector, or an error result.
    """
    if registry is None:
        log.error(
            "operation.no_registry",
            operation_id=operation.id,
            connector=operation.connector,
        )
        return ConnectorResult(
            success=False,
            error=(
                f"No connector registry configured; cannot execute "
                f"'{operation.connector}' operation"
            ),
            metadata={"operation_id": operation.id},
        )

    try:
        connector = registry.create(operation.connector)
    except KeyError:
        known = registry.known_connectors()
        log.error(
            "operation.unknown_connector",
            operation_id=operation.id,
            connector=operation.connector,
            known_connectors=known,
        )
        return ConnectorResult(
            success=False,
            error=(
                f"Unknown connector '{operation.connector}'. "
                f"Registered connectors: {known}"
            ),
            metadata={"operation_id": operation.id},
        )

    try:
        async with connector:
            result = await connector.execute(
                operation=operation.operation_type,
                tenant_id=operation.tenant_id,
                user_id=operation.user_id,
                params=operation.params,
            )
        return result

    except Exception as exc:
        log.error(
            "operation.connector_exception",
            operation_id=operation.id,
            connector=operation.connector,
            operation_type=operation.operation_type,
            error=str(exc),
            exc_info=True,
        )
        return ConnectorResult(
            success=False,
            error=str(exc),
            metadata={
                "operation_id": operation.id,
                "connector": operation.connector,
                "operation_type": operation.operation_type,
            },
        )


class OperationStatus(StrEnum):
    """Status of a write operation."""

    PROPOSED = "proposed"  # Initial state after creation
    PENDING_APPROVAL = "pending_approval"  # Waiting for operator approval
    APPROVED = "approved"  # Approved, ready to execute
    REJECTED = "rejected"  # Rejected by operator
    EXECUTING = "executing"  # Currently executing
    COMPLETED = "completed"  # Successfully completed
    FAILED = "failed"  # Execution failed


class RiskLevel(StrEnum):
    """Risk level for write operations."""

    LOW = "low"  # Auto-approve, minimal impact
    MEDIUM = "medium"  # Require approval, moderate impact
    HIGH = "high"  # Require approval, significant impact
    CRITICAL = "critical"  # Require approval + MFA, critical impact


@dataclass
class WriteOperation:
    """A write operation requiring approval.

    Tracks the complete lifecycle from proposal through execution:
    - Proposed by a user
    - Approved/rejected by an operator
    - Executed against external system
    - Result captured in audit trail

    All operations are tenant-scoped for isolation.
    """

    # Identity
    tenant_id: uuid.UUID
    user_id: uuid.UUID  # Who proposed the operation

    # Operation details
    connector: str  # Which connector (sap, mes, etc.)
    operation_type: str  # Specific operation (create_purchase_request, etc.)
    params: dict[str, Any]  # Operation parameters
    description: str  # Human-readable description

    # Risk and approval
    risk_level: RiskLevel
    requires_approval: bool = True
    requires_mfa: bool = False

    # State
    id: str | None = None  # Assigned when proposed
    status: OperationStatus = OperationStatus.PROPOSED
    proposed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None
    executed_at: datetime | None = None
    approved_by: uuid.UUID | None = None  # Operator who approved/rejected

    # Audit
    audit_trail: list[dict[str, Any]] = field(default_factory=list)
    execution_result: ConnectorResult | None = None

    def __post_init__(self) -> None:
        """Set approval requirements based on risk level."""
        if self.risk_level == RiskLevel.LOW:
            self.requires_approval = False
            self.requires_mfa = False
        elif self.risk_level == RiskLevel.MEDIUM or self.risk_level == RiskLevel.HIGH:
            self.requires_approval = True
            self.requires_mfa = False
        elif self.risk_level == RiskLevel.CRITICAL:
            self.requires_approval = True
            self.requires_mfa = True

    def add_audit_entry(self, event: str, actor: uuid.UUID, **kwargs: Any) -> None:
        """Add an entry to the audit trail."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "actor": str(actor),
            **kwargs,
        }
        self.audit_trail.append(entry)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "connector": self.connector,
            "operation_type": self.operation_type,
            "params": self.params,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "requires_approval": self.requires_approval,
            "requires_mfa": self.requires_mfa,
            "status": self.status.value,
            "proposed_at": self.proposed_at.isoformat(),
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "approved_by": str(self.approved_by) if self.approved_by else None,
            "audit_trail": self.audit_trail,
        }


class WriteOperationExecutor:
    """Executor for write operations with HITL approval workflow.

    Manages the complete lifecycle of write operations:
    1. propose() - Create operation, auto-approve LOW risk
    2. approve() / reject() - Operator review
    3. execute() - Execute approved operations via real connectors
    4. get_pending() - List operations awaiting approval
    5. get_history() - Audit history

    Uses in-memory storage (non-persistent). For multi-instance or restart-safe
    deployments, use PersistentWriteOperationExecutor which stores state in PostgreSQL.
    """

    def __init__(self, connector_registry: ConnectorRegistry | None = None) -> None:
        """Initialize executor with in-memory storage.

        Args:
            connector_registry: Optional registry of real connectors.  When
                ``None`` (default), execution falls back to a structured error
                result rather than silently pretending to succeed.
        """
        # In-memory storage: operation_id -> WriteOperation
        self._operations: dict[str, WriteOperation] = {}
        self._registry = connector_registry

    async def propose(self, operation: WriteOperation) -> WriteOperation:
        """Propose a write operation.

        Auto-approves LOW risk operations.
        Others go to PENDING_APPROVAL state.

        Args:
            operation: WriteOperation to propose

        Returns:
            WriteOperation with ID assigned and status set
        """
        # Assign ID
        operation.id = str(uuid.uuid4())

        # Add audit entry for proposal
        operation.add_audit_entry("proposed", operation.user_id)

        # Auto-approve LOW risk operations
        if operation.risk_level == RiskLevel.LOW:
            operation.status = OperationStatus.APPROVED
            operation.approved_at = datetime.now(UTC)
            operation.approved_by = operation.user_id  # Self-approved
            operation.add_audit_entry("auto_approved", operation.user_id)

            log.info(
                "operation.auto_approved",
                operation_id=operation.id,
                connector=operation.connector,
                operation_type=operation.operation_type,
                tenant_id=str(operation.tenant_id),
            )
        else:
            operation.status = OperationStatus.PENDING_APPROVAL

            log.info(
                "operation.proposed",
                operation_id=operation.id,
                connector=operation.connector,
                operation_type=operation.operation_type,
                risk_level=operation.risk_level,
                requires_mfa=operation.requires_mfa,
                tenant_id=str(operation.tenant_id),
            )

        # Store operation
        self._operations[operation.id] = operation

        return operation

    async def approve(
        self,
        operation_id: str,
        approver_user_id: uuid.UUID,
        mfa_verified: bool,
    ) -> WriteOperation:
        """Approve a pending operation.

        Args:
            operation_id: Operation ID to approve
            approver_user_id: User ID of approver
            mfa_verified: Whether MFA was verified

        Returns:
            Approved WriteOperation

        Raises:
            ValueError: If operation not found, not pending, or MFA required but not verified
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        if operation.status != OperationStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Operation {operation_id} is not pending approval (status: {operation.status})"
            )

        # Check MFA requirement
        if operation.requires_mfa and not mfa_verified:
            raise ValueError(
                f"Operation {operation_id} requires MFA verification"
            )

        # Approve
        operation.status = OperationStatus.APPROVED
        operation.approved_by = approver_user_id
        operation.approved_at = datetime.now(UTC)
        operation.add_audit_entry("approved", approver_user_id, mfa_verified=mfa_verified)

        log.info(
            "operation.approved",
            operation_id=operation_id,
            approver=str(approver_user_id),
            mfa_verified=mfa_verified,
            tenant_id=str(operation.tenant_id),
        )

        return operation

    async def reject(
        self,
        operation_id: str,
        rejector_user_id: uuid.UUID,
        reason: str,
    ) -> WriteOperation:
        """Reject a pending operation.

        Args:
            operation_id: Operation ID to reject
            rejector_user_id: User ID of rejector
            reason: Reason for rejection

        Returns:
            Rejected WriteOperation

        Raises:
            ValueError: If operation not found or not pending
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        if operation.status != OperationStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Operation {operation_id} is not pending approval (status: {operation.status})"
            )

        # Reject
        operation.status = OperationStatus.REJECTED
        operation.approved_by = rejector_user_id  # Track who reviewed it
        operation.add_audit_entry("rejected", rejector_user_id, reason=reason)

        log.info(
            "operation.rejected",
            operation_id=operation_id,
            rejector=str(rejector_user_id),
            reason=reason,
            tenant_id=str(operation.tenant_id),
        )

        return operation

    async def execute(self, operation_id: str) -> ConnectorResult:
        """Execute an approved operation.

        Args:
            operation_id: Operation ID to execute

        Returns:
            ConnectorResult from execution

        Raises:
            ValueError: If operation not found or not approved
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        if operation.status != OperationStatus.APPROVED:
            raise ValueError(
                f"Only APPROVED operations can be executed (current status: {operation.status})"
            )

        # Mark as executing
        operation.status = OperationStatus.EXECUTING
        operation.executed_at = datetime.now(UTC)
        operation.add_audit_entry("execution_started", operation.user_id)

        log.info(
            "operation.executing",
            operation_id=operation_id,
            connector=operation.connector,
            operation_type=operation.operation_type,
            tenant_id=str(operation.tenant_id),
        )

        # Route to the real connector via registry
        result = await self._route_to_connector(operation)

        # Update operation with result
        operation.execution_result = result
        if result.success:
            operation.status = OperationStatus.COMPLETED
            operation.add_audit_entry("execution_completed", operation.user_id)
            log.info("operation.completed", operation_id=operation_id)
        else:
            # Execution failed — record failure and attempt rollback audit entry.
            # Actual data-level rollback is connector-specific; we surface the
            # error so callers can handle compensating transactions if needed.
            operation.status = OperationStatus.FAILED
            operation.add_audit_entry(
                "execution_failed", operation.user_id, error=result.error
            )
            log.error(
                "operation.failed",
                operation_id=operation_id,
                error=result.error,
            )

        return result

    async def _route_to_connector(self, operation: WriteOperation) -> ConnectorResult:
        """Route operation to connector. Delegates to standalone ``route_to_connector``."""
        return await route_to_connector(operation, self._registry)

    async def get_operation(self, operation_id: str) -> WriteOperation:
        """Get an operation by ID.

        Args:
            operation_id: Operation ID

        Returns:
            WriteOperation

        Raises:
            ValueError: If operation not found
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")
        return operation

    async def get_pending(self, tenant_id: uuid.UUID) -> list[WriteOperation]:
        """Get all pending operations for a tenant.

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of pending WriteOperations, ordered by proposed_at
        """
        pending = [
            op
            for op in self._operations.values()
            if op.tenant_id == tenant_id
            and op.status == OperationStatus.PENDING_APPROVAL
        ]
        return sorted(pending, key=lambda op: op.proposed_at)

    async def get_history(
        self, tenant_id: uuid.UUID, limit: int = 100
    ) -> list[WriteOperation]:
        """Get operation history for a tenant.

        Args:
            tenant_id: Tenant UUID
            limit: Maximum number of operations to return

        Returns:
            List of WriteOperations, ordered by proposed_at DESC (most recent first)
        """
        operations = [
            op for op in self._operations.values() if op.tenant_id == tenant_id
        ]
        # Sort by proposed_at DESC
        operations.sort(key=lambda op: op.proposed_at, reverse=True)
        return operations[:limit]

    def get_pending_operations(self) -> list[WriteOperation]:
        """Return all operations currently in PENDING_APPROVAL state (cross-tenant).

        Used by EscalationService to check timeouts without accessing private state.

        Returns:
            List of all pending WriteOperations across all tenants.
        """
        return [
            op
            for op in self._operations.values()
            if op.status == OperationStatus.PENDING_APPROVAL
        ]


# ---------------------------------------------------------------------------
# PostgreSQL-backed persistent executor
# ---------------------------------------------------------------------------

from sqlalchemy import select  # noqa: E402 — local import to avoid circular deps
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.core.policy import apply_tenant_filter  # noqa: E402
from src.models.write_operation import WriteOperationRecord  # noqa: E402


def _record_to_operation(record: WriteOperationRecord) -> WriteOperation:
    """Convert a WriteOperationRecord ORM row to a WriteOperation dataclass.

    The WriteOperation dataclass is the public API used by callers; the
    WriteOperationRecord is the DB representation.  We convert on every read
    so that callers do not need to know about the ORM layer.
    """
    op = WriteOperation(
        tenant_id=record.tenant_id,
        user_id=record.requested_by,
        connector=record.connector,
        operation_type=record.operation_type,
        params=record.parameters,
        description=record.description,
        risk_level=RiskLevel(record.risk_level),
    )
    op.id = str(record.id)
    op.status = OperationStatus(record.status)
    op.proposed_at = record.proposed_at
    op.approved_at = record.approved_at
    op.executed_at = record.executed_at
    op.approved_by = record.approved_by
    op.audit_trail = list(record.audit_trail) if record.audit_trail else []
    # requires_approval / requires_mfa are recomputed by __post_init__ from
    # risk_level, but we override with the persisted values so they stay
    # consistent even if policy changes between restarts.
    op.requires_approval = record.requires_approval
    op.requires_mfa = record.requires_mfa
    return op


def _operation_to_record(operation: WriteOperation) -> WriteOperationRecord:
    """Convert a WriteOperation dataclass to a new WriteOperationRecord row.

    Only used for initial INSERT (propose).  Subsequent mutations are
    applied directly to the fetched record via _apply_operation_to_record().
    """
    assert operation.id is not None, "operation.id must be set before persisting"
    return WriteOperationRecord(
        id=uuid.UUID(operation.id),
        tenant_id=operation.tenant_id,
        requested_by=operation.user_id,
        connector=operation.connector,
        operation_type=operation.operation_type,
        description=operation.description,
        parameters=operation.params,
        risk_level=operation.risk_level.value,
        requires_approval=operation.requires_approval,
        requires_mfa=operation.requires_mfa,
        status=operation.status.value,
        proposed_at=operation.proposed_at,
        approved_at=operation.approved_at,
        executed_at=operation.executed_at,
        created_at=operation.proposed_at,
        updated_at=datetime.now(UTC),
        approved_by=operation.approved_by,
        result_json=None,
        audit_trail=list(operation.audit_trail),
    )


def _apply_operation_to_record(
    operation: WriteOperation, record: WriteOperationRecord
) -> None:
    """Sync mutable fields from a WriteOperation back onto an existing record.

    Called after approve/reject/execute to persist state transitions.
    """
    record.status = operation.status.value
    record.approved_at = operation.approved_at
    record.executed_at = operation.executed_at
    record.approved_by = operation.approved_by
    record.audit_trail = list(operation.audit_trail)
    record.updated_at = datetime.now(UTC)


class PersistentWriteOperationExecutor:
    """PostgreSQL-backed write operation executor.

    Drop-in replacement for WriteOperationExecutor that persists every
    operation to the ``write_operations`` table so that:

    - Operations survive process restarts.
    - Multiple instances (e.g. multiple API pods) share the same operation
      state without conflict.

    Public API is identical to WriteOperationExecutor.  The only difference
    is that every method requires an ``AsyncSession`` to be passed in — this
    keeps the class stateless and compatible with FastAPI's Depends() pattern.

    Usage::

        executor = PersistentWriteOperationExecutor(registry)

        # In a FastAPI route:
        async def approve_op(
            operation_id: str,
            db: AsyncSession = Depends(get_db_session),
            current_user: User = Depends(get_current_user),
        ) -> ...:
            operation = await executor.approve(
                operation_id=operation_id,
                approver_user_id=current_user.id,
                mfa_verified=True,
                db=db,
            )
    """

    def __init__(self, connector_registry: ConnectorRegistry | None = None) -> None:
        self._registry = connector_registry

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_record(
        self, operation_id: str, db: AsyncSession
    ) -> WriteOperationRecord:
        """Fetch a WriteOperationRecord by ID.  Raises ValueError if missing."""
        try:
            op_uuid = uuid.UUID(operation_id)
        except ValueError:
            raise ValueError(f"Operation {operation_id} is not a valid UUID") from None

        result = await db.get(WriteOperationRecord, op_uuid)
        if result is None:
            raise ValueError(f"Operation {operation_id} not found")
        return result

    # ------------------------------------------------------------------
    # Public API (mirrors WriteOperationExecutor)
    # ------------------------------------------------------------------

    async def propose(
        self, operation: WriteOperation, db: AsyncSession
    ) -> WriteOperation:
        """Propose a write operation and persist it to the database.

        Auto-approves LOW risk operations.
        Others go to PENDING_APPROVAL state.

        Args:
            operation: WriteOperation to propose (id must be None on entry)
            db: Active async database session

        Returns:
            WriteOperation with id assigned and status set
        """
        operation.id = str(uuid.uuid4())
        operation.add_audit_entry("proposed", operation.user_id)

        if operation.risk_level == RiskLevel.LOW:
            operation.status = OperationStatus.APPROVED
            operation.approved_at = datetime.now(UTC)
            operation.approved_by = operation.user_id
            operation.add_audit_entry("auto_approved", operation.user_id)
            log.info(
                "operation.auto_approved",
                operation_id=operation.id,
                connector=operation.connector,
                operation_type=operation.operation_type,
                tenant_id=str(operation.tenant_id),
            )
        else:
            operation.status = OperationStatus.PENDING_APPROVAL
            log.info(
                "operation.proposed",
                operation_id=operation.id,
                connector=operation.connector,
                operation_type=operation.operation_type,
                risk_level=operation.risk_level,
                requires_mfa=operation.requires_mfa,
                tenant_id=str(operation.tenant_id),
            )

        record = _operation_to_record(operation)
        db.add(record)
        await db.flush()  # Assign DB-side defaults; caller commits

        return operation

    async def approve(
        self,
        operation_id: str,
        approver_user_id: uuid.UUID,
        mfa_verified: bool,
        db: AsyncSession,
    ) -> WriteOperation:
        """Approve a pending operation.

        Args:
            operation_id: Operation ID to approve
            approver_user_id: User ID of approver
            mfa_verified: Whether MFA was verified
            db: Active async database session

        Returns:
            Approved WriteOperation

        Raises:
            ValueError: If operation not found, not pending, or MFA required
                        but not verified
        """
        record = await self._get_record(operation_id, db)
        operation = _record_to_operation(record)

        if operation.status != OperationStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Operation {operation_id} is not pending approval"
                f" (status: {operation.status})"
            )

        if operation.requires_mfa and not mfa_verified:
            raise ValueError(f"Operation {operation_id} requires MFA verification")

        operation.status = OperationStatus.APPROVED
        operation.approved_by = approver_user_id
        operation.approved_at = datetime.now(UTC)
        operation.add_audit_entry("approved", approver_user_id, mfa_verified=mfa_verified)

        _apply_operation_to_record(operation, record)
        await db.flush()

        log.info(
            "operation.approved",
            operation_id=operation_id,
            approver=str(approver_user_id),
            mfa_verified=mfa_verified,
            tenant_id=str(operation.tenant_id),
        )

        return operation

    async def reject(
        self,
        operation_id: str,
        rejector_user_id: uuid.UUID,
        reason: str,
        db: AsyncSession,
    ) -> WriteOperation:
        """Reject a pending operation.

        Args:
            operation_id: Operation ID to reject
            rejector_user_id: User ID of rejector
            reason: Reason for rejection
            db: Active async database session

        Returns:
            Rejected WriteOperation

        Raises:
            ValueError: If operation not found or not pending
        """
        record = await self._get_record(operation_id, db)
        operation = _record_to_operation(record)

        if operation.status != OperationStatus.PENDING_APPROVAL:
            raise ValueError(
                f"Operation {operation_id} is not pending approval"
                f" (status: {operation.status})"
            )

        operation.status = OperationStatus.REJECTED
        operation.approved_by = rejector_user_id
        operation.add_audit_entry("rejected", rejector_user_id, reason=reason)

        _apply_operation_to_record(operation, record)
        await db.flush()

        log.info(
            "operation.rejected",
            operation_id=operation_id,
            rejector=str(rejector_user_id),
            reason=reason,
            tenant_id=str(operation.tenant_id),
        )

        return operation

    async def execute(self, operation_id: str, db: AsyncSession) -> ConnectorResult:
        """Execute an approved operation.

        Args:
            operation_id: Operation ID to execute
            db: Active async database session

        Returns:
            ConnectorResult from execution

        Raises:
            ValueError: If operation not found or not approved
        """
        record = await self._get_record(operation_id, db)
        operation = _record_to_operation(record)

        if operation.status != OperationStatus.APPROVED:
            raise ValueError(
                f"Only APPROVED operations can be executed"
                f" (current status: {operation.status})"
            )

        operation.status = OperationStatus.EXECUTING
        operation.executed_at = datetime.now(UTC)
        operation.add_audit_entry("execution_started", operation.user_id)

        _apply_operation_to_record(operation, record)
        await db.flush()

        log.info(
            "operation.executing",
            operation_id=operation_id,
            connector=operation.connector,
            operation_type=operation.operation_type,
            tenant_id=str(operation.tenant_id),
        )

        # Delegate to the same connector routing used by the in-memory executor
        result = await self._route_to_connector(operation)

        # Persist result and final status
        if result.success:
            operation.status = OperationStatus.COMPLETED
            operation.add_audit_entry("execution_completed", operation.user_id)
            log.info("operation.completed", operation_id=operation_id)
        else:
            operation.status = OperationStatus.FAILED
            operation.add_audit_entry(
                "execution_failed", operation.user_id, error=result.error
            )
            log.error("operation.failed", operation_id=operation_id, error=result.error)

        _apply_operation_to_record(operation, record)
        record.result_json = {
            "success": result.success,
            "data": result.data if hasattr(result, "data") else None,
            "error": result.error,
            "metadata": result.metadata if hasattr(result, "metadata") else {},
        }
        await db.flush()

        return result

    async def get_operation(
        self, operation_id: str, db: AsyncSession
    ) -> WriteOperation:
        """Get an operation by ID.

        Args:
            operation_id: Operation ID
            db: Active async database session

        Returns:
            WriteOperation

        Raises:
            ValueError: If operation not found
        """
        record = await self._get_record(operation_id, db)
        return _record_to_operation(record)

    async def get_pending(
        self, tenant_id: uuid.UUID, db: AsyncSession
    ) -> list[WriteOperation]:
        """Get all pending operations for a tenant.

        Args:
            tenant_id: Tenant UUID
            db: Active async database session

        Returns:
            List of pending WriteOperations ordered by proposed_at ASC
        """
        stmt = apply_tenant_filter(
            select(WriteOperationRecord).where(
                WriteOperationRecord.status == OperationStatus.PENDING_APPROVAL.value
            ).order_by(WriteOperationRecord.proposed_at),
            WriteOperationRecord,
            tenant_id,
        )
        result = await db.execute(stmt)
        records = result.scalars().all()
        return [_record_to_operation(r) for r in records]

    def get_pending_operations(self) -> list[WriteOperation]:
        """Not supported for the DB-backed executor.

        Use ``get_pending(tenant_id, db)`` instead, which accepts an
        ``AsyncSession`` and queries the ``write_operations`` table directly.

        Raises:
            NotImplementedError: Always -- DB-backed executors require an async
                session and cannot support synchronous cross-tenant enumeration.
        """
        raise NotImplementedError(
            "PersistentWriteOperationExecutor does not support get_pending_operations(). "
            "Use get_pending(tenant_id, db) with an AsyncSession instead."
        )

    async def get_history(
        self, tenant_id: uuid.UUID, db: AsyncSession, limit: int = 100
    ) -> list[WriteOperation]:
        """Get operation history for a tenant.

        Args:
            tenant_id: Tenant UUID
            db: Active async database session
            limit: Maximum number of operations to return

        Returns:
            List of WriteOperations ordered by proposed_at DESC (most recent first)
        """
        stmt = apply_tenant_filter(
            select(WriteOperationRecord)
            .order_by(WriteOperationRecord.proposed_at.desc())
            .limit(limit),
            WriteOperationRecord,
            tenant_id,
        )
        result = await db.execute(stmt)
        records = result.scalars().all()
        return [_record_to_operation(r) for r in records]

    # ------------------------------------------------------------------
    # Connector routing (reuses WriteOperationExecutor logic)
    # ------------------------------------------------------------------

    async def _route_to_connector(self, operation: WriteOperation) -> ConnectorResult:
        """Route operation to connector. Delegates to standalone ``route_to_connector``."""
        return await route_to_connector(operation, self._registry)
