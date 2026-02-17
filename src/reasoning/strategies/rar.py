"""Retrieval-Augmented Reasoning (RAR) strategy.

Interleaves reasoning with retrieval to fill knowledge gaps identified during
the reasoning process.  This is distinct from standard RAG: instead of
retrieving once upfront, RAR treats retrieval as a tool the reasoning process
can invoke when it realises it lacks information.

5-step pipeline:
  1. **Initial reasoning**: attempt to answer with pre-loaded context only
  2. **Gap identification**: list knowledge gaps and generate search queries
  3. **Retrieval**: fetch documents for each search query via ``retrieve_fn``
  4. **Augmented reasoning**: reason again with the retrieved documents
  5. **Source verification**: verify the answer is grounded in retrieved sources

If no ``retrieve_fn`` is provided the strategy falls back gracefully, skipping
retrieval (steps 3-4 become no-ops) and performing only initial reasoning +
verification.

LLM call flow:
  1 (initial reasoning) + 1 (gap identification) + 1 (augmented reasoning)
  + 1 (source verification) = 4 LLM calls minimum

``retrieve_fn`` signature:
    async def retrieve(query: str) -> list[str]:
        ...  # Returns list of document text snippets
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import structlog

from src.agent.llm import LLMClient
from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy

log = structlog.get_logger(__name__)

# Type alias for the pluggable retrieval function
RetrieveFn = Callable[[str], Awaitable[list[str]]]


class RetrievalAugmentedReasoningStrategy(ReasoningStrategy):
    """5-step reasoning that identifies and fills knowledge gaps via retrieval.

    Args:
        retrieve_fn:         Async callable ``(query: str) -> list[str]``.
                             Returns a list of document snippets.  Pass ``None``
                             to disable retrieval (degrades to CoT).
        max_search_queries:  Max retrieval queries generated in step 2.
        temperature:         LLM temperature for reasoning calls.
        max_tokens:          Max tokens per LLM call.
    """

    def __init__(
        self,
        retrieve_fn: RetrieveFn | None = None,
        max_search_queries: int = 3,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> None:
        self._retrieve_fn = retrieve_fn
        self._max_search_queries = max_search_queries
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "retrieval_augmented_reasoning"

    async def reason(
        self,
        query: str,
        context: str,
        llm_client: LLMClient,
    ) -> ReasoningResult:
        """Execute the 5-step RAR pipeline."""
        log.debug("rar.start", query_length=len(query), has_retrieve_fn=self._retrieve_fn is not None)
        total_tokens = 0
        pipeline_steps: list[str] = []
        all_sources: list[str] = []

        # ------------------------------------------------------------------ #
        # Step 1: Initial reasoning attempt with available context
        # ------------------------------------------------------------------ #
        initial_prompt = f"""You are a precise reasoning assistant.

Attempt to answer the following query using ONLY the context provided.
If you lack sufficient information, still provide your best partial answer
and clearly note what is missing.

CONTEXT:
{context[:2500] if context else "(none provided)"}

QUERY: {query}

Respond ONLY with valid JSON:
{{
    "partial_answer": "Your best answer given available context",
    "confidence": 0.0,
    "is_complete": false,
    "missing_info": ["list of what information is missing or uncertain"]
}}"""

        initial_messages = [
            {"role": "system", "content": "You are a precise reasoning assistant. Always respond with valid JSON only."},
            {"role": "user", "content": initial_prompt},
        ]

        initial_answer = ""
        initial_confidence = 0.0
        is_complete = False
        missing_info: list[str] = []

        try:
            initial_response = await llm_client.complete(
                messages=initial_messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            total_tokens += self._extract_tokens(initial_response)
            initial_parsed = json.loads(llm_client.extract_text(initial_response))

            initial_answer = initial_parsed.get("partial_answer", "")
            initial_confidence = float(initial_parsed.get("confidence", 0.0))
            is_complete = bool(initial_parsed.get("is_complete", False))
            missing_info = initial_parsed.get("missing_info", [])

            log.debug(
                "rar.initial_complete",
                is_complete=is_complete,
                missing_count=len(missing_info),
                confidence=initial_confidence,
            )
            pipeline_steps.append(
                f"Step 1 (initial): confidence={initial_confidence:.2f}, "
                f"complete={is_complete}, gaps={len(missing_info)}"
            )

        except Exception as exc:
            log.error("rar.initial_failed", error=str(exc))
            initial_answer = f"Initial reasoning failed: {exc}"
            pipeline_steps.append(f"Step 1 (initial): FAILED – {exc}")

        # If answer is already complete and confident, skip retrieval
        if is_complete and initial_confidence >= 0.8:
            log.info("rar.skipping_retrieval", reason="complete_and_confident")
            pipeline_steps.append("Step 2-4: skipped (initial answer sufficient)")
            return ReasoningResult(
                answer=initial_answer,
                confidence=initial_confidence,
                steps=pipeline_steps,
                strategy_name=self.name,
                token_count=total_tokens,
                reasoning_chain=[{"step": "initial", "answer": initial_answer}],
                metadata={
                    "retrieval_performed": False,
                    "missing_info": missing_info,
                    "sources": [],
                },
            )

        # ------------------------------------------------------------------ #
        # Step 2: Identify knowledge gaps and generate search queries
        # ------------------------------------------------------------------ #
        gap_prompt = f"""You are a knowledge gap analyser.

