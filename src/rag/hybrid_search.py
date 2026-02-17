"""Hybrid search combining semantic (pgvector) and lexical (PostgreSQL full-text) retrieval.

Architecture:
1. Semantic search: pgvector cosine similarity on embeddings
2. Lexical search: PostgreSQL tsvector/tsquery for BM25-style ranking
3. Fusion: Reciprocal Rank Fusion (RRF) to merge both result sets
4. Tenant isolation: ALL queries scoped by tenant_id

The RRF algorithm:
  score(doc) = sum(1 / (k + rank_i))
  where k=60 is a constant, rank_i is position in result set i

This gives higher scores to documents appearing in both result sets,
while still surfacing unique results from each method.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.llm import LLMClient
from src.config import Settings
from src.models.document import Document, DocumentChunk

log = structlog.get_logger(__name__)

_RRF_K = 60  # Standard RRF constant


@dataclass
class SearchResult:
    """Single search result with rich metadata for citation."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    score: float
    content: str
    chunk_index: int
    metadata: dict[str, Any]
    source: str  # document filename
    document_version: str


class HybridSearchEngine:
    """Combines semantic and lexical search with Reciprocal Rank Fusion."""

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        llm_client: LLMClient,
        *,
        semantic_weight: float = 0.5,
        lexical_weight: float = 0.5,
    ) -> None:
        """Initialize hybrid search.

        Args:
            db: Async database session
            settings: Application settings
            llm_client: LLM client for embeddings
            semantic_weight: Weight for semantic results (0.0 - 1.0)
            lexical_weight: Weight for lexical results (0.0 - 1.0)
        """
        self._db = db
        self._settings = settings
        self._llm = llm_client
        self._semantic_weight = semantic_weight
        self._lexical_weight = lexical_weight

    async def search(
        self,
        *,
        query: str,
        tenant_id: uuid.UUID,
        top_k: int | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[SearchResult]:
        """Perform hybrid search combining semantic and lexical retrieval.

        Args:
            query: User's natural language query
            tenant_id: MANDATORY - scopes all results to this tenant
            top_k: Number of final results to return (default: settings.vector_top_k)
            document_ids: Optional filter to specific documents

        Returns:
            List of SearchResult objects, sorted by fused score (highest first)
        """
        effective_top_k = top_k or self._settings.vector_top_k

        if not query.strip():
            return []

        log.debug(
            "hybrid_search.start",
            query_preview=query[:50],
            tenant_id=str(tenant_id),
            top_k=effective_top_k,
        )

        # Execute both searches in parallel (could be optimized to run concurrently)
        semantic_results = await self._semantic_search(
            query=query,
            tenant_id=tenant_id,
            top_k=effective_top_k * 2,  # Fetch more for fusion
            document_ids=document_ids,
        )

        lexical_results = await self._lexical_search(
            query=query,
            tenant_id=tenant_id,
            top_k=effective_top_k * 2,
            document_ids=document_ids,
        )

        # Fuse results using RRF
        fused = await self._reciprocal_rank_fusion(
            semantic_results=semantic_results,
            lexical_results=lexical_results,
            tenant_id=tenant_id,
        )

        # Return top-K after fusion
        results = fused[:effective_top_k]

        log.debug(
            "hybrid_search.complete",
            tenant_id=str(tenant_id),
            semantic_count=len(semantic_results),
            lexical_count=len(lexical_results),
            fused_count=len(fused),
            final_count=len(results),
        )

        return results

    async def _semantic_search(
        self,
        *,
        query: str,
        tenant_id: uuid.UUID,
        top_k: int,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[tuple[uuid.UUID, float]]:
        """Perform pgvector semantic similarity search.

        Returns:
            List of (chunk_id, score) tuples
        """
        try:
            embeddings = await self._llm.embed([query])
            query_embedding = embeddings[0]
        except Exception as exc:
            log.warning("semantic_search.embed_failed", error=str(exc))
            return []

        embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"

        doc_filter = ""
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "top_k": top_k,
            "embedding": embedding_str,
        }

        if document_ids:
            doc_filter = "AND dc.document_id = ANY(:doc_ids)"
            params["doc_ids"] = [str(did) for did in document_ids]

        sql = text(f"""
            SELECT
                dc.id AS chunk_id,
                1 - (dc.embedding <=> CAST(:embedding AS vector)) AS score
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
            rows = result.all()
        except Exception as exc:
            log.error("semantic_search.query_failed", error=str(exc))
            return []

        return [(row.chunk_id, float(row.score)) for row in rows]

    async def _lexical_search(
        self,
        *,
        query: str,
        tenant_id: uuid.UUID,
        top_k: int,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[tuple[uuid.UUID, float]]:
        """Perform PostgreSQL full-text search using tsvector.

        Returns:
            List of (chunk_id, score) tuples
        """
        # Convert query to tsquery format (plainto_tsquery handles basic tokenization)
        doc_filter = ""
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "query": query,
            "top_k": top_k,
        }

        if document_ids:
            doc_filter = "AND dc.document_id = ANY(:doc_ids)"
            params["doc_ids"] = [str(did) for did in document_ids]

        # Use ts_rank_cd for BM25-style ranking (considers cover density)
        sql = text(f"""
            SELECT
                dc.id AS chunk_id,
                ts_rank_cd(
                    to_tsvector('english', dc.content),
                    plainto_tsquery('english', :query)
                ) AS score
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            WHERE
                dc.tenant_id = :tenant_id
                AND d.tenant_id = :tenant_id
                AND d.status = 'ready'
                AND to_tsvector('english', dc.content) @@ plainto_tsquery('english', :query)
                {doc_filter}
            ORDER BY score DESC
            LIMIT :top_k
        """)

        try:
            result = await self._db.execute(sql, params)
            rows = result.all()
        except Exception as exc:
            log.error("lexical_search.query_failed", error=str(exc))
            return []

        return [(row.chunk_id, float(row.score)) for row in rows]

    async def _reciprocal_rank_fusion(
        self,
        *,
        semantic_results: list[tuple[uuid.UUID, float]],
        lexical_results: list[tuple[uuid.UUID, float]],
        tenant_id: uuid.UUID,
    ) -> list[SearchResult]:
        """Fuse semantic and lexical results using RRF.

        RRF formula: score(doc) = sum(weight_i / (k + rank_i))

        Args:
            semantic_results: List of (chunk_id, semantic_score) tuples
            lexical_results: List of (chunk_id, lexical_score) tuples
            tenant_id: Tenant ID for fetching chunks

        Returns:
            Fused and sorted list of SearchResult objects
        """
        # Build rank maps
        semantic_ranks = {chunk_id: rank for rank, (chunk_id, _) in enumerate(semantic_results)}
        lexical_ranks = {chunk_id: rank for rank, (chunk_id, _) in enumerate(lexical_results)}

        # Get all unique chunk IDs
        all_chunk_ids = set(semantic_ranks.keys()) | set(lexical_ranks.keys())

        # Calculate RRF scores
        rrf_scores: dict[uuid.UUID, float] = {}
        for chunk_id in all_chunk_ids:
            score = 0.0
            if chunk_id in semantic_ranks:
                score += self._semantic_weight / (_RRF_K + semantic_ranks[chunk_id])
            if chunk_id in lexical_ranks:
                score += self._lexical_weight / (_RRF_K + lexical_ranks[chunk_id])
            rrf_scores[chunk_id] = score

        # Sort by RRF score descending
        sorted_chunk_ids = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        # Extract ordered chunk IDs and fetch full data
        chunk_ids = [cid for cid, _ in sorted_chunk_ids]
        return await self._fetch_chunks(
            chunk_ids=chunk_ids,
            scores=rrf_scores,
            tenant_id=tenant_id,
        )

    async def _fetch_chunks(
        self,
        *,
        chunk_ids: list[uuid.UUID],
        scores: dict[uuid.UUID, float],
        tenant_id: uuid.UUID,
    ) -> list[SearchResult]:
        """Fetch full chunk data for the given IDs.

        Args:
            chunk_ids: Ordered list of chunk IDs to fetch
            scores: Map of chunk_id -> fused_score
            tenant_id: Tenant ID for validation

        Returns:
            List of SearchResult objects in the same order as chunk_ids
        """
        if not chunk_ids:
            return []

        sql = select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.content,
            DocumentChunk.chunk_index,
            DocumentChunk.chunk_metadata,
            Document.filename,
            Document.version,
        ).join(
            Document, Document.id == DocumentChunk.document_id
        ).where(
            DocumentChunk.id.in_(chunk_ids),
            DocumentChunk.tenant_id == tenant_id,
            Document.tenant_id == tenant_id,
        )

        result = await self._db.execute(sql)
        rows = result.all()

        # Build map for quick lookup
        chunk_map = {
            row.id: SearchResult(
                chunk_id=row.id,
                document_id=row.document_id,
                score=scores[row.id],
                content=row.content,
                chunk_index=row.chunk_index,
                metadata=dict(row.chunk_metadata) if row.chunk_metadata else {},
                source=row.filename,
                document_version=row.version,
            )
            for row in rows
        }

        # Return in original order
        return [chunk_map[cid] for cid in chunk_ids if cid in chunk_map]


async def hybrid_search_from_context(
    db: AsyncSession,
    settings: Settings,
    llm_client: LLMClient,
) -> HybridSearchEngine:
    """Factory for HybridSearchEngine - used by tool gateway."""
    return HybridSearchEngine(db=db, settings=settings, llm_client=llm_client)
