"""API routes for write operations with HITL approval.

Endpoints:
- POST /operations/propose - Propose a write operation
- GET /operations/pending - List pending approvals
- POST /operations/{id}/approve - Approve an operation
- POST /operations/{id}/reject - Reject an operation
- GET /operations/{id} - Get operation details
- GET /operations/history - Get operation history

All endpoints enforce tenant isolation and RBAC.
"""

from __future__ import annotations

from typing import Any

import pyotp
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.config import Settings, get_settings
from src.connectors.base import ConnectorResult
from src.core.policy import require_role
from src.database import get_db_session
from src.models.user import UserRole
from src.operations.write_framework import (
    RiskLevel,
    WriteOperation,
    WriteOperationExecutor,
    build_connector_registry_from_settings,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


def _build_executor() -> WriteOperationExecutor:
    """Build the module-level executor with a real ConnectorRegistry.

    Called once at module import time.  The registry is populated from
    application Settings so connector endpoints and credentials are never
    hardcoded here.

    Falls back to a registry-less executor if Settings construction fails
    (e.g., during unit tests that don't export all env vars).  In that case
    execution requests will return a descriptive failure rather than crashing
    the whole application.
    """
    try:
        settings = get_settings()
        registry = build_connector_registry_from_settings(settings)
        log.info(
            "operations.executor_initialized",
            connectors=registry.known_connectors(),
        )
        return WriteOperationExecutor(connector_registry=registry)
    except Exception as exc:
        log.warning(
            "operations.executor_fallback_no_registry",
            error=str(exc),
        )
        return WriteOperationExecutor()


# Module-level executor — shared across all requests (in-memory store).
# Wired with a real ConnectorRegistry built from Settings.
_executor = _build_executor()


# ------------------------------------------------------------------ #
# MFA helpers
# ------------------------------------------------------------------ #


def _verify_mfa(
    mfa_code: str | None,
    user: AuthenticatedUser,
    settings: Settings,
) -> bool:
    """Verify an MFA code against the user's TOTP secret.

    Behaviour matrix:
    - mfa_enabled=False: return True only when a code was supplied (backward-compat).
    - mfa_enabled=True, code absent: return False immediately.
    - mfa_enabled=True, user has a per-user TOTP secret (stored in JWT claim
      ``totp_secret``): validate with pyotp; allow a ±1 window (30-second drift).
    - mfa_enabled=True, no per-user secret but static fallback configured:
      compare against ``settings.mfa_static_code``.
    - mfa_enabled=True, no per-user secret and no static fallback: return False.

    Returns:
        True if the code is valid, False otherwise.
    """
    if not settings.mfa_enabled:
        # When MFA is disabled, treat any non-empty code as verified (backward-compat).
        return bool(mfa_code)

    if not mfa_code:
        return False

    # Prefer a per-user TOTP secret if the identity provider injects it as a
    # JWT claim.  This keeps the secret out of our database and lets the IdP
    # manage per-user MFA enrollment.
    totp_secret: str | None = user.claims.get("totp_secret")

    if totp_secret:
        try:
            return pyotp.TOTP(totp_secret).verify(mfa_code, valid_window=1)
        except Exception:
            # Malformed secret — treat as unverified rather than crashing.
            log.warning(
                "api.mfa_invalid_totp_secret",
                user_id=str(user.id),
            )
            return False

    # Fall back to a globally-configured static code (e.g., for development or
    # simple deployments that haven't rolled out per-user TOTP yet).
    if settings.mfa_static_code:
        return mfa_code == settings.mfa_static_code

    # No TOTP secret and no static fallback — cannot verify.
    log.warning(
        "api.mfa_no_secret_configured",
        user_id=str(user.id),
    )
    return False


# ------------------------------------------------------------------ #
# Request/Response models
# ------------------------------------------------------------------ #


class ProposeOperationRequest(BaseModel):
    """Request to propose a write operation."""

    connector: str = Field(..., description="Connector name (sap, mes, etc.)")
    operation_type: str = Field(..., description="Operation type")
    params: dict[str, Any] = Field(..., description="Operation parameters")
    description: str = Field(..., description="Human-readable description")
    risk_level: RiskLevel = Field(..., description="Risk level")


class ApproveOperationRequest(BaseModel):
    """Request to approve an operation."""

    mfa_code: str | None = Field(None, description="MFA verification code")


class RejectOperationRequest(BaseModel):
    """Request to reject an operation."""

    reason: str = Field(..., description="Reason for rejection", min_length=10)


class OperationResponse(BaseModel):
    """Response containing operation details."""

    id: str
    tenant_id: str
    user_id: str
    connector: str
    operation_type: str
    params: dict[str, Any]
    description: str
    risk_level: str
    requires_approval: bool
    requires_mfa: bool
    status: str
    proposed_at: str
    approved_at: str | None
    executed_at: str | None
    approved_by: str | None
    audit_trail: list[dict[str, Any]]

    @classmethod
    def from_operation(cls, op: WriteOperation) -> OperationResponse:
        """Convert WriteOperation to response model."""
        return cls(**op.to_dict())


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #


@router.post("/propose", response_model=OperationResponse, status_code=status.HTTP_201_CREATED)
async def propose_operation(
    request: ProposeOperationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    """Propose a write operation for approval.

    LOW risk operations are auto-approved.
    Others go to PENDING_APPROVAL state.

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    # Create operation
    operation = WriteOperation(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        connector=request.connector,
        operation_type=request.operation_type,
        params=request.params,
        description=request.description,
        risk_level=request.risk_level,
    )

    # Propose
    proposed = await _executor.propose(operation)

    log.info(
        "api.operation_proposed",
        operation_id=proposed.id,
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        status=proposed.status,
    )

    return OperationResponse.from_operation(proposed)


@router.get("/pending", response_model=list[OperationResponse])
async def get_pending_operations(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[OperationResponse]:
    """Get all pending approval operations for the current tenant.

    Only shows operations for the user's tenant (tenant isolation).

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    pending = await _executor.get_pending(tenant_id=current_user.tenant_id)

    log.info(
        "api.pending_operations_retrieved",
        count=len(pending),
        tenant_id=str(current_user.tenant_id),
    )

    return [OperationResponse.from_operation(op) for op in pending]


@router.post("/{operation_id}/approve", response_model=OperationResponse)
async def approve_operation(
    operation_id: str,
    request: ApproveOperationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> OperationResponse:
    """Approve a pending operation.

    MFA verification required for CRITICAL risk operations.

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    try:
        # Get operation to check tenant isolation
        operation = await _executor.get_operation(operation_id)

        # Enforce tenant isolation
        if operation.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found",
            )

        # MFA verification
        mfa_verified = _verify_mfa(
            mfa_code=request.mfa_code,
            user=current_user,
            settings=settings,
        )

        if operation.requires_mfa and not mfa_verified:
            log.warning(
                "api.mfa_verification_failed",
                operation_id=operation_id,
                user_id=str(current_user.id),
                tenant_id=str(current_user.tenant_id),
                mfa_enabled=settings.mfa_enabled,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="MFA verification required and failed for this operation",
            )

        # Approve
        approved = await _executor.approve(
            operation_id=operation_id,
            approver_user_id=current_user.id,
            mfa_verified=mfa_verified,
        )

        log.info(
            "api.operation_approved",
            operation_id=operation_id,
            approver=str(current_user.id),
            tenant_id=str(current_user.tenant_id),
        )

        return OperationResponse.from_operation(approved)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.post("/{operation_id}/reject", response_model=OperationResponse)
async def reject_operation(
    operation_id: str,
    request: RejectOperationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    """Reject a pending operation.

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    try:
        # Get operation to check tenant isolation
        operation = await _executor.get_operation(operation_id)

        # Enforce tenant isolation
        if operation.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found",
            )

        # Reject
        rejected = await _executor.reject(
            operation_id=operation_id,
            rejector_user_id=current_user.id,
            reason=request.reason,
        )

        log.info(
            "api.operation_rejected",
            operation_id=operation_id,
            rejector=str(current_user.id),
            tenant_id=str(current_user.tenant_id),
        )

        return OperationResponse.from_operation(rejected)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> OperationResponse:
    """Get operation details including full audit trail.

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    try:
        operation = await _executor.get_operation(operation_id)

        # Enforce tenant isolation
        if operation.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found",
            )

        return OperationResponse.from_operation(operation)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )


@router.get("/history", response_model=list[OperationResponse])
async def get_operation_history(
    limit: int = 100,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[OperationResponse]:
    """Get operation history for the current tenant.

    Returns operations ordered by proposed_at DESC (most recent first).
    Default limit: 100.

    Requires OPERATOR role or higher.
    """
    require_role(current_user.role, UserRole.OPERATOR)

    history = await _executor.get_history(
        tenant_id=current_user.tenant_id,
        limit=min(limit, 1000),  # Cap at 1000
    )

    log.info(
        "api.operation_history_retrieved",
        count=len(history),
        tenant_id=str(current_user.tenant_id),
    )

    return [OperationResponse.from_operation(op) for op in history]


class ExecuteOperationResponse(BaseModel):
    """Response from executing an approved operation."""

    operation: OperationResponse
    success: bool
    data: Any
    error: str | None
    metadata: dict[str, Any]


@router.post("/{operation_id}/execute", response_model=ExecuteOperationResponse)
async def execute_operation(
    operation_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ExecuteOperationResponse:
    """Execute an approved write operation via the connector registry.

    Routes the operation to the appropriate connector (SAP, MES, etc.)
    based on the ``connector`` field set at proposal time.

    Only APPROVED operations can be executed.  Tenant isolation is
    enforced: a user can only execute operations belonging to their tenant.

    Requires OPERATOR role or higher.

    Returns:
        The updated operation state together with the raw connector result.

    Raises:
        404 if the operation does not exist or belongs to another tenant.
        400 if the operation is not in APPROVED state.
        500 if the connector registry is not configured or the connector
            raises an unexpected error (error details are included in the
            response body, not just the HTTP status, to aid debugging).
    """
    require_role(current_user.role, UserRole.OPERATOR)

    try:
        # Tenant isolation check before execution.
        operation = await _executor.get_operation(operation_id)
        if operation.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found",
            )

        # Delegate to the executor; it validates status internally.
        result: ConnectorResult = await _executor.execute(operation_id=operation_id)

        # Retrieve the updated operation record.
        updated_op = await _executor.get_operation(operation_id)

        log.info(
            "api.operation_executed",
            operation_id=operation_id,
            user_id=str(current_user.id),
            tenant_id=str(current_user.tenant_id),
            success=result.success,
            connector=updated_op.connector,
        )

        return ExecuteOperationResponse(
            operation=OperationResponse.from_operation(updated_op),
            success=result.success,
            data=result.data,
            error=result.error,
            metadata=result.metadata,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