Given the following partial answer and its identified missing information,
generate up to {self._max_search_queries} focused search queries that would
retrieve the missing information needed to complete the answer.

QUERY: {query}
PARTIAL ANSWER: {initial_answer}
MISSING INFO:
{chr(10).join(f"- {m}" for m in missing_info) or "- General context needed"}

Respond ONLY with valid JSON:
{{
    "search_queries": [
        "specific search query 1",
        "specific search query 2"
    ],
    "gap_summary": "one sentence describing what information is needed"
}}

Limit to {self._max_search_queries} queries. Focus on the most important gaps.
Respond ONLY with JSON, no additional text."""

        gap_messages = [
            {"role": "system", "content": "You are a knowledge gap specialist. Always respond with valid JSON only."},
            {"role": "user", "content": gap_prompt},
        ]

        search_queries: list[str] = []
        gap_summary = ""

        try:
            gap_response = await llm_client.complete(
                messages=gap_messages,
                temperature=0.2,
                max_tokens=512,
            )
            total_tokens += self._extract_tokens(gap_response)
            gap_parsed = json.loads(llm_client.extract_text(gap_response))

            search_queries = gap_parsed.get("search_queries", [])[:self._max_search_queries]
            gap_summary = gap_parsed.get("gap_summary", "")

            log.debug("rar.gaps_identified", queries=len(search_queries), gap_summary=gap_summary)
            pipeline_steps.append(
                f"Step 2 (gaps): identified {len(search_queries)} search queries – {gap_summary}"
            )

        except Exception as exc:
            log.warning("rar.gap_identification_failed", error=str(exc))
            # Fallback: use the original query as the search query
            search_queries = [query]
            pipeline_steps.append(f"Step 2 (gaps): FAILED, using original query – {exc}")

        # ------------------------------------------------------------------ #
        # Step 3: Retrieve relevant documents
        # ------------------------------------------------------------------ #
        retrieved_docs: list[str] = []

        if self._retrieve_fn is not None and search_queries:
            async def fetch(sq: str) -> list[str]:
                try:
                    docs = await self._retrieve_fn(sq)
                    log.debug("rar.retrieved", query=sq[:60], docs=len(docs))
                    return docs
                except Exception as exc:
                    log.warning("rar.retrieve_failed", query=sq[:60], error=str(exc))
                    return []

            fetch_results = await asyncio.gather(*[fetch(sq) for sq in search_queries])
            for docs in fetch_results:
                retrieved_docs.extend(docs)

            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_docs: list[str] = []
            for doc in retrieved_docs:
                if doc not in seen:
                    seen.add(doc)
                    unique_docs.append(doc)
            retrieved_docs = unique_docs
            all_sources = list(search_queries)

            log.debug("rar.retrieval_complete", total_docs=len(retrieved_docs))
            pipeline_steps.append(
                f"Step 3 (retrieve): fetched {len(retrieved_docs)} documents "
                f"from {len(search_queries)} queries"
            )
        else:
            pipeline_steps.append("Step 3 (retrieve): skipped (no retrieve_fn configured)")

        # ------------------------------------------------------------------ #
        # Step 4: Reason again with retrieved context
        # ------------------------------------------------------------------ #
        retrieved_context = "\n\n---\n\n".join(
            f"[Document {i + 1}]: {doc[:800]}"
            for i, doc in enumerate(retrieved_docs[:5])  # Cap at 5 docs
        )

        augmented_prompt = f"""You are a precise reasoning assistant with access to retrieved documents.

Answer the following query using both the original context and the newly
retrieved documents below.  Cite which documents support your claims where
relevant.

ORIGINAL CONTEXT:
{context[:1500] if context else "(none)"}

RETRIEVED DOCUMENTS:
{retrieved_context or "(no documents retrieved)"}

QUERY: {query}
PARTIAL ANSWER FROM INITIAL REASONING: {initial_answer}

Now provide a complete, well-grounded answer.

Respond ONLY with valid JSON:
{{
    "final_answer": "Complete answer grounded in the available evidence",
    "confidence": 0.0,
    "sources_used": ["document 1", "original context", ...]
}}

