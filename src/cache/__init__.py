"""Response Caching Layer.

Public API:
    CacheBackend          - Abstract base for all backends
    RedisCacheBackend     - Redis-backed production cache
    InMemoryCacheBackend  - Dict-backed cache for dev/testing
    get_cache_backend     - Factory: selects backend from settings

    CachedResponse        - Dataclass returned by ResponseCache
    ResponseCache         - Tenant-scoped LLM response cache

    EmbeddingCache        - Content-hash-keyed embedding cache

    CacheMiddleware       - FastAPI middleware for HTTP response caching
"""

from src.cache.backend import (
    CacheBackend,
    InMemoryCacheBackend,
    RedisCacheBackend,
    get_cache_backend,
)
from src.cache.embedding_cache import EmbeddingCache
from src.cache.middleware import CacheMiddleware
from src.cache.response_cache import CachedResponse, ResponseCache

__all__ = [
    "CacheBackend",
    "RedisCacheBackend",
    "InMemoryCacheBackend",
    "get_cache_backend",
    "CachedResponse",
    "ResponseCache",
    "EmbeddingCache",
    "CacheMiddleware",
]
