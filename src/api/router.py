"""Main API router - aggregates all sub-routers.

All routes are versioned under /api/v1 except health checks.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api import (
    admin,
    analytics,
    cache,
    chat,
    compliance,
    compliance_admin,
    conversations,
    documents,
    feedback,
    goals,
    health,
    ingestion,
    keys,
    memory,
    multimodal,
    plans,
    playground,
    plugins,
    sso,
    tenant_admin,
    webhooks,
)
from src.api.routes import operations, spaces

# Public router (no auth required)
public_router = APIRouter()
public_router.include_router(health.router)

# Versioned API router
api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(chat.router)
# Ingestion router must be included BEFORE the legacy documents router
# so that /documents/upload and /documents/jobs routes take precedence.
api_v1_router.include_router(ingestion.router)
api_v1_router.include_router(documents.router)
api_v1_router.include_router(conversations.router)
api_v1_router.include_router(admin.router)
api_v1_router.include_router(plans.router)
api_v1_router.include_router(operations.router)
api_v1_router.include_router(spaces.router)
api_v1_router.include_router(compliance.router)
api_v1_router.include_router(playground.router)
api_v1_router.include_router(feedback.router)
api_v1_router.include_router(plugins.router)
api_v1_router.include_router(analytics.router)
api_v1_router.include_router(keys.router)
api_v1_router.include_router(memory.router)
api_v1_router.include_router(cache.router)
api_v1_router.include_router(tenant_admin.router)
api_v1_router.include_router(compliance_admin.router)
api_v1_router.include_router(multimodal.router)
api_v1_router.include_router(sso.router)
api_v1_router.include_router(webhooks.router)
api_v1_router.include_router(goals.router)
