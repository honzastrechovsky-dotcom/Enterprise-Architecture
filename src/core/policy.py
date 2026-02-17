"""Policy engine - RBAC and tenant isolation enforcement.

This module is the enforcement point for two critical security properties:

1. Tenant Isolation (mandatory):
   Every query that touches a tenant-scoped table MUST be filtered by
   tenant_id. The apply_tenant_filter() function is the canonical way to
   add this filter. All service functions must call it.

2. RBAC (mandatory):
   Role-based access control is checked via check_permission() before
   any write operation. Read operations are permitted to all authenticated
   users within the same tenant.

Permission matrix:
  Action          | admin | operator | viewer
  ----------------|-------|----------|-------
  chat.send       |  yes  |   yes    |  yes
  document.upload |  yes  |   yes    |  no
  document.delete |  yes  |   no     |  no
  admin.*         |  yes  |   no     |  no
  user.manage     |  yes  |   no     |  no
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TypeVar

import structlog
from fastapi import HTTPException, status
from sqlalchemy import Select

from src.models.user import UserRole

log = structlog.get_logger(__name__)

T = TypeVar("T")


class Permission(StrEnum):
    # Chat
    CHAT_SEND = "chat.send"

    # Documents
    DOCUMENT_READ = "document.read"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DELETE = "document.delete"

    # Conversations
    CONVERSATION_READ = "conversation.read"
    CONVERSATION_WRITE = "conversation.write"
    CONVERSATION_DELETE = "conversation.delete"

    # Admin
    ADMIN_TENANT_READ = "admin.tenant.read"
    ADMIN_TENANT_WRITE = "admin.tenant.write"
    ADMIN_USER_READ = "admin.user.read"
    ADMIN_USER_WRITE = "admin.user.write"

    # Audit
    AUDIT_READ = "audit.read"

    # Classification & Compliance (see Data Classification Policy)
    CLASSIFICATION_III_ACCESS = "classification.class_iii.access"
    CLASSIFICATION_IV_APPROVAL = "classification.class_iv.approval"
    EXPORT_CONTROL_OVERRIDE = "export_control.override"
    PII_VIEW_UNREDACTED = "pii.view_unredacted"

    # Plans
    CREATE_PLAN = "plan.create"
    APPROVE_PLAN = "plan.approve"

    # Feedback
    FEEDBACK_SUBMIT = "feedback.submit"
    FEEDBACK_READ = "feedback.read"
    FEEDBACK_EXPORT = "feedback.export"

    # Analytics
    ANALYTICS_READ = "analytics.read"

    # Fine-tuning
    FINETUNING_MANAGE = "finetuning.manage"
    FINETUNING_READ = "finetuning.read"
    FINETUNING_EXPORT = "finetuning.export"


# Permission -> minimum required role (inclusive upward)
_PERMISSION_TO_MIN_ROLE: dict[Permission, UserRole] = {
    Permission.CHAT_SEND: UserRole.VIEWER,
    Permission.DOCUMENT_READ: UserRole.VIEWER,
    Permission.CONVERSATION_READ: UserRole.VIEWER,
    Permission.CONVERSATION_WRITE: UserRole.VIEWER,
    Permission.DOCUMENT_UPLOAD: UserRole.OPERATOR,
    Permission.CONVERSATION_DELETE: UserRole.OPERATOR,
    Permission.DOCUMENT_DELETE: UserRole.ADMIN,
    Permission.ADMIN_TENANT_READ: UserRole.ADMIN,
    Permission.ADMIN_TENANT_WRITE: UserRole.ADMIN,
    Permission.ADMIN_USER_READ: UserRole.ADMIN,
    Permission.ADMIN_USER_WRITE: UserRole.ADMIN,
    Permission.AUDIT_READ: UserRole.ADMIN,
    Permission.CLASSIFICATION_III_ACCESS: UserRole.OPERATOR,
    Permission.CLASSIFICATION_IV_APPROVAL: UserRole.ADMIN,
    Permission.EXPORT_CONTROL_OVERRIDE: UserRole.ADMIN,
    Permission.PII_VIEW_UNREDACTED: UserRole.ADMIN,
    Permission.CREATE_PLAN: UserRole.OPERATOR,
    Permission.APPROVE_PLAN: UserRole.OPERATOR,
    # Feedback - all authenticated users can submit feedback
    Permission.FEEDBACK_SUBMIT: UserRole.VIEWER,
    Permission.FEEDBACK_READ: UserRole.OPERATOR,
    Permission.FEEDBACK_EXPORT: UserRole.ADMIN,
    # Analytics - operators and above can read
    Permission.ANALYTICS_READ: UserRole.OPERATOR,
    # Fine-tuning - operators can create, admins can export
    Permission.FINETUNING_MANAGE: UserRole.OPERATOR,
    Permission.FINETUNING_READ: UserRole.OPERATOR,
    Permission.FINETUNING_EXPORT: UserRole.ADMIN,
}

# Role hierarchy: higher index = more permissions
_ROLE_HIERARCHY = [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN]


def _role_level(role: UserRole) -> int:
    try:
        return _ROLE_HIERARCHY.index(role)
    except ValueError:
        return -1


def check_permission(
    user_role: UserRole,
    permission: Permission,
    *,
    raise_on_failure: bool = True,
) -> bool:
    """Check whether user_role satisfies the required permission.

    If raise_on_failure=True (default), raises HTTP 403 on failure.
    If raise_on_failure=False, returns False instead.
    """
    min_role = _PERMISSION_TO_MIN_ROLE.get(permission, UserRole.ADMIN)
    has_permission = _role_level(user_role) >= _role_level(min_role)

    if not has_permission:
        log.warning(
            "policy.permission_denied",
            role=user_role,
            permission=permission,
            required_role=min_role,
        )
        if raise_on_failure:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: '{permission}' requires role '{min_role}' or higher",
            )
        return False

    return True


def apply_tenant_filter(stmt: Select[T], model_class: type, tenant_id: uuid.UUID) -> Select[T]:
    """Add WHERE tenant_id = :tenant_id to a SQLAlchemy select statement.

    This is the MANDATORY function for adding tenant isolation to queries.
    Every query that touches a tenant-scoped table must call this.

    Usage:
        stmt = apply_tenant_filter(select(Conversation), Conversation, tenant_id)
        result = await db.execute(stmt)

    Raises AttributeError if the model does not have a tenant_id column,
    which forces developers to notice missing tenant scoping early.
    """
    if not hasattr(model_class, "tenant_id"):
        raise AttributeError(
            f"Model {model_class.__name__} does not have a tenant_id column. "
            "Every tenant-scoped model must have tenant_id."
        )
    return stmt.where(model_class.tenant_id == tenant_id)  # type: ignore[return-value]


def require_role(user_role: UserRole, minimum_role: UserRole) -> None:
    """Require that a user's role meets the minimum required role.

    Raises HTTP 403 if the user's role is below the minimum required.

    Args:
        user_role: The user's current role.
        minimum_role: The minimum role required.

    Raises:
        HTTPException: 403 if the user's role is insufficient.
    """
    if _role_level(user_role) < _role_level(minimum_role):
        log.warning(
            "policy.role_requirement_not_met",
            role=user_role,
            required_role=minimum_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{minimum_role}' or higher required, got '{user_role}'",
        )


def assert_resource_belongs_to_tenant(
    resource_tenant_id: uuid.UUID,
    requesting_tenant_id: uuid.UUID,
    resource_name: str = "resource",
) -> None:
    """Assert that a resource's tenant matches the requesting tenant.

    Used after fetching a resource by ID to prevent cross-tenant access
    when a user somehow guesses another tenant's resource UUID.

    Raises HTTP 404 (not 403) intentionally - we do not confirm existence
    of resources belonging to other tenants.
    """
    if resource_tenant_id != requesting_tenant_id:
        log.warning(
            "policy.cross_tenant_access_attempt",
            resource_tenant=str(resource_tenant_id),
            requesting_tenant=str(requesting_tenant_id),
            resource=resource_name,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name} not found",
        )
