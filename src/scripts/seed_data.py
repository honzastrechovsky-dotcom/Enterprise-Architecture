"""Seed test data for development.

Creates:
- 2 tenants (acme-corp, beta-inc)
- 1 admin + 1 viewer per tenant
- Prints JWT tokens for each user for API testing

Usage:
    python -m src.scripts.seed_data
    # Outputs tokens you can use with curl or Swagger UI
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

log = structlog.get_logger(__name__)


async def seed() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import src.models  # noqa: F401
    from src.auth.oidc import create_dev_token
    from src.config import get_settings
    from src.database import get_engine
    from src.database import init_db as _init_engine
    from src.models.tenant import Tenant
    from src.models.user import User, UserRole

    settings = get_settings()
    _init_engine(settings)
    engine = get_engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    tenants_data = [
        {"name": "Acme Corp", "slug": "acme-corp"},
        {"name": "Beta Inc", "slug": "beta-inc"},
    ]
    secret = settings.dev_jwt_secret.get_secret_value()

    async with session_factory() as db:
        created_tenants = []
        for t_data in tenants_data:
            tenant = Tenant(name=t_data["name"], slug=t_data["slug"])
            db.add(tenant)
            await db.flush()
            created_tenants.append(tenant)
            log.info("seed.tenant_created", slug=t_data["slug"], id=str(tenant.id))

        tokens: list[dict] = []
        for tenant in created_tenants:
            for role in [UserRole.ADMIN, UserRole.VIEWER]:
                sub = str(uuid.uuid4())
                user = User(
                    tenant_id=tenant.id,
                    external_id=sub,
                    email=f"{role.value}@{tenant.slug}.example.com",
                    display_name=f"{tenant.name} {role.value.title()}",
                    role=role,
                )
                db.add(user)
                await db.flush()

                token = create_dev_token(
                    sub=sub,
                    tenant_id=str(tenant.id),
                    role=role.value,
                    email=user.email,
                    secret=secret,
                    expires_in=86400,  # 24 hours
                )
                tokens.append({
                    "tenant": tenant.slug,
                    "role": role.value,
                    "email": user.email,
                    "token": token,
                })
                log.info(
                    "seed.user_created",
                    email=user.email,
                    tenant=tenant.slug,
                    role=role.value,
                )

        await db.commit()

    print("\n" + "=" * 70)
    print("SEED DATA CREATED - Development tokens:")
    print("=" * 70)
    for t in tokens:
        print(f"\nTenant: {t['tenant']}  Role: {t['role']}  Email: {t['email']}")
        print(f"Token: {t['token'][:80]}...")
        print(f"\ncurl -H 'Authorization: Bearer {t['token'][:40]}...' http://localhost:8000/api/v1/chat")
    print("\n" + "=" * 70)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
