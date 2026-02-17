"""RAG retrieval pipeline.

Given a user query:
1. Embed the query using the same model as ingestion
   (checks embedding cache first to avoid redundant LLM calls)
2. Perform pgvector cosine similarity search, filtered by tenant_id
3. Return top-K chunks with their source document metadata

Tenant isolation is enforced at the database level: every similarity search
includes WHERE tenant_id = :tenant_id. It is impossible to retrieve chunks
belonging to another tenant through this interface.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.config import Settings
from src.rag.hybrid_search import HybridSearchEngine
from src.rag.reranker import CrossEncoderReranker

log = structlog.get_logger(__name__)

# Lazy import to avoid circular dependencies; EmbeddingCache is injected at
# runtime, not imported at module level.
try:
    from src.cache.embedding_cache import EmbeddingCache as _EmbeddingCache
except ImportError:
    _EmbeddingCache = None  # type: ignore[assignment,misc]


class RetrievalService:
    """Semantic retrieval with mandatory tenant isolation."""

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        embedding_cache: Any | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._llm = llm_client
        # Optional embedding cache to avoid re-computing query embeddings
        self._embedding_cache = embedding_cache
        # Enhanced retrieval with hybrid search + reranking
        self._hybrid_search = HybridSearchEngine(db=db, settings=settings, llm_client=llm_client)
        self._reranker = CrossEncoderReranker()

    async def retrieve(
        self,
        *,
        query: str,
        tenant_id: uuid.UUID,
        top_k: int | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve the most relevant chunks for a query.

        Args:
            query: User's natural language query
            tenant_id: MANDATORY - scopes all results to this tenant
            top_k: Number of chunks to return (defaults to settings.vector_top_k)
            document_ids: Optional filter to search within specific documents only

        Returns:
            List of chunk dicts with keys:
              chunk_id, document_id, document_name, document_version,
              chunk_index, content, similarity_score, metadata
        """
        effective_top_k = top_k or self._settings.vector_top_k

        if not query.strip():
            return []

        # 1. Embed the query (check embedding cache first to avoid redundant LLM calls)
        try:
            query_embedding: list[float] | None = None

            if self._embedding_cache is not None:
                from src.cache.embedding_cache import EmbeddingCache
                text_hash = EmbeddingCache.hash_text(query)
                query_embedding = await self._embedding_cache.get_embedding(text_hash)
                if query_embedding is not None:
                    log.debug("retrieve.embedding_cache_hit", query_preview=query[:40])

            if query_embedding is None:
                embeddings = await self._llm.embed([query])
                query_embedding = embeddings[0]
                # Store in cache for future calls (best-effort)
                if self._embedding_cache is not None:
                    try:
                        from src.cache.embedding_cache import EmbeddingCache
                        text_hash = EmbeddingCache.hash_text(query)
                        await self._embedding_cache.cache_embedding(text_hash, query_embedding)
                    except Exception as cache_exc:
                        log.debug("retrieve.embedding_cache_store_failed", error=str(cache_exc))

        except Exception as exc:
            log.warning("retrieve.embed_failed", error=str(exc))
            return []

        # 2. pgvector similarity search with tenant isolation
        #
        # We use raw SQL for the pgvector <=> operator (cosine distance).
        # The cast to vector ensures pgvector comparison.
        # CRITICAL: tenant_id filter is ALWAYS present - no exceptions.
        #
        embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

        doc_filter = ""
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "top_k": effective_top_k,
            "embedding": embedding_str,
        }

        if document_ids:
            doc_filter = "AND dc.document_id = ANY(:doc_ids)"
            params["doc_ids"] = [str(did) for did in document_ids]

        sql = text(f"""
            SELECT
                dc.id              AS chunk_id,
                dc.document_id,
                dc.tenant_id,
                dc.content,
                dc.chunk_index,
                dc.chunk_metadata  AS metadata,
                d.filename         AS document_name,
                d.version          AS document_version,
                1 - (dc.embedding <=> CAST(:embedding AS vector)) AS similarity_score
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            WHERE
                dc.tenant_id = :tenant_id
                AND d.tenant_id = :tenant_id
                AND d.status = 'ready'
                AND dc.embedding IS NOT NULL
                {doc_filter}
            ORDER BY dc.embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """)

        try:
            result = await self._db.execute(sql, params)
            rows = result.mappings().all()
        except Exception as exc:
            log.error("retrieve.query_failed", error=str(exc))
            return []

        chunks = []
        for row in rows:
            # Double-check tenant isolation at the application layer
            if str(row["tenant_id"]) != str(tenant_id):
                log.error(
                    "retrieve.tenant_isolation_violation",
                    chunk_tenant=str(row["tenant_id"]),
                    requesting_tenant=str(tenant_id),
                )
                continue  # Skip and alert - never return foreign tenant data

            chunks.append({
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "document_name": row["document_name"],
                "document_version": row["document_version"],
                "chunk_index": row["chunk_index"],
                "content": row["content"],
                "similarity_score": float(row["similarity_score"]),
                "metadata": dict(row["metadata"]) if row["metadata"] else {},
            })

        # 11C3: Apply feedback-based score adjustments (fail-open)
        chunks = await self._apply_feedback_weights(chunks=chunks, tenant_id=tenant_id)

        log.debug(
            "retrieve.complete",
            query_preview=query[:50],
            tenant_id=str(tenant_id),
            result_count=len(chunks),
        )

        return chunks

    async def _apply_feedback_weights(
        self,
        *,
        chunks: list[dict[str, Any]],
        tenant_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Apply feedback-derived score multipliers to retrieved chunks.

        The response_feedback table does not contain a document_id column.
        Its `tags` field stores user-facing labels (e.g. "accurate",
        "helpful") -- not document identifiers.  The `model_used` column
        stores the LLM model name, not a document reference.

        Until the feedback schema is extended with an explicit document_id
        foreign key, document-level sentiment weighting cannot be performed
        reliably.  We skip the adjustment and return chunks unchanged.

        Args:
            chunks: Chunks as returned by the pgvector query
            tenant_id: Tenant scope (unused until schema supports doc-level feedback)

        Returns:
            Chunks unchanged (feedback weighting skipped)
        """
        if not chunks:
            return chunks

        log.debug(
            "rag.feedback_weighting.skipped",
            reason="feedback schema lacks document_id column",
            tenant_id=str(tenant_id),
        )

        return chunks

    async def enhanced_retrieve(
        self,
        *,
        query: str,
        tenant_id: uuid.UUID,
        top_k: int | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[dict[str, Any]]:
        """Enhanced retrieval with hybrid search + reranking.

        Enhanced retrieval that combines:
        1. Hybrid search (semantic + BM25 lexical)
        2. Cross-encoder reranking for relevance

        Args:
            query: User's natural language query
            tenant_id: MANDATORY - scopes all results to this tenant
            top_k: Number of chunks to return (defaults to settings.vector_top_k)
            document_ids: Optional filter to search within specific documents only

        Returns:
            Reranked list of chunks with enhanced relevance scores
        """
        effective_top_k = top_k or self._settings.vector_top_k

        if not query.strip():
            return []

        # Use settings flag to enable/disable enhanced retrieval
        use_enhanced = getattr(self._settings, "use_enhanced_retrieval", True)

        if not use_enhanced:
            # Fall back to standard retrieval
            return await self.retrieve(
                query=query,
                tenant_id=tenant_id,
                top_k=top_k,
                document_ids=document_ids,
            )

        try:
            # Stage 1: Hybrid search (semantic + BM25)
            # Retrieve more candidates than needed for reranking
            candidate_multiplier = 3
            hybrid_results = await self._hybrid_search.search(
                query=query,
                tenant_id=tenant_id,
                top_k=effective_top_k * candidate_multiplier,
                document_ids=document_ids,
            )

            if not hybrid_results:
                return []

            # Stage 2: Cross-encoder reranking
            reranked_results = await self._reranker.rerank(
                query=query,
                chunks=hybrid_results,
                top_k=effective_top_k,
            )

            log.debug(
                "retrieve.enhanced_complete",
                query_preview=query[:50],
                tenant_id=str(tenant_id),
                candidates=len(hybrid_results),
                final_count=len(reranked_results),
            )

            return reranked_results

        except Exception as exc:
            log.warning(
                "retrieve.enhanced_failed_fallback",
                error=str(exc),
                query_preview=query[:50],
            )
            # Fall back to standard retrieval on error
            return await self.retrieve(
                query=query,
                tenant_id=tenant_id,
                top_k=top_k,
                document_ids=document_ids,
            )


async def retrieval_service_from_context(
    db: AsyncSession,
    settings: Settings,
    llm_client: LLMClient,
) -> RetrievalService:
    """Factory for RetrievalService - used by tool gateway."""
    return RetrievalService(db=db, settings=settings, llm_client=llm_client)
