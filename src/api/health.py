"""Health check endpoints.

/health/live   - Liveness probe: is the process up?
/health/ready  - Readiness probe: can we serve traffic? (DB reachable?)

These are public endpoints - no auth required.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import text

from src.database import get_engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict:
    """Liveness probe - always returns 200 if the process is running."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@router.get("/ready")
async def readiness() -> dict:
    """Readiness probe - checks DB connectivity."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    is_ready = db_status == "ok"
    return {
        "status": "ready" if is_ready else "not_ready",
        "database": db_status,
        "timestamp": datetime.now(UTC).isoformat(),
    }
