"""Embedding cache - avoid re-computing embeddings for identical text.

Embeddings are expensive (LLM API call) and entirely deterministic for
a given (text, model) pair. This cache uses the SHA-256 hash of the text
as the key so the raw text never appears in Redis and identical text
always resolves to the same key regardless of where it came from.

Default TTL is 86400 s (24 hours) because embeddings don't change unless
the underlying model changes. A longer TTL is safe because the hash key
naturally invalidates when the text changes.
"""

from __future__ import annotations

import hashlib

import structlog

from src.cache.backend import CacheBackend

log = structlog.get_logger(__name__)

_EMBEDDING_NS = "emb"
_DEFAULT_EMBEDDING_TTL = 86400  # 24 hours


def _text_hash(text: str) -> str:
    """Return SHA-256 hex digest of the input text (UTF-8 encoded)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _embedding_key(text_hash: str) -> str:
    return f"{_EMBEDDING_NS}:{text_hash}"


class EmbeddingCache:
    """Cache for dense vector embeddings keyed on content hash.

    The caller is responsible for computing text_hash. Using the content
    hash decouples the cache from the source format: the same chunk of
    text embedded from a PDF, docx, or plain text all share one entry.
    """

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Key helpers (public so callers can pre-compute without a get)
    # ------------------------------------------------------------------

    @staticmethod
    def hash_text(text: str) -> str:
        """Return the canonical hash for a piece of text.

        Use this to build a text_hash before calling get_embedding /
        cache_embedding so you never have to pass the raw text around.
        """
        return _text_hash(text)

    # ------------------------------------------------------------------
    # Single-item operations
    # ------------------------------------------------------------------

    async def get_embedding(self, text_hash: str) -> list[float] | None:
        """Return cached embedding for text_hash, or None on miss."""
        key = _embedding_key(text_hash)
        result = await self._backend.get(key)
        if result is None:
            log.debug("cache.embedding.miss", text_hash=text_hash[:16])
            return None
        log.debug("cache.embedding.hit", text_hash=text_hash[:16])
        return list(result)  # backend may return a plain list or JSON array

    async def cache_embedding(
        self,
        text_hash: str,
        embedding: list[float],
        ttl: int = _DEFAULT_EMBEDDING_TTL,
    ) -> None:
        """Store a vector embedding under text_hash.

        Args:
            text_hash: SHA-256 hex digest of the source text
            embedding: Dense vector as list[float]
            ttl: Seconds until expiry (default 24h)
        """
        key = _embedding_key(text_hash)
        await self._backend.set(key, embedding, ttl)
        log.debug(
            "cache.embedding.stored",
            text_hash=text_hash[:16],
            dims=len(embedding),
            ttl=ttl,
        )

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def batch_get(
        self, text_hashes: list[str]
    ) -> dict[str, list[float] | None]:
        """Return embeddings for multiple hashes in one logical operation.

        Missing hashes map to None in the result. The dict preserves the
        input order (Python 3.7+ dict is ordered by insertion).

        Args:
            text_hashes: List of SHA-256 hex digests

        Returns:
            Dict mapping each text_hash to its embedding or None.
        """
        results: dict[str, list[float] | None] = {}
        for text_hash in text_hashes:
            results[text_hash] = await self.get_embedding(text_hash)
        return results

    async def batch_cache(
        self,
        embeddings: dict[str, list[float]],
        ttl: int = _DEFAULT_EMBEDDING_TTL,
    ) -> None:
        """Store multiple embeddings in one logical operation.

        Args:
            embeddings: Mapping of text_hash -> embedding vector
            ttl: Seconds until expiry for all entries (default 24h)
        """
        for text_hash, embedding in embeddings.items():
            await self.cache_embedding(text_hash, embedding, ttl)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def invalidate(self, text_hash: str) -> None:
        """Remove a single embedding from the cache."""
        key = _embedding_key(text_hash)
        await self._backend.delete(key)
        log.debug("cache.embedding.invalidated", text_hash=text_hash[:16])

    async def flush(self) -> None:
        """Remove all cached embeddings (pattern-based)."""
        deleted = await self._backend.delete_pattern(f"{_EMBEDDING_NS}:*")
        log.info("cache.embedding.flushed", keys_deleted=deleted)
