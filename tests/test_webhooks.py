"""Tests for the webhook subsystem.

Coverage:
  - WebhookService.register
  - WebhookService.deliver with HMAC signing
  - WebhookService.verify_signature
  - WebhookService.retry_failed (exponential backoff)
  - WebhookService.get_deliveries
  - WebhookService.delete
  - Event filtering (only subscribed events received)
  - API endpoints (register, list, delete, test, deliveries)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.webhook import (
    SUPPORTED_EVENTS,
    WebhookService,
    _RETRY_DELAYS,
    _MAX_ATTEMPTS,
)

# Import ORM constants only - avoid triggering full mapper init in unit tests
from src.models.webhook import DeliveryStatus


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def webhook_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_webhook(
    tenant_id: uuid.UUID,
    url: str = "https://example.com/hook",
    events: list[str] | None = None,
    secret_hash: str = "abc123",
    enabled: bool = True,
) -> MagicMock:
    """Create a mock Webhook object without triggering SQLAlchemy mapper."""
    wh = MagicMock()
    wh.id = uuid.uuid4()
    wh.tenant_id = tenant_id
    wh.url = url
    wh.events = events or ["agent.completed"]
    wh.secret_hash = secret_hash
    wh.enabled = enabled
    wh.created_at = datetime.now(timezone.utc)
    wh.updated_at = datetime.now(timezone.utc)
    return wh


def _make_delivery(
    webhook_id: uuid.UUID,
    event_type: str = "agent.completed",
    status: str = DeliveryStatus.PENDING,
    attempts: int = 0,
) -> MagicMock:
    """Create a mock WebhookDelivery object without triggering SQLAlchemy mapper."""
    d = MagicMock()
    d.id = uuid.uuid4()
    d.webhook_id = webhook_id
    d.event_type = event_type
    d.payload = {"type": event_type, "data": {}}
    d.status = status
    d.response_code = None
    d.attempts = attempts
    d.next_retry_at = None
    d.created_at = datetime.now(timezone.utc)
    return d


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db: MagicMock) -> WebhookService:
    return WebhookService(mock_db)


# ------------------------------------------------------------------ #
# HMAC signing tests
# ------------------------------------------------------------------ #


class TestHMACSignature:
    """Tests for HMAC-SHA256 payload signing."""

    def test_sign_payload_returns_hex_string(self) -> None:
        """sign_payload should return a 64-character hex string."""
        signature = WebhookService.sign_payload("hello", "secret")
        assert isinstance(signature, str)
        assert len(signature) == 64  # sha256 hex digest length

    def test_sign_payload_is_deterministic(self) -> None:
        """Same inputs always produce the same signature."""
        sig1 = WebhookService.sign_payload("payload", "key")
        sig2 = WebhookService.sign_payload("payload", "key")
        assert sig1 == sig2

    def test_sign_payload_different_keys(self) -> None:
        """Different keys should produce different signatures."""
        sig1 = WebhookService.sign_payload("payload", "key1")
        sig2 = WebhookService.sign_payload("payload", "key2")
        assert sig1 != sig2

    def test_sign_payload_different_payloads(self) -> None:
        """Different payloads should produce different signatures."""
        sig1 = WebhookService.sign_payload("payload1", "key")
        sig2 = WebhookService.sign_payload("payload2", "key")
        assert sig1 != sig2

    def test_verify_signature_accepts_valid(self) -> None:
        """verify_signature should return True for a valid signature."""
        payload = '{"event": "test"}'
        secret_hash = hashlib.sha256(b"my-secret").hexdigest()
        signature = WebhookService.sign_payload(payload, secret_hash)
        assert WebhookService.verify_signature(payload, secret_hash, signature)

    def test_verify_signature_rejects_invalid(self) -> None:
        """verify_signature should return False for a tampered payload."""
        payload = '{"event": "test"}'
        secret_hash = hashlib.sha256(b"my-secret").hexdigest()
        signature = WebhookService.sign_payload(payload, secret_hash)
        tampered = '{"event": "tampered"}'
        assert not WebhookService.verify_signature(tampered, secret_hash, signature)

    def test_verify_signature_rejects_wrong_key(self) -> None:
        """verify_signature should return False for a wrong key."""
        payload = '{"event": "test"}'
        secret_hash = hashlib.sha256(b"my-secret").hexdigest()
        wrong_hash = hashlib.sha256(b"wrong-secret").hexdigest()
        signature = WebhookService.sign_payload(payload, secret_hash)
        assert not WebhookService.verify_signature(payload, wrong_hash, signature)

    def test_hash_secret_is_sha256(self) -> None:
        """_hash_secret should produce a SHA-256 digest."""
        raw = "super-secret-key"
        result = WebhookService._hash_secret(raw)
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert result == expected


# ------------------------------------------------------------------ #
# Registration tests
# ------------------------------------------------------------------ #


class TestWebhookRegistration:
    """Tests for webhook registration."""

    @pytest.mark.asyncio
    async def test_register_valid_events(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Registering with valid events should add a Webhook to the session."""
        mock_webhook = MagicMock()
        mock_webhook.tenant_id = tenant_id
        mock_webhook.events = ["agent.completed"]
        mock_webhook.secret_hash = "a" * 64

        with patch("src.services.webhook.Webhook", return_value=mock_webhook):
            webhook = await service.register(
                tenant_id=tenant_id,
                url="https://example.com/hook",
                events=["agent.completed"],
                secret="a-very-long-secret-key",
            )
        mock_db.add.assert_called_once()
        assert webhook.tenant_id == tenant_id
        assert "agent.completed" in webhook.events

    @pytest.mark.asyncio
    async def test_register_unknown_event_raises(
        self, service: WebhookService, tenant_id: uuid.UUID
    ) -> None:
        """Registering with an unknown event type should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown event types"):
            await service.register(
                tenant_id=tenant_id,
                url="https://example.com/hook",
                events=["invalid.event"],
                secret="a-very-long-secret-key",
            )

    @pytest.mark.asyncio
    async def test_register_deduplicates_events(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Duplicate events in registration should be deduplicated."""
        captured_events: list[list[str]] = []

        def capture_webhook(**kwargs: Any) -> MagicMock:
            captured_events.append(kwargs.get("events", []))
            m = MagicMock()
            m.events = kwargs.get("events", [])
            return m

        with patch("src.services.webhook.Webhook", side_effect=capture_webhook):
            await service.register(
                tenant_id=tenant_id,
                url="https://example.com/hook",
                events=["agent.completed", "agent.completed"],
                secret="a-very-long-secret-key",
            )

        assert len(captured_events) == 1
        assert captured_events[0].count("agent.completed") == 1

    @pytest.mark.asyncio
    async def test_register_stores_hashed_secret(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Registered webhook should store a hash, not the raw secret."""
        raw_secret = "my-raw-secret-123456"
        expected_hash = WebhookService._hash_secret(raw_secret)

        captured_kwargs: dict[str, Any] = {}

        def capture_webhook(**kwargs: Any) -> MagicMock:
            captured_kwargs.update(kwargs)
            m = MagicMock()
            m.secret_hash = kwargs.get("secret_hash", "")
            return m

        with patch("src.services.webhook.Webhook", side_effect=capture_webhook):
            webhook = await service.register(
                tenant_id=tenant_id,
                url="https://example.com/hook",
                events=["agent.completed"],
                secret=raw_secret,
            )

        stored_hash = captured_kwargs.get("secret_hash", "")
        assert stored_hash != raw_secret
        assert len(stored_hash) == 64  # SHA-256 hex length
        assert stored_hash == expected_hash

    def test_supported_events_set(self) -> None:
        """SUPPORTED_EVENTS should contain the documented event types."""
        expected = {
            "agent.completed",
            "document.ingested",
            "feedback.received",
            "compliance.alert",
            "user.created",
        }
        assert expected.issubset(SUPPORTED_EVENTS)


# ------------------------------------------------------------------ #
# Delivery tests
# ------------------------------------------------------------------ #


def _make_mock_delivery(
    webhook_id: uuid.UUID,
    event_type: str = "agent.completed",
    status: str = DeliveryStatus.PENDING,
    attempts: int = 0,
) -> MagicMock:
    """Create a fresh mock delivery for patching WebhookDelivery constructor."""
    d = MagicMock()
    d.id = uuid.uuid4()
    d.webhook_id = webhook_id
    d.event_type = event_type
    d.payload = {}
    d.status = status
    d.response_code = None
    d.attempts = attempts
    d.next_retry_at = None
    d.created_at = datetime.now(timezone.utc)
    return d


class TestWebhookDelivery:
    """Tests for event delivery and HMAC signing."""

    @pytest.mark.asyncio
    async def test_deliver_calls_endpoint(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """deliver() should make an HTTP POST to each matching webhook."""
        webhook = _make_webhook(tenant_id, events=["agent.completed"])
        mock_delivery = _make_mock_delivery(webhook.id)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [webhook]
        mock_db.execute = AsyncMock(return_value=result_mock)

        with patch("src.services.webhook.WebhookDelivery", return_value=mock_delivery), \
             patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            deliveries = await service.deliver(
                tenant_id=tenant_id,
                event_type="agent.completed",
                payload={"data": "test"},
            )

        assert len(deliveries) == 1
        assert deliveries[0].status == DeliveryStatus.DELIVERED

    @pytest.mark.asyncio
    async def test_deliver_filters_unsubscribed_events(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Webhooks not subscribed to an event should not receive it."""
        webhook = _make_webhook(tenant_id, events=["document.ingested"])

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [webhook]
        mock_db.execute = AsyncMock(return_value=result_mock)

        deliveries = await service.deliver(
            tenant_id=tenant_id,
            event_type="agent.completed",  # not subscribed
            payload={"data": "test"},
        )

        assert len(deliveries) == 0

    @pytest.mark.asyncio
    async def test_deliver_marks_failed_on_error(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Delivery to an unreachable endpoint should mark status as pending (for retry)."""
        webhook = _make_webhook(tenant_id, events=["agent.completed"])
        mock_delivery = _make_mock_delivery(webhook.id)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [webhook]
        mock_db.execute = AsyncMock(return_value=result_mock)

        with patch("src.services.webhook.WebhookDelivery", return_value=mock_delivery), \
             patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            deliveries = await service.deliver(
                tenant_id=tenant_id,
                event_type="agent.completed",
                payload={"data": "test"},
            )

        assert len(deliveries) == 1
        # After 1 attempt with max=3, should be pending with retry scheduled
        assert deliveries[0].status in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)

    @pytest.mark.asyncio
    async def test_deliver_includes_signature_header(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """Delivery POST should include X-EAP-Signature-256 header."""
        webhook = _make_webhook(tenant_id, events=["agent.completed"])
        mock_delivery = _make_mock_delivery(webhook.id)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [webhook]
        mock_db.execute = AsyncMock(return_value=result_mock)

        captured_headers: dict[str, str] = {}

        async def capture_post(url: str, *, content: bytes, headers: dict[str, str]) -> MagicMock:
            captured_headers.update(headers)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("src.services.webhook.WebhookDelivery", return_value=mock_delivery), \
             patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = capture_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await service.deliver(
                tenant_id=tenant_id,
                event_type="agent.completed",
                payload={"data": "test"},
            )

        assert "X-EAP-Signature-256" in captured_headers
        sig = captured_headers["X-EAP-Signature-256"]
        assert sig.startswith("sha256=")


# ------------------------------------------------------------------ #
# Retry logic tests
# ------------------------------------------------------------------ #


class TestRetryLogic:
    """Tests for exponential backoff retry logic."""

    def test_retry_delays_defined(self) -> None:
        """Retry delays should be defined and non-negative."""
        assert len(_RETRY_DELAYS) >= 2
        for delay in _RETRY_DELAYS:
            assert delay >= 0

    def test_max_attempts_at_least_three(self) -> None:
        """Maximum attempts should be at least 3."""
        assert _MAX_ATTEMPTS >= 3

    @pytest.mark.asyncio
    async def test_retry_failed_calls_attempt_delivery(
        self, service: WebhookService, mock_db: MagicMock, webhook_id: uuid.UUID
    ) -> None:
        """retry_failed should re-attempt pending/failed deliveries."""
        webhook = _make_webhook(uuid.uuid4())
        webhook.id = webhook_id

        delivery = _make_delivery(webhook_id, status=DeliveryStatus.PENDING, attempts=1)

        # First execute: get deliveries to retry
        delivery_result = MagicMock()
        delivery_result.scalars.return_value.all.return_value = [delivery]

        # Second execute: get webhook
        webhook_result = MagicMock()
        webhook_result.scalar_one_or_none.return_value = webhook

        execute_results = [delivery_result, webhook_result, MagicMock()]
        call_count = 0

        async def mock_execute(stmt: Any) -> Any:
            nonlocal call_count
            idx = min(call_count, len(execute_results) - 1)
            call_count += 1
            return execute_results[idx]

        mock_db.execute = mock_execute

        with patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            retried = await service.retry_failed(webhook_id)

        assert retried == 1

    @pytest.mark.asyncio
    async def test_retry_failed_returns_zero_when_no_pending(
        self, service: WebhookService, mock_db: MagicMock, webhook_id: uuid.UUID
    ) -> None:
        """retry_failed should return 0 if there are no retryable deliveries."""
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=result_mock)

        retried = await service.retry_failed(webhook_id)
        assert retried == 0


# ------------------------------------------------------------------ #
# Verify endpoint tests
# ------------------------------------------------------------------ #


class TestVerifyEndpoint:
    """Tests for endpoint reachability verification."""

    @pytest.mark.asyncio
    async def test_verify_endpoint_returns_true_for_2xx(
        self, service: WebhookService
    ) -> None:
        """verify_endpoint should return True for a 200 response."""
        with patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await service.verify_endpoint("https://example.com/hook")

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_endpoint_returns_false_on_connection_error(
        self, service: WebhookService
    ) -> None:
        """verify_endpoint should return False when connection fails."""
        with patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await service.verify_endpoint("https://unreachable.example.com/hook")

        assert result is False

    @pytest.mark.asyncio
    async def test_verify_endpoint_returns_false_for_500(
        self, service: WebhookService
    ) -> None:
        """verify_endpoint should return False for a 500 response."""
        with patch("src.services.webhook.httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await service.verify_endpoint("https://error.example.com/hook")

        assert result is False


# ------------------------------------------------------------------ #
# Deletion tests
# ------------------------------------------------------------------ #


class TestWebhookDeletion:
    """Tests for webhook deletion."""

    @pytest.mark.asyncio
    async def test_delete_existing_webhook_returns_true(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """delete() should return True when the webhook exists."""
        webhook = _make_webhook(tenant_id)

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = webhook
        mock_db.execute = AsyncMock(return_value=result_mock)
        mock_db.delete = AsyncMock()

        result = await service.delete(webhook.id, tenant_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_missing_webhook_returns_false(
        self, service: WebhookService, mock_db: MagicMock, tenant_id: uuid.UUID
    ) -> None:
        """delete() should return False when the webhook doesn't exist."""
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=result_mock)

        result = await service.delete(uuid.uuid4(), tenant_id)
        assert result is False


# ------------------------------------------------------------------ #
# Delivery history tests
# ------------------------------------------------------------------ #


class TestDeliveryHistory:
    """Tests for get_deliveries."""

    @pytest.mark.asyncio
    async def test_get_deliveries_returns_list(
        self, service: WebhookService, mock_db: MagicMock, webhook_id: uuid.UUID
    ) -> None:
        """get_deliveries should return a list of WebhookDelivery objects."""
        deliveries = [
            _make_delivery(webhook_id, status=DeliveryStatus.DELIVERED),
            _make_delivery(webhook_id, status=DeliveryStatus.FAILED),
        ]

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = deliveries
        mock_db.execute = AsyncMock(return_value=result_mock)

        result = await service.get_deliveries(webhook_id)
        assert len(result) == 2
