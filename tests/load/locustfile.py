"""Locust load test for the Enterprise Agent Platform.

User behaviour classes:
- CasualUser   (weight=3): health checks + read-only agent/conversation lookups
- PowerUser    (weight=1): full chat conversation + plan CRUD lifecycle

Run:
    locust -f tests/load/locustfile.py \
           --host http://localhost:8000 \
           --users 50 --spawn-rate 5 \
           --run-time 5m

Environment variables (all optional):
    TARGET_HOST          base URL of the platform  (default: http://localhost:8000)
    DEV_JWT_SECRET       symmetric HS256 secret    (default: dev-only-jwt-secret-not-for-production)
    LOAD_TEST_TENANT_ID  UUID of test tenant       (default: auto-generated stable UUID)
"""

from __future__ import annotations

import os
import time
import uuid

import jwt
from locust import HttpUser, between, events, tag, task
from locust.exception import RescheduleTask

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEV_JWT_SECRET: str = os.getenv(
    "DEV_JWT_SECRET",
    "dev-only-jwt-secret-not-for-production",
)
LOAD_TEST_TENANT_ID: str = os.getenv(
    "LOAD_TEST_TENANT_ID",
    # Stable, deterministic UUID derived from a known namespace so every
    # worker targets the same tenant without pre-setup.
    str(uuid.uuid5(uuid.NAMESPACE_DNS, "load-test.enterprise-agents.local")),
)

# Role-specific user IDs — these are the external_id claim in the JWT.
# In a real deployment, these accounts should exist in the DB beforehand.
ADMIN_EXTERNAL_ID = "load-test-admin-user"
OPERATOR_EXTERNAL_ID = "load-test-operator-user"
VIEWER_EXTERNAL_ID = "load-test-viewer-user"

# SSE byte budget: read at most this many bytes from a streaming response
# before closing to avoid memory exhaustion.
SSE_MAX_BYTES = 8_192  # 8 KB


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_jwt(external_id: str, role: str, ttl_seconds: int = 3600) -> str:
    """Generate a short-lived HS256 JWT that the dev server will accept.

    Claims required by src/auth/oidc.py:
        sub, tenant_id, role, exp, aud
    """
    now = int(time.time())
    payload = {
        "sub": external_id,
        "tenant_id": LOAD_TEST_TENANT_ID,
        "role": role,
        "aud": "enterprise-agents-api",
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, DEV_JWT_SECRET, algorithm="HS256")


def _auth_headers(external_id: str, role: str) -> dict[str, str]:
    token = _make_jwt(external_id, role)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Locust event hooks
