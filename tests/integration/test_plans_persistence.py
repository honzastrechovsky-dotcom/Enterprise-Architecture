"""Integration tests for execution plan persistence.

Verifies that PlanRecord rows are correctly stored, status transitions are
persisted, and that tenant isolation prevents cross-tenant access.

Run with:
    pytest -m integration tests/integration/test_plans_persistence.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.policy import apply_tenant_filter
from src.models.plan import PlanRecord


def _make_plan(tenant_id: uuid.UUID, user_id: uuid.UUID, goal: str = "Test goal for the system") -> PlanRecord:
    """Helper to create a minimal PlanRecord without going through the API."""
    now = datetime.now(timezone.utc)
    return PlanRecord(
        tenant_id=tenant_id,
        created_by=user_id,
        goal=goal,
        status="draft",
        graph_json={"nodes": {}},
        execution_plan="Step 1: do something",
        metadata_json={"context": None},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.integration
async def test_plan_persisted_to_db(integration_db: AsyncSession, seed_data: dict):
    """Creating a PlanRecord and flushing stores it in the database."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    admin_a = seed_data["users"]["admin_a"]

    plan = _make_plan(tenant_a.id, admin_a.id, goal="Deploy SAP batch job to production cluster")
    integration_db.add(plan)
    await integration_db.flush()

    assert plan.id is not None

    # Fetch it back with a fresh query
    result = await integration_db.execute(
        select(PlanRecord).where(PlanRecord.id == plan.id)
    )
    fetched = result.scalar_one()
    assert fetched.goal == "Deploy SAP batch job to production cluster"
    assert fetched.status == "draft"
    assert fetched.tenant_id == tenant_a.id


@pytest.mark.integration
async def test_approve_plan_updates_status(integration_db: AsyncSession, seed_data: dict):
    """Approving a plan changes its status to 'approved' and persists the approver."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    admin_a = seed_data["users"]["admin_a"]

    plan = _make_plan(tenant_a.id, admin_a.id, goal="Execute quarterly MES report generation")
    integration_db.add(plan)
    await integration_db.flush()

    now = datetime.now(timezone.utc)
    plan.status = "approved"
    plan.approved_by = admin_a.id
    plan.approved_at = now
    plan.updated_at = now
    await integration_db.flush()

    result = await integration_db.execute(
        select(PlanRecord).where(PlanRecord.id == plan.id)
    )
    fetched = result.scalar_one()
    assert fetched.status == "approved"
    assert fetched.approved_by == admin_a.id
    assert fetched.approved_at is not None


@pytest.mark.integration
async def test_reject_plan_updates_status(integration_db: AsyncSession, seed_data: dict):
    """Rejecting a plan changes its status to 'rejected' and persists the rejector."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    admin_a = seed_data["users"]["admin_a"]

    plan = _make_plan(tenant_a.id, admin_a.id, goal="Trigger emergency system shutdown sequence")
    integration_db.add(plan)
    await integration_db.flush()

    now = datetime.now(timezone.utc)
    plan.status = "rejected"
    plan.rejected_by = admin_a.id
    plan.rejected_at = now
    plan.updated_at = now
    await integration_db.flush()

    result = await integration_db.execute(
        select(PlanRecord).where(PlanRecord.id == plan.id)
    )
    fetched = result.scalar_one()
    assert fetched.status == "rejected"
    assert fetched.rejected_by == admin_a.id
    assert fetched.rejected_at is not None


@pytest.mark.integration
async def test_tenant_a_plan_invisible_to_tenant_b(integration_db: AsyncSession, seed_data: dict):
    """A plan created in tenant A is not visible when querying as tenant B."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]
    admin_a = seed_data["users"]["admin_a"]

    plan = _make_plan(tenant_a.id, admin_a.id, goal="Run automated quality inspection pipeline")
    integration_db.add(plan)
    await integration_db.flush()

    # Query for tenant B — should return nothing
    stmt = apply_tenant_filter(
        select(PlanRecord).where(PlanRecord.id == plan.id),
        PlanRecord,
        tenant_b.id,
    )
    result = await integration_db.execute(stmt)
    fetched = result.scalar_one_or_none()
    assert fetched is None, "Tenant B must not see Tenant A's plan"


@pytest.mark.integration
async def test_list_plans_returns_only_own_tenant(integration_db: AsyncSession, seed_data: dict):
    """Listing plans with tenant filter returns only that tenant's plans."""
    tenant_a = seed_data["tenants"]["tenant_a"]
    tenant_b = seed_data["tenants"]["tenant_b"]
    admin_a = seed_data["users"]["admin_a"]
    admin_b = seed_data["users"]["admin_b"]

    # Create one plan per tenant
    plan_a = _make_plan(tenant_a.id, admin_a.id, goal="Analyse production defect metrics report")
    plan_b = _make_plan(tenant_b.id, admin_b.id, goal="Generate supplier compliance audit trail")
    integration_db.add(plan_a)
    integration_db.add(plan_b)
    await integration_db.flush()

    stmt_a = apply_tenant_filter(select(PlanRecord), PlanRecord, tenant_a.id)
    result_a = await integration_db.execute(stmt_a)
    plans_a = result_a.scalars().all()

    # Only tenant A's plan should appear
    plan_ids_a = {p.id for p in plans_a}
    assert plan_a.id in plan_ids_a
    assert plan_b.id not in plan_ids_a
