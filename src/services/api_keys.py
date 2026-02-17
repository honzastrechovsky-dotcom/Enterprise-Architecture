"""API Key service - business logic for API key management.

Handles creation, validation, rotation, and revocation of API keys.

Security:
- Raw API keys are only returned once at creation/rotation
- Only SHA-256 hashes are stored in the database
- Keys use secure random generation (secrets module)
- Never log or expose raw keys after creation
"""

from __future__ import annotations

import hashlib
import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.api_key import APIKey

log = structlog.get_logger(__name__)

# Base62 alphabet for key generation (alphanumeric, case-sensitive)
_BASE62_ALPHABET = string.ascii_letters + string.digits


class InvalidAPIKeyError(Exception):
    """Raised when an API key is invalid, expired, or revoked."""

    pass


class APIKeyService:
    """Service for API key operations."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_key(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        scopes: list[str],
        created_by: uuid.UUID,
        description: str | None = None,
        expires_in_days: int | None = None,
        rate_limit_per_minute: int | None = None,
    ) -> tuple[APIKey, str]:
        """Create a new API key.

        Args:
            tenant_id: Tenant this key belongs to
            name: Human-readable name for the key
            scopes: List of allowed scopes (e.g., ['chat', 'documents'])
            created_by: User ID who created this key
            description: Optional description
            expires_in_days: Days until expiration (None = never expires)
            rate_limit_per_minute: Rate limit for this key (None = no limit)

        Returns:
            Tuple of (APIKey model, raw_key string)
            WARNING: The raw key is only returned here and never stored!

        Security:
            - Raw key is generated with cryptographically secure randomness
            - Only SHA-256 hash is stored in database
            - Key format: eap_ + 48 base62 characters
        """
        # Generate secure random key
        raw_key = self._generate_key()

        # Hash the key for storage
        key_hash = self._hash_key(raw_key)

        # Extract prefix for identification
        key_prefix = raw_key[:8]

        # Calculate expiration if specified
        expires_at = None
        if expires_in_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

        # Create API key record
        api_key = APIKey(
            tenant_id=tenant_id,
            name=name,
            description=description,
            key_hash=key_hash,
            key_prefix=key_prefix,
            scopes=scopes,
            rate_limit_per_minute=rate_limit_per_minute,
            created_by=created_by,
            expires_at=expires_at,
        )

        self._db.add(api_key)
        await self._db.flush()

        log.info(
            "api_key.created",
            key_id=str(api_key.id),
            tenant_id=str(tenant_id),
            name=name,
            scopes=scopes,
            prefix=key_prefix,
            expires_at=expires_at,
        )

        # Return both the model and raw key
        # WARNING: This is the ONLY time the raw key is available!
        return api_key, raw_key

    async def validate_key(self, raw_key: str) -> APIKey:
        """Validate an API key and return the associated model.

        Args:
            raw_key: The raw API key to validate

        Returns:
            APIKey model if valid

        Raises:
            InvalidAPIKeyError: If key is invalid, expired, or revoked

        Security:
            - Validates format before DB lookup
            - Uses constant-time hash comparison (via DB query)
            - Checks active status and expiration
        """
        # Validate format
        if not raw_key.startswith("eap_") or len(raw_key) != 52:
            log.warning("api_key.invalid_format", key_prefix=raw_key[:8] if len(raw_key) >= 8 else "invalid")
            raise InvalidAPIKeyError("Invalid API key format")

        # Hash the key for lookup
        key_hash = self._hash_key(raw_key)

        # Look up by hash
        stmt = select(APIKey).where(APIKey.key_hash == key_hash)
        result = await self._db.execute(stmt)
        api_key = result.scalar_one_or_none()

        if api_key is None:
            log.warning("api_key.not_found", key_hash_prefix=key_hash[:8])
            raise InvalidAPIKeyError("Invalid or revoked API key")

        # Check if active
        if not api_key.is_active:
            log.warning(
                "api_key.inactive",
                key_id=str(api_key.id),
                revoked_at=api_key.revoked_at,
            )
            raise InvalidAPIKeyError("Invalid or revoked API key")

        # Check expiration
        if api_key.expires_at is not None and datetime.now(UTC) > api_key.expires_at:
            log.warning(
                "api_key.expired",
                key_id=str(api_key.id),
                expired_at=api_key.expires_at,
            )
            raise InvalidAPIKeyError("API key has expired")

        log.debug(
            "api_key.validated",
            key_id=str(api_key.id),
            tenant_id=str(api_key.tenant_id),
            scopes=api_key.scopes,
        )

        return api_key

    async def list_keys(self, tenant_id: uuid.UUID) -> list[APIKey]:
        """List all API keys for a tenant.

        Args:
            tenant_id: Tenant ID to filter by

        Returns:
            List of APIKey models (excludes raw keys and hashes)
        """
        stmt = (
            select(APIKey)
            .where(APIKey.tenant_id == tenant_id)
            .order_by(APIKey.created_at.desc())
        )

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def revoke_key(self, key_id: uuid.UUID, tenant_id: uuid.UUID) -> APIKey | None:
        """Revoke an API key.

        Args:
            key_id: ID of the key to revoke
            tenant_id: Tenant ID for authorization check

        Returns:
            Revoked APIKey model or None if not found
        """
        stmt = select(APIKey).where(
            APIKey.id == key_id,
            APIKey.tenant_id == tenant_id,
        )

        result = await self._db.execute(stmt)
        api_key = result.scalar_one_or_none()

        if api_key is None:
            return None

        api_key.is_active = False
        api_key.revoked_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "api_key.revoked",
            key_id=str(key_id),
            tenant_id=str(tenant_id),
            name=api_key.name,
        )

        return api_key

    async def rotate_key(self, key_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[APIKey, str]:
        """Rotate an API key.

        Creates a new key with the same configuration and revokes the old one.

        Args:
            key_id: ID of the key to rotate
            tenant_id: Tenant ID for authorization check

        Returns:
            Tuple of (new APIKey model, new raw_key)

        Raises:
            ValueError: If key not found
        """
        # Get the old key
        stmt = select(APIKey).where(
            APIKey.id == key_id,
            APIKey.tenant_id == tenant_id,
        )

        result = await self._db.execute(stmt)
        old_key = result.scalar_one_or_none()

        if old_key is None:
            raise ValueError(f"API key {key_id} not found")

        # Calculate new expiration if the old key had one
        expires_in_days = None
        if old_key.expires_at is not None:
            # Calculate remaining days and use that for new key
            remaining_delta = old_key.expires_at - datetime.now(UTC)
            expires_in_days = max(1, int(remaining_delta.total_seconds() / 86400))

        # Create new key with same config
        new_key, new_raw_key = await self.create_key(
            tenant_id=tenant_id,
            name=old_key.name,
            scopes=old_key.scopes,
            created_by=old_key.created_by,
            description=old_key.description,
            expires_in_days=expires_in_days,
            rate_limit_per_minute=old_key.rate_limit_per_minute,
        )

        # Revoke the old key
        old_key.is_active = False
        old_key.revoked_at = datetime.now(UTC)
        await self._db.flush()

        log.info(
            "api_key.rotated",
            old_key_id=str(key_id),
            new_key_id=str(new_key.id),
            tenant_id=str(tenant_id),
            name=old_key.name,
        )

        return new_key, new_raw_key

    async def update_last_used(self, key_id: uuid.UUID) -> None:
        """Update the last_used_at timestamp for a key.

        This is a non-blocking update for performance. We don't need to
        wait for it to complete or check the result.

        Args:
            key_id: ID of the key to update
        """
        stmt = select(APIKey).where(APIKey.id == key_id)
        result = await self._db.execute(stmt)
        api_key = result.scalar_one_or_none()

        if api_key is not None:
            api_key.last_used_at = datetime.now(UTC)
            # Don't await flush - let it happen in background

    def _generate_key(self) -> str:
        """Generate a secure random API key.

        Format: eap_ + 48 base62 characters

        Returns:
            Secure random key string
        """
        # Generate 48 random base62 characters
        random_part = "".join(secrets.choice(_BASE62_ALPHABET) for _ in range(48))
        return f"eap_{random_part}"

    def _hash_key(self, raw_key: str) -> str:
        """Hash an API key using SHA-256.

        Args:
            raw_key: The raw API key to hash

        Returns:
            Hex-encoded SHA-256 hash (64 characters)
        """
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