# ---------------------------------------------------------------------------


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Log configuration at test start so operators can verify settings."""
    print(
        f"\n[load-test] tenant={LOAD_TEST_TENANT_ID} "
        f"secret={'***' if DEV_JWT_SECRET else 'NOT SET'}\n"
    )


# ---------------------------------------------------------------------------
# Shared task mix-ins
# ---------------------------------------------------------------------------


class HealthMixin:
    """Tasks shared across all user classes."""

    @tag("health")
    @task(5)
    def health_live(self):
        """GET /health/live — no auth required, should be sub-50 ms."""
        with self.client.get(
            "/health/live",
            name="/health/live",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"health/live returned {resp.status_code}")

    @tag("health")
    @task(2)
    def health_ready(self):
        """GET /health/ready — lightweight DB ping."""
        with self.client.get(
            "/health/ready",
            name="/health/ready",
            catch_response=True,
        ) as resp:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and data.get("status") == "ready":
                resp.success()
            elif resp.status_code == 200:
                # Not-ready is not an HTTP error but we still want to track it
                resp.failure(f"health/ready status={data.get('status')}")
            else:
                resp.failure(f"health/ready HTTP {resp.status_code}")


class MetricsMixin:
    """Task for the Prometheus /metrics endpoint."""

    @tag("metrics")
    @task(1)
    def prometheus_metrics(self):
        """GET /metrics — Prometheus scrape target."""
        with self.client.get(
            "/metrics",
            name="/metrics",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 204):
                resp.success()
            else:
                resp.failure(f"/metrics returned {resp.status_code}")


# ---------------------------------------------------------------------------
# Casual User
# ---------------------------------------------------------------------------


class CasualUser(HealthMixin, MetricsMixin, HttpUser):
    """Represents a passive observer / dashboard poller.

    Weight 3 — most common user type in production.
    Tasks: health checks, metrics scrape, conversation list, analytics.
    """

    weight = 3
    wait_time = between(2, 6)

    def on_start(self):
        self._headers = _auth_headers(VIEWER_EXTERNAL_ID, "viewer")
        # Track conversation IDs discovered during the session
        self._conversation_ids: list[str] = []

    # Health tasks inherited from HealthMixin (weight 5+2=7 in that mixin)

    @tag("conversations")
    @task(3)
    def list_conversations(self):
        """GET /api/v1/conversations — paginated conversation history."""
        with self.client.get(
            "/api/v1/conversations",
            headers=self._headers,
            params={"limit": 10, "offset": 0},
            name="/api/v1/conversations (list)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                # Harvest IDs for use in get_conversation
                items = data if isinstance(data, list) else data.get("items", [])
                for item in items[:5]:
                    cid = item.get("id") or item.get("conversation_id")
                    if cid and cid not in self._conversation_ids:
                        self._conversation_ids.append(cid)
                resp.success()
            elif resp.status_code == 401:
                resp.failure("Unauthorized — check DEV_JWT_SECRET")
                raise RescheduleTask()
            else:
                resp.failure(f"conversations list HTTP {resp.status_code}")

    @tag("conversations")
    @task(2)
    def get_single_conversation(self):
        """GET /api/v1/conversations/{id} — fetch specific conversation."""
        if not self._conversation_ids:
            raise RescheduleTask()
        cid = self._conversation_ids[0]
        with self.client.get(
            f"/api/v1/conversations/{cid}",
            headers=self._headers,
            name="/api/v1/conversations/:id",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            else:
                resp.failure(f"conversation get HTTP {resp.status_code}")

    @tag("analytics")
    @task(1)
    def analytics_agents(self):
        """GET /api/v1/analytics/agents — agent performance stats."""
        with self.client.get(
            "/api/v1/analytics/agents",
            headers=self._headers,
            name="/api/v1/analytics/agents",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 403):
                # 403 expected if viewer lacks analytics permission
                resp.success()
            else:
                resp.failure(f"analytics/agents HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# Power User
# ---------------------------------------------------------------------------


class PowerUser(HealthMixin, HttpUser):
    """Represents a product user actively chatting and managing plans.

    Weight 1 — less common but resource-intensive.
    Tasks: full chat, SSE streaming chat, full plan CRUD lifecycle.
    """

    weight = 1
    wait_time = between(5, 15)

    def on_start(self):
        self._operator_headers = _auth_headers(OPERATOR_EXTERNAL_ID, "operator")
        self._admin_headers = _auth_headers(ADMIN_EXTERNAL_ID, "admin")
        self._conversation_id: str | None = None
        self._plan_id: str | None = None

    # ------------------------------------------------------------------ #
    # Chat tasks
    # ------------------------------------------------------------------ #

    @tag("chat")
    @task(4)
    def chat_message(self):
        """POST /api/v1/chat — non-streaming chat round-trip."""
        body = {
            "message": (
                "What are the key performance indicators for our Q4 production metrics? "
                "Please summarise in three bullet points."
            ),
        }
        if self._conversation_id:
            body["conversation_id"] = self._conversation_id

        with self.client.post(
            "/api/v1/chat",
            json=body,
            headers=self._operator_headers,
            name="/api/v1/chat",
            catch_response=True,
            # Chat may be slow — allow up to 30 s before failure
            timeout=30,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                self._conversation_id = str(data.get("conversation_id", ""))
                resp.success()
            elif resp.status_code == 429:
                resp.failure("Rate limited")
                raise RescheduleTask()
            elif resp.status_code in (502, 503):
                # LLM backend unavailable — not a platform error
                resp.failure(f"LLM unavailable: {resp.status_code}")
                raise RescheduleTask()
            else:
                resp.failure(f"chat POST HTTP {resp.status_code}: {resp.text[:200]}")

    @tag("chat", "sse")
    @task(2)
    def chat_stream(self):
        """POST /api/v1/chat/stream — SSE streaming chat.

        We read the first SSE_MAX_BYTES of the response and then close,
        which is sufficient to verify the stream starts correctly without
        exhausting memory during high-concurrency tests.
        """
        body = {
            "message": "Summarise the last five maintenance work orders in one sentence each.",
        }
        if self._conversation_id:
            body["conversation_id"] = self._conversation_id

        with self.client.post(
            "/api/v1/chat/stream",
            json=body,
            headers={
                **self._operator_headers,
                "Accept": "text/event-stream",
            },
            name="/api/v1/chat/stream",
            catch_response=True,
            stream=True,
            timeout=30,
        ) as resp:
            if resp.status_code == 200:
                # Read a bounded amount to confirm stream is flowing
                received = 0
                for chunk in resp.iter_content(chunk_size=512):
                    received += len(chunk)
                    if received >= SSE_MAX_BYTES:
                        break
                resp.success()
            elif resp.status_code in (429, 502, 503):
                resp.failure(f"chat/stream unavailable: {resp.status_code}")
                raise RescheduleTask()
            else:
                resp.failure(f"chat/stream HTTP {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------ #
    # Plan CRUD lifecycle
    # ------------------------------------------------------------------ #

    @tag("plans")
    @task(2)
    def plan_lifecycle(self):
        """Full plan lifecycle: create -> get -> approve -> get status.

        Uses admin role so it can both create (operator+) and approve (engineer+).
        """
        # 1. Create plan
        plan_id = self._create_plan()
        if plan_id is None:
            return

        # 2. Get plan details
        self._get_plan(plan_id)

        # 3. Approve the plan
        self._approve_plan(plan_id)

        # 4. Check execution status
        self._get_plan_status(plan_id)

        # Cache for follow-up reads
        self._plan_id = plan_id

    def _create_plan(self) -> str | None:
        with self.client.post(
            "/api/v1/plans",
            json={
                "goal": (
                    "Analyse the last 30 days of production downtime events, "
                    "identify the top 3 root causes, and generate a corrective "
                    "action report with estimated fix timelines."
                ),
                "context": "Load test plan — created by power-user scenario",
            },
            headers=self._admin_headers,
            name="/api/v1/plans (create)",
            catch_response=True,
            timeout=30,
        ) as resp:
            if resp.status_code == 201:
                data = resp.json()
                plan_id = data.get("plan_id")
                resp.success()
                return plan_id
            elif resp.status_code == 403:
                resp.failure("Insufficient permissions for plan creation")
            elif resp.status_code in (502, 503):
                resp.failure(f"Plan creation LLM error: {resp.status_code}")
                raise RescheduleTask()
            else:
                resp.failure(f"plans POST HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def _get_plan(self, plan_id: str) -> None:
        with self.client.get(
            f"/api/v1/plans/{plan_id}",
            headers=self._admin_headers,
            name="/api/v1/plans/:id",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"plans GET HTTP {resp.status_code}")

    def _approve_plan(self, plan_id: str) -> None:
        with self.client.post(
            f"/api/v1/plans/{plan_id}/approve",
            json={"comment": "Approved by load-test power user"},
            headers=self._admin_headers,
            name="/api/v1/plans/:id/approve",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 400):
                # 400 if already approved — still valid for load test
                resp.success()
            else:
                resp.failure(f"plans approve HTTP {resp.status_code}")

    def _get_plan_status(self, plan_id: str) -> None:
        with self.client.get(
            f"/api/v1/plans/{plan_id}/status",
            headers=self._admin_headers,
            name="/api/v1/plans/:id/status",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"plans status HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    # Admin tasks (background mix)
    # ------------------------------------------------------------------ #

    @tag("admin")
    @task(1)
    def list_users(self):
        """GET /api/v1/admin/users — tenant user roster."""
        with self.client.get(
            "/api/v1/admin/users",
            headers=self._admin_headers,
            name="/api/v1/admin/users",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 403):
                resp.success()
            else:
                resp.failure(f"admin/users HTTP {resp.status_code}")
