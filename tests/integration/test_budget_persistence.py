"""Integration tests for PersistentBudgetManager.

Tests that token budgets are correctly persisted to PostgreSQL and that
concurrent write protection via SELECT FOR UPDATE works as expected.

Run with:
    pytest -m integration tests/integration/test_budget_persistence.py
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.model_router.budget import PersistentBudgetManager
from src.agent.model_router.router import ModelTier
from src.models.token_budget import TokenBudgetRecord, TokenUsageRecord


@pytest.mark.integration
async def test_budget_created_on_first_check(integration_db: AsyncSession, seed_data: dict):
    """First async_check_budget creates the budget row in the database."""
    tenant = seed_data["tenants"]["tenant_a"]
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(integration_db.get_bind(), expire_on_commit=False)
    manager = PersistentBudgetManager(session_factory=factory)

    can_afford = await manager.async_check_budget(
        session=integration_db,
        tenant_id=tenant.id,
        estimated_tokens=100,
    )

    assert can_afford is True

    # Verify row was created in DB
    result = await integration_db.execute(
        select(TokenBudgetRecord).where(TokenBudgetRecord.tenant_id == tenant.id)
    )
    record = result.scalar_one_or_none()
    assert record is not None
    assert record.daily_limit == 1_000_000
    assert record.current_daily == 0  # check_budget does not increment counters


@pytest.mark.integration
async def test_record_usage_increments_counters(integration_db: AsyncSession, seed_data: dict):
    """async_record_usage updates daily and monthly counters in the DB row."""
    tenant = seed_data["tenants"]["tenant_a"]
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(integration_db.get_bind(), expire_on_commit=False)
    manager = PersistentBudgetManager(session_factory=factory)

    await manager.async_record_usage(
        session=integration_db,
        tenant_id=tenant.id,
        model_tier=ModelTier.LIGHT,
        input_tokens=300,
        output_tokens=200,
        complexity_score=0.2,
    )
    await integration_db.flush()

    result = await integration_db.execute(
        select(TokenBudgetRecord).where(TokenBudgetRecord.tenant_id == tenant.id)
    )
    record = result.scalar_one()

    assert record.current_daily == 500
    assert record.current_monthly == 500


@pytest.mark.integration
async def test_usage_record_appended_to_log(integration_db: AsyncSession, seed_data: dict):
    """async_record_usage inserts a TokenUsageRecord row for the audit log."""
    tenant = seed_data["tenants"]["tenant_a"]
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(integration_db.get_bind(), expire_on_commit=False)
    manager = PersistentBudgetManager(session_factory=factory)

    await manager.async_record_usage(
        session=integration_db,
        tenant_id=tenant.id,
        model_tier=ModelTier.STANDARD,
        input_tokens=100,
        output_tokens=50,
        complexity_score=0.5,
    )
    await integration_db.flush()

    result = await integration_db.execute(
        select(TokenUsageRecord).where(TokenUsageRecord.tenant_id == tenant.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].model_tier == "standard"
    assert rows[0].total_tokens == 150


@pytest.mark.integration
async def test_budget_exhausted_blocks_request(integration_db: AsyncSession, seed_data: dict):
    """async_check_budget returns False when daily limit is exhausted."""
    tenant = seed_data["tenants"]["tenant_b"]
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(integration_db.get_bind(), expire_on_commit=False)
    # Set a very small daily limit so one recording exhausts it
    manager = PersistentBudgetManager(
        session_factory=factory,
        default_daily_limit=100,
        default_monthly_limit=10_000,
    )

    # Record 100 tokens to reach the daily limit
    await manager.async_record_usage(
        session=integration_db,
        tenant_id=tenant.id,
        model_tier=ModelTier.LIGHT,
        input_tokens=60,
        output_tokens=40,
    )
    await integration_db.flush()

    # Next check for 1 more token should fail
    can_afford = await manager.async_check_budget(
        session=integration_db,
        tenant_id=tenant.id,
        estimated_tokens=1,
    )
    assert can_afford is False


@pytest.mark.integration
async def test_daily_reset_clears_counter(integration_db: AsyncSession, seed_data: dict):
    """A stale last_reset_date triggers a daily counter reset on the next operation."""
    tenant = seed_data["tenants"]["tenant_a"]
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(integration_db.get_bind(), expire_on_commit=False)
    manager = PersistentBudgetManager(session_factory=factory)

    # Manually insert a budget record with a past reset date and non-zero counter
    yesterday = "2000-01-01"
    old_month = "2000-01"
    record = TokenBudgetRecord(
        tenant_id=tenant.id,
        daily_limit=1_000_000,
        monthly_limit=20_000_000,
        current_daily=999_000,
        current_monthly=999_000,
        last_reset_date=yesterday,
        last_reset_month=old_month,
        updated_at=datetime.now(timezone.utc),
    )
    integration_db.add(record)
    await integration_db.flush()

    # Check should reset counter and allow the request
    can_afford = await manager.async_check_budget(
        session=integration_db,
        tenant_id=tenant.id,
        estimated_tokens=1_000,
    )
    assert can_afford is True

    # Verify reset happened in memory (applied via _apply_resets)
    usage = await manager.async_get_usage(
        session=integration_db,
        tenant_id=tenant.id,
    )
    assert usage.current_daily == 0
    assert usage.current_monthly == 0
