"""Self-Consistency reasoning strategy.

Runs the same query N times (default 3) with slightly higher temperature to
get diverse answers, then aggregates via majority vote.  The consistency score
(fraction of runs that agree with the majority answer) is reported as part of
the result metadata.

Why this works: LLMs occasionally produce wrong answers.  Running the same
prompt multiple times and taking the most common answer significantly reduces
random errors, especially for factual or mathematical queries.

LLM call flow:
  N independent reasoning calls (N = ``num_samples``, default 3)
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import structlog

from src.agent.llm import LLMClient
from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy

log = structlog.get_logger(__name__)

_EXTRACTION_PROMPT_TEMPLATE = """You are a precise answer extractor.

Given the following reasoning and answer text, extract ONLY the final answer as
a concise, self-contained string (no more than 2-3 sentences).

QUERY: {query}

RESPONSE TEXT:
{response_text}

Respond ONLY with valid JSON:
{{"final_answer": "the extracted answer here"}}"""


class SelfConsistencyStrategy(ReasoningStrategy):
    """Multiple independent reasoning runs with majority-vote aggregation.

    Args:
        num_samples:        Number of independent LLM runs (default 3).
        sample_temperature: Temperature for each sample run (default 0.7 –
                            higher than CoT to get diverse outputs).
        max_tokens:         Max tokens per LLM call.
    """

    def __init__(
        self,
        num_samples: int = 3,
        sample_temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> None:
        if num_samples < 1:
            raise ValueError("num_samples must be >= 1")
        self._num_samples = num_samples
        self._sample_temperature = sample_temperature
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "self_consistency"

    async def reason(
        self,
        query: str,
        context: str,
        llm_client: LLMClient,
    ) -> ReasoningResult:
        """Run N independent reasoning passes and aggregate by majority vote."""
        log.debug("sc.start", num_samples=self._num_samples, query_length=len(query))

        # ------------------------------------------------------------------ #
        # Step 1: Run N independent completions in parallel
        # ------------------------------------------------------------------ #
        base_prompt = f"""Answer the following query with clear, step-by-step reasoning.

CONTEXT (use if relevant):
{context[:3000] if context else "(none)"}

QUERY: {query}

Think through the problem carefully, then state your final answer clearly at the
end, prefixed with "FINAL ANSWER:".
"""

        messages = [
            {"role": "system", "content": "You are a careful reasoning assistant."},
            {"role": "user", "content": base_prompt},
        ]

        async def run_single_sample(sample_index: int) -> tuple[str, int]:
            """Run one completion and return (response_text, tokens)."""
            try:
                response = await llm_client.complete(
                    messages=messages,
                    temperature=self._sample_temperature,
                    max_tokens=self._max_tokens,
                )
                text = llm_client.extract_text(response)
                tokens = self._extract_tokens(response)
                log.debug("sc.sample_complete", sample=sample_index, tokens=tokens)
                return text, tokens
            except Exception as exc:
                log.error("sc.sample_failed", sample=sample_index, error=str(exc))
                return f"Sample {sample_index} failed: {exc}", 0

        # Run all samples concurrently
        sample_tasks = [run_single_sample(i) for i in range(self._num_samples)]
        sample_results: list[tuple[str, int]] = await asyncio.gather(*sample_tasks)

        sample_texts = [text for text, _ in sample_results]
        total_tokens = sum(tokens for _, tokens in sample_results)

        # ------------------------------------------------------------------ #
        # Step 2: Extract a normalised final answer from each sample
        # ------------------------------------------------------------------ #
        extracted_answers: list[str] = []

        async def extract_answer(sample_text: str) -> str:
            """Use LLM to extract a clean final answer from a raw sample."""
            # First try cheap heuristic: look for FINAL ANSWER: prefix
            for line in sample_text.splitlines():
                stripped = line.strip()
                if stripped.upper().startswith("FINAL ANSWER:"):
                    extracted = stripped[len("FINAL ANSWER:"):].strip()
                    if extracted:
                        return extracted

            # Fallback: ask the LLM to extract it
            try:
                extract_prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
                    query=query,
                    response_text=sample_text[:1500],
                )
                extract_messages = [
                    {"role": "system", "content": "You extract final answers. Always respond with valid JSON only."},
                    {"role": "user", "content": extract_prompt},
                ]
                extraction_response = await llm_client.complete(
                    messages=extract_messages,
                    temperature=0.0,
                    max_tokens=256,
                )
                nonlocal total_tokens
                total_tokens += self._extract_tokens(extraction_response)
                parsed = json.loads(llm_client.extract_text(extraction_response))
                return parsed.get("final_answer", sample_text[:200])
            except Exception:
                # Last resort: return truncated raw text
                return sample_text[:200].strip()

        # total_tokens is captured via nonlocal inside extract_answer; run
        # extractions sequentially to avoid shared-variable races.
        for sample_text in sample_texts:
            answer = await extract_answer(sample_text)
            extracted_answers.append(answer)

        # ------------------------------------------------------------------ #
        # Step 3: Majority vote
        # ------------------------------------------------------------------ #
        # Normalise for comparison: lowercase + strip punctuation/whitespace
        def normalise(text: str) -> str:
            import re
            return re.sub(r"\s+", " ", text.lower().strip().rstrip("."))

        normalised = [normalise(a) for a in extracted_answers]
        vote_counts = Counter(normalised)
        majority_normalised, majority_count = vote_counts.most_common(1)[0]

        # Find the original (non-normalised) answer corresponding to the majority
        majority_answer = extracted_answers[normalised.index(majority_normalised)]
        consistency_score = majority_count / len(extracted_answers)

        # Confidence is the consistency score scaled between 0.4 and 1.0
        # (even a single run gets 0.4 minimum confidence for having any answer)
        confidence = 0.4 + 0.6 * consistency_score

        log.info(
            "sc.complete",
            num_samples=self._num_samples,
            majority_count=majority_count,
            consistency_score=consistency_score,
            confidence=confidence,
            total_tokens=total_tokens,
        )

        # ------------------------------------------------------------------ #
        # Assemble result
        # ------------------------------------------------------------------ #
        steps_summary = [
            f"Run {i + 1}: {text[:120].replace(chr(10), ' ')}"
            for i, text in enumerate(sample_texts)
        ]
        steps_summary.append(
            f"Majority vote: {majority_count}/{self._num_samples} runs agreed "
            f"(consistency={consistency_score:.2f})"
        )

        return ReasoningResult(
            answer=majority_answer,
            confidence=confidence,
            steps=steps_summary,
            strategy_name=self.name,
            token_count=total_tokens,
            reasoning_chain=list(extracted_answers),
            metadata={
                "num_samples": self._num_samples,
                "consistency_score": consistency_score,
                "vote_distribution": dict(vote_counts),
                "all_answers": extracted_answers,
            },
        )
