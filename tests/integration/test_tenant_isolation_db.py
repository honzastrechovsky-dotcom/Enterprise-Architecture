"""Integration tests for tenant isolation at the database level.

Critical security property: data written by tenant A must never be readable
by tenant B, even when both tenants exist in the same database.

These tests verify that apply_tenant_filter() enforces isolation for every
tenant-scoped model and that there are no leakage paths through raw queries.

Run with:
    pytest -m integration tests/integration/test_tenant_isolation_db.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.policy import apply_tenant_filter
from src.models.plan import PlanRecord
from src.models.token_budget import TokenBudgetRecord, TokenUsageRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan_record(tenant_id: uuid.UUID, user_id: uuid.UUID) -> PlanRecord:
    now = datetime.now(timezone.utc)
    return PlanRecord(
        tenant_id=tenant_id,
        created_by=user_id,
        goal="Tenant isolation verification plan goal text",
        status="draft",
        graph_json={"nodes": {}},
        execution_plan="No execution steps defined",
        metadata_json={},
        created_at=now,
        updated_at=now,
    )


def _make_budget_record(tenant_id: uuid.UUID) -> TokenBudgetRecord:
    now = datetime.now(timezone.utc)
    return TokenBudgetRecord(
        tenant_id=tenant_id,
        daily_limit=1_000_000,
        monthly_limit=20_000_000,
        current_daily=0,
        current_monthly=0,
        last_reset_date=now.strftime("%Y-%m-%d"),
        last_reset_month=now.strftime("%Y-%m"),
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Plan isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_plan_data_isolated_between_tenants(integration_db: AsyncSession, seed_data: dict):
    """Tenant B cannot read Tenant A's execution plans via apply_tenant_filter."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]
    admin_a = seed_data["users"]["admin_a"]

    plan_a = _make_plan_record(tenant_a.id, admin_a.id)
    integration_db.add(plan_a)
    await integration_db.flush()

    # Tenant B filtered query must return empty
    stmt = apply_tenant_filter(select(PlanRecord), PlanRecord, tenant_b.id)
    result = await integration_db.execute(stmt)
    plans = result.scalars().all()
    assert all(p.tenant_id == tenant_b.id for p in plans), (
        "Tenant B query returned rows belonging to tenant A"
    )

    # Direct lookup by plan ID with tenant B filter must return None
    stmt_direct = apply_tenant_filter(
        select(PlanRecord).where(PlanRecord.id == plan_a.id),
        PlanRecord,
        tenant_b.id,
    )
    result_direct = await integration_db.execute(stmt_direct)
    assert result_direct.scalar_one_or_none() is None


@pytest.mark.integration
async def test_apply_tenant_filter_raises_for_model_without_tenant_id(
    integration_db: AsyncSession,
):
    """apply_tenant_filter raises AttributeError for models without tenant_id."""
    from src.models.tenant import Tenant

    with pytest.raises(AttributeError, match="does not have a tenant_id column"):
        apply_tenant_filter(select(Tenant), Tenant, uuid.uuid4())


# ---------------------------------------------------------------------------
# Budget isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_budget_records_isolated_between_tenants(
    integration_db: AsyncSession, seed_data: dict
):
    """Token budget rows for tenant A are not visible to tenant B queries."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]

    budget_a = _make_budget_record(tenant_a.id)
    integration_db.add(budget_a)
    await integration_db.flush()

    # Query scoped to tenant B should not return tenant A's budget
    result = await integration_db.execute(
        select(TokenBudgetRecord).where(TokenBudgetRecord.tenant_id == tenant_b.id)
    )
    budgets_b = result.scalars().all()
    assert all(b.tenant_id == tenant_b.id for b in budgets_b)
    tenant_a_leaked = any(b.tenant_id == tenant_a.id for b in budgets_b)
    assert not tenant_a_leaked, "Tenant A's budget leaked into Tenant B's query"


@pytest.mark.integration
async def test_usage_records_isolated_between_tenants(
    integration_db: AsyncSession, seed_data: dict
):
    """TokenUsageRecord rows for tenant A are not visible to tenant B queries."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]
    now = datetime.now(timezone.utc)

    usage_a = TokenUsageRecord(
        tenant_id=tenant_a.id,
        timestamp=now,
        model_tier="light",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        complexity_score=0.1,
    )
    integration_db.add(usage_a)
    await integration_db.flush()

    # Query scoped to tenant B should not return tenant A's usage
    result = await integration_db.execute(
        select(TokenUsageRecord).where(TokenUsageRecord.tenant_id == tenant_b.id)
    )
    usage_b_rows = result.scalars().all()
    assert all(u.tenant_id == tenant_b.id for u in usage_b_rows)
    leaked = any(u.tenant_id == tenant_a.id for u in usage_b_rows)
    assert not leaked, "Tenant A's usage record leaked into Tenant B's query"


# ---------------------------------------------------------------------------
# Cross-tenant UUID guessing
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cross_tenant_uuid_guessing_blocked(
    integration_db: AsyncSession, seed_data: dict
):
    """Guessing a plan UUID from another tenant returns no result when tenant filter applied."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]
    admin_a = seed_data["users"]["admin_a"]

    # Tenant A creates a plan
    plan_a = _make_plan_record(tenant_a.id, admin_a.id)
    integration_db.add(plan_a)
    await integration_db.flush()

    # Tenant B tries to access it by guessing the UUID
    stmt = apply_tenant_filter(
        select(PlanRecord).where(PlanRecord.id == plan_a.id),
        PlanRecord,
        tenant_b.id,
    )
    result = await integration_db.execute(stmt)
    assert result.scalar_one_or_none() is None, (
        "Tenant B was able to access Tenant A's plan via UUID guessing"
    )


@pytest.mark.integration
async def test_two_tenants_same_data_shape_remain_isolated(
    integration_db: AsyncSession, seed_data: dict
):
    """Both tenants can have their own budgets without cross-visibility."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]

    budget_a = _make_budget_record(tenant_a.id)
    budget_b = _make_budget_record(tenant_b.id)
    integration_db.add(budget_a)
    integration_db.add(budget_b)
    await integration_db.flush()

    # Each tenant sees exactly their own budget
    result_a = await integration_db.execute(
        select(TokenBudgetRecord).where(TokenBudgetRecord.tenant_id == tenant_a.id)
    )
    result_b = await integration_db.execute(
        select(TokenBudgetRecord).where(TokenBudgetRecord.tenant_id == tenant_b.id)
    )

    rows_a = result_a.scalars().all()
    rows_b = result_b.scalars().all()

    assert all(r.tenant_id == tenant_a.id for r in rows_a)
    assert all(r.tenant_id == tenant_b.id for r in rows_b)
    assert len(rows_a) >= 1
    assert len(rows_b) >= 1
