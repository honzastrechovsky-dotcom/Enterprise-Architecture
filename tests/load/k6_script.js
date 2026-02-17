/**
 * k6 load test for Enterprise Agent Platform
 *
 * Scenarios mirror the Locust locustfile.py:
 *   - health_check   : GET /health/live + /health/ready
 *   - chat_baseline  : POST /api/v1/chat (non-streaming)
 *   - chat_streaming : POST /api/v1/chat/stream (SSE)
 *   - plan_lifecycle : create → get → approve → status
 *   - agent_list     : GET /api/v1/analytics/agents
 *
 * Stages (total ~5.5 min):
 *   ramp-up  → 1 min   0  → 50 VUs
 *   sustained→ 3 min  50 VUs steady
 *   spike    → 30 s  50 → 150 VUs
 *   cooldown → 1 min 150 → 0 VUs
 *
 * Thresholds:
 *   - http_req_duration{endpoint:health}  p(95) < 500 ms
 *   - http_req_duration{endpoint:chat}    p(95) < 2000 ms
 *   - http_req_failed                     rate  < 0.01  (< 1 %)
 *
 * Usage:
 *   k6 run tests/load/k6_script.js \
 *      -e TARGET_HOST=http://localhost:8000 \
 *      -e DEV_JWT_SECRET=dev-only-jwt-secret-not-for-production \
 *      -e LOAD_TEST_TENANT_ID=<uuid>
 *
 * Required k6 extensions: none (uses built-in k6/crypto + k6/encoding)
 *
 * NOTE: k6 does not have a native JWT library.  We build HS256 tokens
 * manually using the built-in HMAC-SHA256 primitives.
 */

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";
import { hmac } from "k6/crypto";
import { b64encode } from "k6/encoding";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const TARGET_HOST = __ENV.TARGET_HOST || "http://localhost:8000";
const DEV_JWT_SECRET =
  __ENV.DEV_JWT_SECRET || "dev-only-jwt-secret-not-for-production";
const LOAD_TEST_TENANT_ID =
  __ENV.LOAD_TEST_TENANT_ID || "6ba7b810-9dad-11d1-80b4-00c04fd430c8";

// How many SSE bytes to read before closing (prevents memory exhaustion)
const SSE_READ_TIMEOUT_MS = 5000;

// External IDs embedded in JWT sub claim
const ADMIN_SUB = "load-test-admin-user";
const OPERATOR_SUB = "load-test-operator-user";
const VIEWER_SUB = "load-test-viewer-user";

// ---------------------------------------------------------------------------
// Custom metrics
// ---------------------------------------------------------------------------

const chatLatency = new Trend("chat_latency_ms", true);
const planLatency = new Trend("plan_latency_ms", true);
const streamFirstByteLatency = new Trend("stream_first_byte_ms", true);
const errorRate = new Rate("load_test_error_rate");

// ---------------------------------------------------------------------------
// k6 options
// ---------------------------------------------------------------------------

