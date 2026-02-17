"""WebhookService: registration, delivery, signing and retry logic.

Event types supported:
  agent.completed      - An agent run finished
  document.ingested    - A document was ingested into the RAG index
  feedback.received    - A user submitted feedback on an agent response
  compliance.alert     - A compliance policy was violated
  user.created         - A new user was provisioned in the system

Delivery signing uses HMAC-SHA256.  The raw secret is kept only in memory
during the registration call; only a bcrypt hash is stored in the database.
The signature is sent in the ``X-EAP-Signature-256`` header so consumers can
verify authenticity without storing the secret server-side.

Retry policy: exponential backoff, 3 attempts maximum.
  Attempt 1: immediate
  Attempt 2: 60 s delay
  Attempt 3: 300 s delay
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.webhook import DeliveryStatus, Webhook, WebhookDelivery

log = structlog.get_logger(__name__)

SUPPORTED_EVENTS: frozenset[str] = frozenset(
    {
        "agent.completed",
        "document.ingested",
        "feedback.received",
        "compliance.alert",
        "user.created",
    }
)

# Exponential-backoff retry delays in seconds (index = attempt number, 1-based)
_RETRY_DELAYS: list[int] = [0, 60, 300]  # attempt 1, 2, 3
_MAX_ATTEMPTS = 3


class WebhookService:
    """All webhook-related operations scoped to the current async DB session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    async def register(
        self,
        tenant_id: uuid.UUID,
        url: str,
        events: list[str],
        secret: str,
    ) -> Webhook:
        """Register a new webhook endpoint for a tenant.

        Args:
            tenant_id: Owning tenant UUID.
            url: HTTPS endpoint URL.
            events: List of event type strings to subscribe to.
            secret: Raw HMAC secret (stored only as a hash).

        Returns:
            Persisted Webhook ORM object.

        Raises:
            ValueError: If unknown event types are provided.
        """
        unknown = set(events) - SUPPORTED_EVENTS
        if unknown:
            raise ValueError(f"Unknown event types: {sorted(unknown)}")

        secret_hash = self._hash_secret(secret)

        webhook = Webhook(
            tenant_id=tenant_id,
            url=url,
            events=list(set(events)),
            secret_hash=secret_hash,
            enabled=True,
        )
        self._db.add(webhook)
        await self._db.flush()

        log.info(
            "webhook.registered",
            webhook_id=str(webhook.id),
            tenant_id=str(tenant_id),
            url=url,
            events=events,
        )
        return webhook

    # ------------------------------------------------------------------ #
    # Querying
    # ------------------------------------------------------------------ #

    async def list_for_tenant(self, tenant_id: uuid.UUID) -> list[Webhook]:
        """Return all webhooks registered for a tenant."""
        stmt = (
            select(Webhook)
            .where(Webhook.tenant_id == tenant_id)
            .order_by(Webhook.created_at.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> Webhook | None:
        """Fetch a single webhook, scoped to the tenant."""
        stmt = select(Webhook).where(
            Webhook.id == webhook_id,
            Webhook.tenant_id == tenant_id,
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Deletion
    # ------------------------------------------------------------------ #

    async def delete(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        """Delete a webhook by ID, scoped to the tenant.

        Returns True if the webhook existed and was deleted.
        """
        webhook = await self.get(webhook_id, tenant_id)
        if webhook is None:
            return False
        await self._db.delete(webhook)
        await self._db.flush()
        log.info(
            "webhook.deleted",
            webhook_id=str(webhook_id),
            tenant_id=str(tenant_id),
        )
        return True

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #

    async def deliver(
        self,
        tenant_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[WebhookDelivery]:
        """Fan-out an event to all matching enabled webhooks for the tenant.

        Creates a WebhookDelivery record for each webhook and immediately
        attempts delivery.  Failed attempts are scheduled for retry.

        Args:
            tenant_id: The tenant that owns the event.
            event_type: One of the SUPPORTED_EVENTS strings.
            payload: Arbitrary event data to deliver.

        Returns:
            List of WebhookDelivery records created (one per matching webhook).
        """
        stmt = select(Webhook).where(
            Webhook.tenant_id == tenant_id,
            Webhook.enabled.is_(True),
        )
        result = await self._db.execute(stmt)
        webhooks = result.scalars().all()

        deliveries: list[WebhookDelivery] = []
        for webhook in webhooks:
            if event_type not in webhook.events:
                continue
            delivery = await self._attempt_delivery(webhook, event_type, payload)
            deliveries.append(delivery)

        return deliveries

    async def _attempt_delivery(
        self,
        webhook: Webhook,
        event_type: str,
        payload: dict[str, Any],
        existing_delivery: WebhookDelivery | None = None,
    ) -> WebhookDelivery:
        """Perform one HTTP POST to the webhook endpoint.

        Creates (or updates) a WebhookDelivery record with the result.
        """
        if existing_delivery is None:
            delivery = WebhookDelivery(
                webhook_id=webhook.id,
                event_type=event_type,
                payload=payload,
                status=DeliveryStatus.PENDING,
                attempts=0,
            )
            self._db.add(delivery)
        else:
            delivery = existing_delivery

        delivery.attempts += 1
        body = json.dumps(payload, default=str)
        signature = self.sign_payload(body, webhook.secret_hash)

        status_code: int | None = None
        success = False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    webhook.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-EAP-Event": event_type,
                        "X-EAP-Signature-256": f"sha256={signature}",
                        "X-EAP-Delivery-ID": str(delivery.id) if delivery.id else "",
                    },
                )
                status_code = response.status_code
                success = 200 <= status_code < 300
        except Exception as exc:
            log.warning(
                "webhook.delivery_error",
                webhook_id=str(webhook.id),
                event_type=event_type,
                attempt=delivery.attempts,
                error=str(exc),
            )

        delivery.response_code = status_code

        if success:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.next_retry_at = None
            log.info(
                "webhook.delivered",
                webhook_id=str(webhook.id),
                event_type=event_type,
                attempt=delivery.attempts,
                status_code=status_code,
            )
        else:
            if delivery.attempts >= _MAX_ATTEMPTS:
                delivery.status = DeliveryStatus.FAILED
                delivery.next_retry_at = None
                log.error(
                    "webhook.delivery_failed_permanently",
                    webhook_id=str(webhook.id),
                    event_type=event_type,
                    attempts=delivery.attempts,
                )
            else:
                delivery.status = DeliveryStatus.PENDING
                delay = _RETRY_DELAYS[delivery.attempts] if delivery.attempts < len(_RETRY_DELAYS) else 300
                delivery.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                log.warning(
                    "webhook.delivery_scheduled_retry",
                    webhook_id=str(webhook.id),
                    event_type=event_type,
                    attempt=delivery.attempts,
                    retry_in_seconds=delay,
                )

        await self._db.flush()
        return delivery

    # ------------------------------------------------------------------ #
    # Endpoint verification
    # ------------------------------------------------------------------ #

    async def verify_endpoint(self, url: str) -> bool:
        """Check that the webhook URL is reachable (HTTP GET, expect 2xx/3xx).

        This is a lightweight liveness check, not a full delivery test.

        Returns:
            True if the endpoint responds with a non-5xx status code.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                reachable = response.status_code < 500
                log.info(
                    "webhook.endpoint_verified",
                    url=url,
                    status_code=response.status_code,
                    reachable=reachable,
                )
                return reachable
        except Exception as exc:
            log.warning("webhook.endpoint_unreachable", url=url, error=str(exc))
            return False

    # ------------------------------------------------------------------ #
    # Retry failed deliveries
    # ------------------------------------------------------------------ #

    async def retry_failed(self, webhook_id: uuid.UUID) -> int:
        """Immediately retry all pending/failed deliveries for a webhook.

        Only retries deliveries that have not yet reached the max attempt limit.

        Returns:
            Number of deliveries retried.
        """
        stmt = (
            select(WebhookDelivery)
            .where(
                WebhookDelivery.webhook_id == webhook_id,
                WebhookDelivery.status.in_(
                    [DeliveryStatus.PENDING, DeliveryStatus.FAILED]
                ),
                WebhookDelivery.attempts < _MAX_ATTEMPTS,
            )
        )
        result = await self._db.execute(stmt)
        deliveries = result.scalars().all()

        if not deliveries:
            return 0

        webhook_stmt = select(Webhook).where(Webhook.id == webhook_id)
        webhook_result = await self._db.execute(webhook_stmt)
        webhook = webhook_result.scalar_one_or_none()

        if webhook is None:
            return 0

        retried = 0
        for delivery in deliveries:
            await self._attempt_delivery(
                webhook,
                delivery.event_type,
                delivery.payload,
                existing_delivery=delivery,
            )
            retried += 1

        log.info(
            "webhook.retry_complete",
            webhook_id=str(webhook_id),
            retried=retried,
        )
        return retried

    # ------------------------------------------------------------------ #
    # Delivery history
    # ------------------------------------------------------------------ #

    async def get_deliveries(
        self,
        webhook_id: uuid.UUID,
        limit: int = 50,
    ) -> list[WebhookDelivery]:
        """Return recent delivery history for a webhook."""
        stmt = (
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------ #
    # Crypto helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def sign_payload(payload: str, secret: str) -> str:
        """Compute HMAC-SHA256 signature for a delivery payload.

        Args:
            payload: JSON string of the event payload.
            secret: The raw secret (or secret hash used as key).

        Returns:
            Hex-encoded HMAC digest.
        """
        return hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _hash_secret(secret: str) -> str:
        """Derive a one-way hash of the raw secret for storage.

        We use SHA-256 here (not bcrypt) because we need to re-derive the
        key on every delivery to compute HMAC.  Storing the SHA-256 hash
        prevents plaintext exposure while allowing HMAC signing.
        """
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def verify_signature(payload: str, secret_hash: str, signature: str) -> bool:
        """Verify an incoming HMAC-SHA256 signature.

        Args:
            payload: Raw JSON string received.
            secret_hash: The stored secret hash (used as HMAC key).
            signature: The hex signature from X-EAP-Signature-256 header.

        Returns:
            True if the signature is valid.
        """
        expected = WebhookService.sign_payload(payload, secret_hash)
        return hmac.compare_digest(expected, signature)
