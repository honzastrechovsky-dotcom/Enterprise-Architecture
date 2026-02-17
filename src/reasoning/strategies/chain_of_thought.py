"""Chain-of-Thought reasoning strategy.

Prompts the LLM to think step-by-step before committing to an answer, then
performs a self-verification pass to check the conclusion for internal
consistency.

LLM call flow (2 total):
  1. Reasoning call  – generates numbered steps + tentative answer (JSON)
  2. Verification call – checks the reasoning for logical errors (JSON)

Verification output is appended to the reasoning chain but does not change
the answer unless the verifier detects a fatal flaw (in which case
``confidence`` is penalised).
"""

from __future__ import annotations

import json

import structlog

from src.agent.llm import LLMClient
from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy

log = structlog.get_logger(__name__)


class ChainOfThoughtStrategy(ReasoningStrategy):
    """Step-by-step reasoning with an automatic verification step.

    Args:
        temperature: LLM temperature for reasoning call (default 0.3 –
                     low for deterministic chains).
        max_tokens:  Max tokens per LLM call.
    """

    def __init__(
        self,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> None:
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "chain_of_thought"

    async def reason(
        self,
        query: str,
        context: str,
        llm_client: LLMClient,
    ) -> ReasoningResult:
        """Execute Chain-of-Thought reasoning with verification."""
        log.debug("cot.start", query_length=len(query))
        total_tokens = 0

        # ------------------------------------------------------------------ #
        # Step 1: Generate the step-by-step reasoning chain
        # ------------------------------------------------------------------ #
        reasoning_prompt = f"""You are a precise reasoning assistant.

Answer the following query by thinking through it step-by-step.

CONTEXT (use this information if relevant):
{context[:3000] if context else "(none)"}

QUERY: {query}

Respond ONLY with valid JSON in this exact structure:
{{
    "reasoning_steps": [
        {{"step": 1, "thought": "...", "conclusion": "..."}},
        {{"step": 2, "thought": "...", "conclusion": "..."}}
    ],
    "final_answer": "Your complete, definitive answer here",
    "confidence": 0.0
}}

Rules:
- reasoning_steps: 3-7 steps, each with a clear thought and intermediate conclusion
- final_answer: the direct answer to the query based on your steps
- confidence: your confidence 0.0-1.0 that the answer is correct
- Respond ONLY with JSON, no additional text"""

        reasoning_messages = [
            {"role": "system", "content": "You are a precise analytical reasoner. Always respond with valid JSON only."},
            {"role": "user", "content": reasoning_prompt},
        ]

        reasoning_steps: list[dict] = []
        final_answer = ""
        raw_confidence = 0.5

        try:
            reasoning_response = await llm_client.complete(
                messages=reasoning_messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            total_tokens += self._extract_tokens(reasoning_response)
            reasoning_text = llm_client.extract_text(reasoning_response)

            parsed = json.loads(reasoning_text)
            reasoning_steps = parsed.get("reasoning_steps", [])
            final_answer = parsed.get("final_answer", "")
            raw_confidence = float(parsed.get("confidence", 0.5))

            log.debug("cot.reasoning_complete", steps=len(reasoning_steps), confidence=raw_confidence)

        except json.JSONDecodeError:
            log.warning("cot.reasoning_json_failed")
            reasoning_steps = []
            final_answer = "Unable to complete chain-of-thought reasoning (parse error)"
            raw_confidence = 0.2

        except Exception as exc:
            log.error("cot.reasoning_failed", error=str(exc))
            reasoning_steps = []
            final_answer = f"Reasoning failed: {exc}"
            raw_confidence = 0.0

        # ------------------------------------------------------------------ #
        # Step 2: Verification pass
        # ------------------------------------------------------------------ #
        steps_text = "\n".join(
            f"Step {s.get('step', i + 1)}: {s.get('thought', '')} => {s.get('conclusion', '')}"
            for i, s in enumerate(reasoning_steps)
        )

        verification_prompt = f"""Verify the following reasoning chain for logical consistency.

ORIGINAL QUERY: {query}

REASONING CHAIN:
{steps_text or "(no steps generated)"}

PROPOSED ANSWER: {final_answer}

Respond ONLY with valid JSON:
{{
    "is_consistent": true,
    "issues": ["issue 1 if any"],
    "verified_confidence": 0.0,
    "verification_note": "brief summary of verification"
}}

Rules:
- is_consistent: true if reasoning logically supports the answer
- issues: list any logical flaws, contradictions, or missing steps (empty list if none)
- verified_confidence: your confidence 0.0-1.0 in the final answer after review
- Respond ONLY with JSON, no additional text"""

        verification_messages = [
            {"role": "system", "content": "You are a logical verification assistant. Always respond with valid JSON only."},
            {"role": "user", "content": verification_prompt},
        ]

        verification_result: dict = {}
        final_confidence = raw_confidence

        try:
            verify_response = await llm_client.complete(
                messages=verification_messages,
                temperature=0.2,
                max_tokens=512,
            )
            total_tokens += self._extract_tokens(verify_response)
            verify_text = llm_client.extract_text(verify_response)

            verification_result = json.loads(verify_text)
            final_confidence = float(verification_result.get("verified_confidence", raw_confidence))

            log.debug(
                "cot.verification_complete",
                is_consistent=verification_result.get("is_consistent"),
                verified_confidence=final_confidence,
            )

        except json.JSONDecodeError:
            log.warning("cot.verification_json_failed")
            verification_result = {
                "is_consistent": None,
                "issues": ["Verification parse error"],
                "verified_confidence": raw_confidence * 0.9,
                "verification_note": "Verification failed to parse – confidence penalised",
            }
            final_confidence = raw_confidence * 0.9

        except Exception as exc:
            log.error("cot.verification_failed", error=str(exc))
            verification_result = {
                "is_consistent": None,
                "issues": [f"Verification error: {exc}"],
                "verified_confidence": raw_confidence * 0.8,
                "verification_note": "Verification errored – confidence penalised",
            }
            final_confidence = raw_confidence * 0.8

        # Penalise confidence if verification found issues
        issues = verification_result.get("issues", [])
        if issues:
            penalty = min(0.1 * len(issues), 0.3)
            final_confidence = max(0.0, final_confidence - penalty)

        # ------------------------------------------------------------------ #
        # Assemble result
        # ------------------------------------------------------------------ #
        steps_summary = [
            f"Step {s.get('step', i + 1)}: {s.get('thought', '')} → {s.get('conclusion', '')}"
            for i, s in enumerate(reasoning_steps)
        ]
        steps_summary.append(
            f"Verification: {verification_result.get('verification_note', 'completed')}"
        )

        reasoning_chain: list = list(reasoning_steps) + [{"verification": verification_result}]

        log.info(
            "cot.complete",
            answer_length=len(final_answer),
            confidence=final_confidence,
            total_tokens=total_tokens,
        )

        return ReasoningResult(
            answer=final_answer,
            confidence=final_confidence,
            steps=steps_summary,
            strategy_name=self.name,
            token_count=total_tokens,
            reasoning_chain=reasoning_chain,
            metadata={
                "verification_issues": issues,
                "is_consistent": verification_result.get("is_consistent"),
            },
        )
