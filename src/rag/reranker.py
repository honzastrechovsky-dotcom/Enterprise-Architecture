"""Cross-encoder reranking using LLM for relevance scoring.

After initial retrieval (semantic, lexical, or hybrid), reranking refines
the result order by scoring each chunk's relevance to the query using an LLM.

Architecture:
1. Take initial search results (top-K from retrieval)
2. Use LLM to score each chunk's relevance (0-10 scale)
3. Batch scoring for efficiency (32 chunks per LLM call)
4. Re-sort by LLM relevance scores
5. Return top-N after reranking

Design decisions:
- Use structured prompting for consistent scoring
- Batch processing to reduce LLM calls
- Configurable top_k to control output size
- Preserve all SearchResult metadata through the pipeline
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.agent.llm import LLMClient

log = structlog.get_logger(__name__)

_RERANK_BATCH_SIZE = 32  # Process this many chunks per LLM call


@dataclass
class RankedResult:
    """Search result with LLM-generated relevance score."""

    chunk_id: Any  # uuid.UUID, kept as Any for compatibility
    document_id: Any
    relevance_score: float  # 0.0 - 1.0 from LLM
    original_score: float  # Original retrieval score
    content: str
    chunk_index: int
    metadata: dict[str, Any]
    source: str
    document_version: str


_RERANK_PROMPT_TEMPLATE = """You are a relevance scoring system. Given a user query and a document chunk, rate how relevant the chunk is to the query on a scale of 0-10.

Query: {query}

Chunk:
{chunk}

Respond with ONLY a single number between 0 and 10, where:
- 0 = completely irrelevant
- 5 = somewhat relevant
- 10 = extremely relevant and directly answers the query

Score:"""


class CrossEncoderReranker:
    """Rerank search results using LLM-based relevance scoring."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model: str | None = None,
    ) -> None:
        """Initialize reranker.

        Args:
            llm_client: LLM client for scoring
            model: Optional model override (defaults to LLMClient's default)
        """
        self._llm = llm_client
        self._model = model

    async def rerank(
        self,
        *,
        query: str,
        results: list[Any],  # List of SearchResult-like objects
        top_k: int | None = None,
    ) -> list[RankedResult]:
        """Rerank search results by LLM relevance scoring.

        Args:
            query: Original user query
            results: List of SearchResult objects from retrieval
            top_k: Number of results to return after reranking (default: all)

        Returns:
            List of RankedResult objects sorted by relevance_score (highest first)
        """
        if not results:
            return []

        effective_top_k = top_k or len(results)

        log.debug(
            "reranker.start",
            query_preview=query[:50],
            input_count=len(results),
            top_k=effective_top_k,
        )

        # Score all results in batches
        scored_results: list[RankedResult] = []

        for batch_start in range(0, len(results), _RERANK_BATCH_SIZE):
            batch = results[batch_start : batch_start + _RERANK_BATCH_SIZE]
            batch_scores = await self._score_batch(query=query, results=batch)

            for result, score in zip(batch, batch_scores):
                scored_results.append(
                    RankedResult(
                        chunk_id=result.chunk_id,
                        document_id=result.document_id,
                        relevance_score=score,
                        original_score=result.score,
                        content=result.content,
                        chunk_index=result.chunk_index,
                        metadata=result.metadata,
                        source=result.source,
                        document_version=result.document_version,
                    )
                )

            log.debug(
                "reranker.batch_complete",
                batch_start=batch_start,
                batch_size=len(batch),
            )

        # Sort by LLM relevance score (descending)
        scored_results.sort(key=lambda x: x.relevance_score, reverse=True)

        # Return top-K
        final_results = scored_results[:effective_top_k]

        log.debug(
            "reranker.complete",
            input_count=len(results),
            output_count=len(final_results),
        )

        return final_results

    async def _score_batch(
        self,
        *,
        query: str,
        results: list[Any],
    ) -> list[float]:
        """Score a batch of results using LLM.

        Args:
            query: User query
            results: Batch of SearchResult objects

        Returns:
            List of scores (0.0 - 1.0), one per result
        """
        # Build prompts for each result
        prompts = [
            _RERANK_PROMPT_TEMPLATE.format(
                query=query,
                chunk=result.content[:2000],  # Truncate to avoid token limits
            )
            for result in results
        ]

        # Score each prompt
        scores: list[float] = []

        for prompt in prompts:
            try:
                messages = [{"role": "user", "content": prompt}]
                response = await self._llm.complete(
                    messages=messages,
                    model=self._model,
                    temperature=0.0,  # Deterministic scoring
                    max_tokens=10,  # Only need a single number
                )

                text = self._llm.extract_text(response).strip()

                # Parse score (0-10 scale) and normalize to 0-1
                try:
                    raw_score = float(text)
                    normalized_score = max(0.0, min(10.0, raw_score)) / 10.0
                    scores.append(normalized_score)
                except ValueError:
                    log.warning("reranker.parse_failed", text=text)
                    scores.append(0.5)  # Default to neutral score

            except Exception as exc:
                log.warning("reranker.score_failed", error=str(exc))
                scores.append(0.5)  # Default to neutral score on error

        return scores


async def reranker_from_context(llm_client: LLMClient) -> CrossEncoderReranker:
    """Factory for CrossEncoderReranker - used by tool gateway."""
    return CrossEncoderReranker(llm_client=llm_client)