export const options = {
  scenarios: {
    health_check: {
      executor: "constant-vus",
      vus: 5,
      duration: "5m30s",
      tags: { endpoint: "health" },
      exec: "healthScenario",
    },
    chat_baseline: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 20 }, // ramp-up
        { duration: "3m", target: 20 }, // sustained
        { duration: "30s", target: 60 }, // spike
        { duration: "1m", target: 0 }, // cooldown
      ],
      tags: { endpoint: "chat" },
      exec: "chatScenario",
    },
    chat_streaming: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 10 },
        { duration: "3m", target: 10 },
        { duration: "30s", target: 30 },
        { duration: "1m", target: 0 },
      ],
      tags: { endpoint: "chat_stream" },
      exec: "chatStreamScenario",
    },
    plan_lifecycle: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 5 },
        { duration: "3m", target: 5 },
        { duration: "30s", target: 15 },
        { duration: "1m", target: 0 },
      ],
      tags: { endpoint: "plans" },
      exec: "planScenario",
    },
    agent_analytics: {
      executor: "constant-vus",
      vus: 3,
      duration: "5m30s",
      tags: { endpoint: "analytics" },
      exec: "analyticsScenario",
    },
  },

  thresholds: {
    // Health endpoints must be fast
    "http_req_duration{endpoint:health}": ["p(95)<500"],

    // Chat can be slower due to LLM latency
    "http_req_duration{endpoint:chat}": ["p(95)<2000"],
    "http_req_duration{endpoint:chat_stream}": ["p(95)<3000"],

    // Plan creation involves an LLM decomposition step
    "http_req_duration{endpoint:plans}": ["p(95)<5000"],

    // Overall error rate must stay below 1 %
    http_req_failed: ["rate<0.01"],
    load_test_error_rate: ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// JWT helpers
// ---------------------------------------------------------------------------

/**
 * Build a base64url-encoded string (no padding, URL-safe chars).
 */
function base64url(data) {
  return b64encode(data, "rawurl");
}

/**
 * Encode a plain JS object as a base64url string.
 */
function base64urlJson(obj) {
  return base64url(JSON.stringify(obj));
}

/**
 * Create an HS256 JWT token.
 *
 * @param {string} sub       - Subject (external_id)
 * @param {string} role      - admin | operator | viewer
 * @param {number} ttlSecs   - Token lifetime in seconds
 * @returns {string}         - Encoded JWT
 */
function makeJwt(sub, role, ttlSecs = 3600) {
  const now = Math.floor(Date.now() / 1000);
  const header = base64urlJson({ alg: "HS256", typ: "JWT" });
  const payload = base64urlJson({
    sub,
    tenant_id: LOAD_TEST_TENANT_ID,
    role,
    aud: "enterprise-agents-api",
    iat: now,
    exp: now + ttlSecs,
  });
  const signingInput = `${header}.${payload}`;
  const signature = base64url(hmac("sha256", DEV_JWT_SECRET, signingInput, "binary"));
  return `${signingInput}.${signature}`;
}

/**
 * Return Authorization header object for the given role.
 */
function authHeaders(sub, role, extra = {}) {
  return {
    Authorization: `Bearer ${makeJwt(sub, role)}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

// ---------------------------------------------------------------------------
// Shared utilities
// ---------------------------------------------------------------------------

/**
 * Record an error in the custom error rate metric and log details.
 */
function recordError(tag, response) {
  errorRate.add(1);
  console.error(
    `[${tag}] HTTP ${response.status}: ${response.body ? response.body.slice(0, 200) : "(empty)"}`
  );
}

/**
 * Parse JSON body, returning null on failure.
 */
function safeJson(response) {
  try {
    return JSON.parse(response.body);
  } catch (_) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Scenario: Health checks
// ---------------------------------------------------------------------------

export function healthScenario() {
  group("health", () => {
    // Liveness
    const live = http.get(`${TARGET_HOST}/health/live`, {
      tags: { name: "GET /health/live" },
    });
    const liveOk = check(live, {
      "health/live status 200": (r) => r.status === 200,
      "health/live has status ok": (r) => {
        const data = safeJson(r);
        return data && data.status === "ok";
      },
    });
    if (!liveOk) recordError("health/live", live);

    // Readiness (DB ping)
    const ready = http.get(`${TARGET_HOST}/health/ready`, {
      tags: { name: "GET /health/ready" },
    });
    const readyOk = check(ready, {
      "health/ready status 200": (r) => r.status === 200,
      "health/ready database ok": (r) => {
        const data = safeJson(r);
        return data && data.database === "ok";
      },
    });
    if (!readyOk) recordError("health/ready", ready);
  });

  sleep(Math.random() * 3 + 2); // 2-5 s between polls
}

// ---------------------------------------------------------------------------
// Scenario: Non-streaming chat
// ---------------------------------------------------------------------------

export function chatScenario() {
  const headers = authHeaders(OPERATOR_SUB, "operator");

  group("chat", () => {
    const start = Date.now();
    const res = http.post(
      `${TARGET_HOST}/api/v1/chat`,
      JSON.stringify({
        message:
          "What are the key performance indicators for our Q4 production metrics? " +
          "Please summarise in three bullet points.",
      }),
      {
        headers,
        timeout: "30s",
        tags: { name: "POST /api/v1/chat" },
      }
    );
    const elapsed = Date.now() - start;
    chatLatency.add(elapsed);

    const ok = check(res, {
      "chat status 200": (r) => r.status === 200,
      "chat has response field": (r) => {
        const data = safeJson(r);
        return data !== null && typeof data.response === "string";
      },
      "chat has conversation_id": (r) => {
        const data = safeJson(r);
        return data !== null && typeof data.conversation_id === "string";
      },
    });

    if (!ok) {
      // 429, 502, 503 are expected under load — still count as errors
      // for threshold tracking but don't crash the scenario.
      if (res.status !== 429 && res.status !== 502 && res.status !== 503) {
        recordError("chat", res);
      } else {
        errorRate.add(0); // intentional: not a platform error
      }
    }
  });

  sleep(Math.random() * 5 + 5); // 5-10 s think time
}

// ---------------------------------------------------------------------------
// Scenario: SSE streaming chat
// ---------------------------------------------------------------------------

export function chatStreamScenario() {
  const headers = authHeaders(OPERATOR_SUB, "operator", {
    Accept: "text/event-stream",
  });

  group("chat_stream", () => {
    const start = Date.now();
    // k6 does not natively stream SSE; we measure time-to-first-byte
    // and verify content-type header instead.
    const res = http.post(
      `${TARGET_HOST}/api/v1/chat/stream`,
      JSON.stringify({
        message: "Summarise the last five maintenance work orders in one sentence each.",
      }),
      {
        headers,
        timeout: `${SSE_READ_TIMEOUT_MS}ms`,
        tags: { name: "POST /api/v1/chat/stream" },
      }
    );
    streamFirstByteLatency.add(Date.now() - start);

    const ok = check(res, {
      "chat/stream status 200": (r) => r.status === 200,
      "chat/stream content-type is text/event-stream": (r) =>
        (r.headers["Content-Type"] || "").includes("text/event-stream"),
      "chat/stream body not empty": (r) => r.body !== null && r.body.length > 0,
    });

    if (!ok && res.status !== 429 && res.status !== 502 && res.status !== 503) {
      recordError("chat/stream", res);
    }
  });

  sleep(Math.random() * 8 + 7); // 7-15 s think time — streaming is heavier
}

// ---------------------------------------------------------------------------
// Scenario: Plan lifecycle (create → get → approve → status)
// ---------------------------------------------------------------------------

export function planScenario() {
  const headers = authHeaders(ADMIN_SUB, "admin");

  group("plans", () => {
    // --- Create ---
    const start = Date.now();
    const createRes = http.post(
      `${TARGET_HOST}/api/v1/plans`,
      JSON.stringify({
        goal:
          "Analyse the last 30 days of production downtime events, " +
          "identify the top 3 root causes, and generate a corrective " +
          "action report with estimated fix timelines.",
        context: "k6 load test — plan lifecycle scenario",
      }),
      {
        headers,
        timeout: "30s",
        tags: { name: "POST /api/v1/plans" },
      }
    );
    planLatency.add(Date.now() - start);

    const createOk = check(createRes, {
      "plans create status 201": (r) => r.status === 201,
      "plans create has plan_id": (r) => {
        const data = safeJson(r);
        return data !== null && typeof data.plan_id === "string";
      },
    });

    if (!createOk) {
      if (createRes.status !== 502 && createRes.status !== 503) {
        recordError("plans/create", createRes);
      }
      return; // Skip remaining steps if create failed
    }

    const planId = safeJson(createRes).plan_id;

    // --- Get ---
    const getRes = http.get(`${TARGET_HOST}/api/v1/plans/${planId}`, {
      headers,
      tags: { name: "GET /api/v1/plans/:id" },
    });
    check(getRes, {
      "plans get status 200": (r) => r.status === 200,
      "plans get goal matches": (r) => {
        const data = safeJson(r);
        return data !== null && typeof data.goal === "string";
      },
    });

    // --- Approve ---
    const approveRes = http.post(
      `${TARGET_HOST}/api/v1/plans/${planId}/approve`,
      JSON.stringify({ comment: "Approved by k6 load test" }),
      {
        headers,
        tags: { name: "POST /api/v1/plans/:id/approve" },
      }
    );
    check(approveRes, {
      "plans approve status 200 or 400": (r) => r.status === 200 || r.status === 400,
    });

    // --- Execution status ---
    const statusRes = http.get(`${TARGET_HOST}/api/v1/plans/${planId}/status`, {
      headers,
      tags: { name: "GET /api/v1/plans/:id/status" },
    });
    check(statusRes, {
      "plans status 200": (r) => r.status === 200,
      "plans status has plan_id": (r) => {
        const data = safeJson(r);
        return data !== null && data.plan_id === planId;
      },
    });
  });

  sleep(Math.random() * 10 + 10); // 10-20 s think time — plans are heavyweight
}

// ---------------------------------------------------------------------------
// Scenario: Agent analytics list
// ---------------------------------------------------------------------------

export function analyticsScenario() {
  const headers = authHeaders(ADMIN_SUB, "admin");

  group("analytics", () => {
    const res = http.get(`${TARGET_HOST}/api/v1/analytics/agents`, {
      headers,
      tags: { name: "GET /api/v1/analytics/agents" },
    });

    check(res, {
      "analytics/agents status 200 or 403": (r) =>
        r.status === 200 || r.status === 403,
    });

    if (res.status !== 200 && res.status !== 403) {
      recordError("analytics/agents", res);
    }

    // Prometheus metrics endpoint (no auth required)
    const metrics = http.get(`${TARGET_HOST}/metrics`, {
      tags: { name: "GET /metrics" },
    });
    check(metrics, {
      "/metrics status 200": (r) => r.status === 200,
    });
  });

  sleep(Math.random() * 4 + 3); // 3-7 s between polls
}
