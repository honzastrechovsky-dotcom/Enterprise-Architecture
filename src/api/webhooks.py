"""Webhook management API endpoints.

Routes:
  POST   /api/v1/webhooks                        - Register a new webhook
  GET    /api/v1/webhooks                        - List webhooks for tenant
  DELETE /api/v1/webhooks/{webhook_id}           - Remove a webhook
  POST   /api/v1/webhooks/{webhook_id}/test      - Send a test event
  GET    /api/v1/webhooks/{webhook_id}/deliveries - Delivery history
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.core.policy import Permission, check_permission
from src.database import get_db_session
from src.services.webhook import SUPPORTED_EVENTS, WebhookService

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
)


# ------------------------------------------------------------------ #
# Pydantic schemas
# ------------------------------------------------------------------ #


class WebhookCreateRequest(BaseModel):
    """Request body for registering a new webhook endpoint."""

    url: HttpUrl = Field(
        ...,
        description="HTTPS endpoint that will receive event POST requests.",
        examples=["https://myapp.example.com/webhooks/eap"],
    )
    events: list[str] = Field(
        ...,
        description=(
            "List of event types to subscribe to. "
            f"Supported: {sorted(SUPPORTED_EVENTS)}"
        ),
        min_length=1,
        examples=[["agent.completed", "document.ingested"]],
    )
    secret: str = Field(
        ...,
        description=(
            "HMAC secret used to sign deliveries. "
            "Sent in X-EAP-Signature-256 header as 'sha256=<hex>'. "
            "This value is never stored in plaintext."
        ),
        min_length=16,
        examples=["my-super-secret-signing-key-here"],
    )


class WebhookResponse(BaseModel):
    """Registered webhook details returned to the caller."""

    id: uuid.UUID = Field(..., description="Webhook unique identifier.")
    tenant_id: uuid.UUID = Field(..., description="Owning tenant UUID.")
    url: str = Field(..., description="Target HTTPS endpoint URL.")
    events: list[str] = Field(..., description="Subscribed event types.")
    enabled: bool = Field(..., description="Whether delivery is active.")
    created_at: datetime = Field(..., description="Registration timestamp (UTC).")
    updated_at: datetime = Field(..., description="Last modification timestamp (UTC).")

    model_config = {"from_attributes": True}


class WebhookListResponse(BaseModel):
    """Paginated list of webhooks for the tenant."""

    items: list[WebhookResponse]
    total: int


class DeliveryResponse(BaseModel):
    """A single webhook delivery attempt record."""

    id: uuid.UUID = Field(..., description="Delivery unique identifier.")
    webhook_id: uuid.UUID = Field(..., description="Parent webhook UUID.")
    event_type: str = Field(..., description="Event type that triggered delivery.")
    payload: dict[str, Any] = Field(..., description="Event payload that was sent.")
    status: str = Field(..., description="pending | delivered | failed")
    response_code: int | None = Field(None, description="HTTP response code received.")
    attempts: int = Field(..., description="Number of delivery attempts made.")
    next_retry_at: datetime | None = Field(
        None, description="Scheduled time for next retry attempt."
    )
    created_at: datetime = Field(..., description="When this delivery was first attempted.")

    model_config = {"from_attributes": True}


class DeliveryListResponse(BaseModel):
    """Recent delivery history for a webhook."""

    items: list[DeliveryResponse]
    total: int


class WebhookTestRequest(BaseModel):
    """Optional body for a manual test delivery."""

    event_type: str = Field(
        default="agent.completed",
        description="Event type to simulate.",
        examples=["agent.completed"],
    )


class RetryResponse(BaseModel):
    """Result of a manual retry operation."""

    retried: int = Field(..., description="Number of deliveries retried.")


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #


@router.post(
    "",
    response_model=WebhookResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint",
    description=(
        "Register a new HTTPS endpoint to receive event notifications. "
        "The endpoint must be reachable and the secret must be at least 16 characters. "
        "Each delivery includes an HMAC-SHA256 signature in the ``X-EAP-Signature-256`` header."
    ),
    responses={
        201: {"description": "Webhook registered successfully."},
        400: {"description": "Invalid event types or unreachable URL."},
        422: {"description": "Request validation error."},
    },
)
async def register_webhook(
    body: WebhookCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """Register a new webhook for the authenticated tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    svc = WebhookService(db)

    # Verify endpoint reachability before persisting
    url_str = str(body.url)
    reachable = await svc.verify_endpoint(url_str)
    if not reachable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Webhook endpoint is not reachable: {url_str}",
        )

    try:
        webhook = await svc.register(
            tenant_id=current_user.tenant_id,
            url=url_str,
            events=body.events,
            secret=body.secret,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return WebhookResponse(
        id=webhook.id,
        tenant_id=webhook.tenant_id,
        url=webhook.url,
        events=webhook.events,
        enabled=webhook.enabled,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


@router.get(
    "",
    response_model=WebhookListResponse,
    summary="List registered webhooks",
    description="Return all webhook endpoints registered for the authenticated tenant.",
    responses={
        200: {"description": "List of webhooks returned successfully."},
    },
)
async def list_webhooks(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookListResponse:
    """List all webhooks registered for the current tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    svc = WebhookService(db)
    webhooks = await svc.list_for_tenant(current_user.tenant_id)

    items = [
        WebhookResponse(
            id=wh.id,
            tenant_id=wh.tenant_id,
            url=wh.url,
            events=wh.events,
            enabled=wh.enabled,
            created_at=wh.created_at,
            updated_at=wh.updated_at,
        )
        for wh in webhooks
    ]
    return WebhookListResponse(items=items, total=len(items))


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook",
    description="Remove a registered webhook endpoint. All delivery history is also deleted.",
    responses={
        204: {"description": "Webhook deleted successfully."},
        404: {"description": "Webhook not found for this tenant."},
    },
)
async def delete_webhook(
    webhook_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Delete a webhook by ID, scoped to the current tenant."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    svc = WebhookService(db)
    deleted = await svc.delete(webhook_id, current_user.tenant_id)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook '{webhook_id}' not found",
        )


@router.post(
    "/{webhook_id}/test",
    response_model=DeliveryResponse,
    summary="Send a test event to a webhook",
    description=(
        "Immediately deliver a synthetic test event to the registered endpoint. "
        "Useful for verifying connectivity and signature handling before relying on live events."
    ),
    responses={
        200: {"description": "Test event delivered (check response for status)."},
        404: {"description": "Webhook not found for this tenant."},
    },
)
async def test_webhook(
    webhook_id: uuid.UUID,
    body: WebhookTestRequest = WebhookTestRequest(),  # noqa: B008
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> DeliveryResponse:
    """Send a synthetic test event to a webhook endpoint."""
    check_permission(current_user.role, Permission.ADMIN_USER_WRITE)

    svc = WebhookService(db)
    webhook = await svc.get(webhook_id, current_user.tenant_id)

    if webhook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook '{webhook_id}' not found",
        )

    test_payload: dict[str, Any] = {
        "type": body.event_type,
        "test": True,
        "webhook_id": str(webhook_id),
        "data": {"message": "This is a test delivery from the Enterprise Agent Platform."},
    }

    deliveries = await svc.deliver(
        tenant_id=current_user.tenant_id,
        event_type=body.event_type,
        payload=test_payload,
    )

    # The test targets a specific webhook; find its delivery record
    for delivery in deliveries:
        if delivery.webhook_id == webhook_id:
            return DeliveryResponse(
                id=delivery.id,
                webhook_id=delivery.webhook_id,
                event_type=delivery.event_type,
                payload=delivery.payload,
                status=delivery.status,
                response_code=delivery.response_code,
                attempts=delivery.attempts,
                next_retry_at=delivery.next_retry_at,
                created_at=delivery.created_at,
            )

    # Webhook exists but event_type not in subscriptions - deliver directly
    deliveries = await svc.deliver(
        tenant_id=current_user.tenant_id,
        event_type="agent.completed",
        payload=test_payload,
    )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Webhook is not subscribed to event type '{body.event_type}'. "
            f"Subscribed events: {webhook.events}"
        ),
    )


@router.get(
    "/{webhook_id}/deliveries",
    response_model=DeliveryListResponse,
    summary="Get delivery history for a webhook",
    description=(
        "Return the most recent delivery attempts for a webhook, ordered by creation time descending. "
        "Includes status, HTTP response codes, and retry scheduling information."
    ),
    responses={
        200: {"description": "Delivery history returned successfully."},
        404: {"description": "Webhook not found for this tenant."},
    },
)
async def get_webhook_deliveries(
    webhook_id: uuid.UUID,
    limit: int = 50,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> DeliveryListResponse:
    """Return recent delivery history for a specific webhook."""
    check_permission(current_user.role, Permission.ADMIN_USER_READ)

    svc = WebhookService(db)
    webhook = await svc.get(webhook_id, current_user.tenant_id)

    if webhook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook '{webhook_id}' not found",
        )

    deliveries = await svc.get_deliveries(webhook_id, limit=limit)

    items = [
        DeliveryResponse(
            id=d.id,
            webhook_id=d.webhook_id,
            event_type=d.event_type,
            payload=d.payload,
            status=d.status,
            response_code=d.response_code,
            attempts=d.attempts,
            next_retry_at=d.next_retry_at,
            created_at=d.created_at,
        )
        for d in deliveries
    ]
    return DeliveryListResponse(items=items, total=len(items))