Respond ONLY with JSON, no additional text."""

        augmented_messages = [
            {"role": "system", "content": "You are a precise reasoning assistant. Always respond with valid JSON only."},
            {"role": "user", "content": augmented_prompt},
        ]

        augmented_answer = initial_answer
        augmented_confidence = initial_confidence
        sources_used: list[str] = []

        try:
            augmented_response = await llm_client.complete(
                messages=augmented_messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            total_tokens += self._extract_tokens(augmented_response)
            augmented_parsed = json.loads(llm_client.extract_text(augmented_response))

            augmented_answer = augmented_parsed.get("final_answer", augmented_answer)
            augmented_confidence = float(augmented_parsed.get("confidence", augmented_confidence))
            sources_used = augmented_parsed.get("sources_used", [])

            log.debug("rar.augmented_complete", confidence=augmented_confidence, sources=len(sources_used))
            pipeline_steps.append(
                f"Step 4 (augmented): confidence={augmented_confidence:.2f}, "
                f"sources_used={len(sources_used)}"
            )

        except Exception as exc:
            log.error("rar.augmented_failed", error=str(exc))
            pipeline_steps.append(f"Step 4 (augmented): FAILED – {exc}")

        # ------------------------------------------------------------------ #
        # Step 5: Source verification
        # ------------------------------------------------------------------ #
        verify_prompt = f"""Verify that the following answer is supported by the provided sources.

QUERY: {query}
ANSWER: {augmented_answer}

ORIGINAL CONTEXT SNIPPET:
{context[:500] if context else "(none)"}

RETRIEVED DOCUMENTS SNIPPET:
{retrieved_context[:800] if retrieved_context else "(none)"}

Check:
1. Is the answer factually consistent with the sources?
2. Are there any unsupported claims?
3. Does the answer fully address the query?

Respond ONLY with valid JSON:
{{
    "is_grounded": true,
    "unsupported_claims": ["claim 1 if unsupported"],
    "verified_confidence": 0.0,
    "verification_note": "brief summary"
}}

Respond ONLY with JSON, no additional text."""

        verify_messages = [
            {"role": "system", "content": "You are a source verification specialist. Always respond with valid JSON only."},
            {"role": "user", "content": verify_prompt},
        ]

        final_confidence = augmented_confidence
        verification_result: dict = {}

        try:
            verify_response = await llm_client.complete(
                messages=verify_messages,
                temperature=0.2,
                max_tokens=512,
            )
            total_tokens += self._extract_tokens(verify_response)
            verification_result = json.loads(llm_client.extract_text(verify_response))

            final_confidence = float(
                verification_result.get("verified_confidence", augmented_confidence)
            )

            # Penalise for unsupported claims
            unsupported = verification_result.get("unsupported_claims", [])
            if unsupported:
                penalty = min(0.15 * len(unsupported), 0.4)
                final_confidence = max(0.0, final_confidence - penalty)

            log.debug(
                "rar.verification_complete",
                is_grounded=verification_result.get("is_grounded"),
                verified_confidence=final_confidence,
            )
            pipeline_steps.append(
                f"Step 5 (verify): grounded={verification_result.get('is_grounded')}, "
                f"confidence={final_confidence:.2f}"
            )

        except Exception as exc:
            log.warning("rar.verification_failed", error=str(exc))
            final_confidence = augmented_confidence * 0.9
            verification_result = {"is_grounded": None, "verification_note": f"Verification failed: {exc}"}
            pipeline_steps.append(f"Step 5 (verify): FAILED – {exc}")

        log.info(
            "rar.complete",
            final_confidence=final_confidence,
            total_tokens=total_tokens,
            docs_retrieved=len(retrieved_docs),
        )

        # ------------------------------------------------------------------ #
        # Assemble result
        # ------------------------------------------------------------------ #
        reasoning_chain = [
            {"step": "initial_reasoning", "answer": initial_answer, "confidence": initial_confidence},
            {"step": "gap_identification", "search_queries": search_queries, "gap_summary": gap_summary},
            {"step": "retrieval", "docs_retrieved": len(retrieved_docs), "sources": all_sources},
            {"step": "augmented_reasoning", "answer": augmented_answer, "confidence": augmented_confidence},
            {"step": "verification", "result": verification_result},
        ]

        return ReasoningResult(
            answer=augmented_answer,
            confidence=final_confidence,
            steps=pipeline_steps,
            strategy_name=self.name,
            token_count=total_tokens,
            reasoning_chain=reasoning_chain,
            metadata={
                "retrieval_performed": len(retrieved_docs) > 0,
                "docs_retrieved": len(retrieved_docs),
                "search_queries": search_queries,
                "sources_used": sources_used,
                "is_grounded": verification_result.get("is_grounded"),
                "missing_info": missing_info,
            },
        )
