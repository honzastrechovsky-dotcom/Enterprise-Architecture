"""Tree-of-Thought (ToT) reasoning strategy.

Implements a beam-search-inspired tree exploration:

1. **Generate** K candidate approaches (``num_branches``, default 3)
2. **Expand** each approach by generating the next reasoning step
3. **Score** each branch via LLM self-evaluation (0-10)
4. **Prune** branches below threshold (keep top ``beam_width``, default 2)
5. **Repeat** for ``max_depth`` levels (default 2)
6. **Conclude**: take the highest-scored surviving branch as the final answer

LLM call count (approximate):
  1 (generate approaches)
  + K * max_depth (expand branches)
  + K * max_depth (score branches)
  + 1 (final conclusion from best path)
  ≈ 1 + 2 * K * max_depth + 1
  = 16 calls for defaults (K=3, depth=2)

All scoring calls run in parallel per depth level to keep latency manageable.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import structlog

from src.agent.llm import LLMClient
from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy

log = structlog.get_logger(__name__)


@dataclass
class _Branch:
    """Internal representation of one branch in the reasoning tree."""

    approach: str                   # Initial approach description
    steps: list[str] = field(default_factory=list)  # Expansion steps accumulated
    score: float = 5.0              # LLM-assigned score (0-10, higher is better)
    alive: bool = True              # False after pruning


class TreeOfThoughtStrategy(ReasoningStrategy):
    """Branch-and-prune tree search over reasoning paths.

    Args:
        num_branches: Number of initial candidate approaches to generate (K).
        max_depth:    Number of expansion/scoring/pruning cycles (D).
        beam_width:   Branches kept alive after each pruning step.
        temperature:  LLM temperature for generation calls.
        max_tokens:   Max tokens per LLM call.
    """

    def __init__(
        self,
        num_branches: int = 3,
        max_depth: int = 2,
        beam_width: int = 2,
        temperature: float = 0.5,
        max_tokens: int = 1024,
    ) -> None:
        if num_branches < 1:
            raise ValueError("num_branches must be >= 1")
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        self._num_branches = num_branches
        self._max_depth = max_depth
        self._beam_width = min(beam_width, num_branches)
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "tree_of_thought"

    async def reason(
        self,
        query: str,
        context: str,
        llm_client: LLMClient,
    ) -> ReasoningResult:
        """Execute Tree-of-Thought reasoning and return the best path."""
        log.debug(
            "tot.start",
            num_branches=self._num_branches,
            max_depth=self._max_depth,
            query_length=len(query),
        )
        total_tokens = 0

        # ------------------------------------------------------------------ #
        # Step 1: Generate initial candidate approaches
        # ------------------------------------------------------------------ #
        generate_prompt = f"""You are a strategic reasoning planner.

Given the following query, generate {self._num_branches} distinct approaches to
answer it.  Each approach should represent a meaningfully different strategy or
angle.

CONTEXT (use if relevant):
{context[:2000] if context else "(none)"}

QUERY: {query}

Respond ONLY with valid JSON:
{{
    "approaches": [
        {{"id": 1, "approach": "Approach description here"}},
        {{"id": 2, "approach": "Another distinct approach"}},
        {{"id": 3, "approach": "Yet another approach"}}
    ]
}}

Respond ONLY with JSON, no additional text."""

        generate_messages = [
            {"role": "system", "content": "You are a strategic planning assistant. Always respond with valid JSON only."},
            {"role": "user", "content": generate_prompt},
        ]

        branches: list[_Branch] = []

        try:
            gen_response = await llm_client.complete(
                messages=generate_messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            total_tokens += self._extract_tokens(gen_response)
            gen_text = llm_client.extract_text(gen_response)
            parsed = json.loads(gen_text)

            for item in parsed.get("approaches", [])[:self._num_branches]:
                branches.append(_Branch(approach=item.get("approach", "")))

            log.debug("tot.branches_generated", count=len(branches))

        except Exception as exc:
            log.error("tot.generate_failed", error=str(exc))
            # Fallback: single generic branch
            branches = [_Branch(approach=f"General approach to: {query}")]

        # Ensure we have at least one branch
        if not branches:
            branches = [_Branch(approach=f"Direct reasoning about: {query}")]

        # ------------------------------------------------------------------ #
        # Phases 2-4 (repeated max_depth times): Expand → Score → Prune
        # ------------------------------------------------------------------ #
        for depth in range(self._max_depth):
            alive_branches = [b for b in branches if b.alive]
            if not alive_branches:
                break

            # --- Expand: generate next reasoning step for each alive branch ---
            async def expand_branch(branch: _Branch, depth_idx: int) -> int:
                """Expand one branch by one reasoning step.  Returns tokens used."""
                prior_steps = "\n".join(f"- {s}" for s in branch.steps) or "(none yet)"
                expand_prompt = f"""Continue reasoning for the following approach.

QUERY: {query}
APPROACH: {branch.approach}
PRIOR STEPS TAKEN:
{prior_steps}
DEPTH: {depth_idx + 1} of {self._max_depth}

Produce the NEXT reasoning step that advances this approach toward answering
the query.  Be concrete and specific.

Respond ONLY with valid JSON:
{{"next_step": "the reasoning step here"}}"""

                expand_messages = [
                    {"role": "system", "content": "You are a step-by-step reasoner. Always respond with valid JSON only."},
                    {"role": "user", "content": expand_prompt},
                ]
                try:
                    resp = await llm_client.complete(
                        messages=expand_messages,
                        temperature=self._temperature,
                        max_tokens=512,
                    )
                    toks = self._extract_tokens(resp)
                    parsed_step = json.loads(llm_client.extract_text(resp))
                    branch.steps.append(parsed_step.get("next_step", ""))
                    return toks
                except Exception as exc:
                    log.warning("tot.expand_failed", depth=depth_idx, error=str(exc))
                    branch.steps.append(f"(expansion error at depth {depth_idx + 1})")
                    return 0

            expand_tokens = await asyncio.gather(
                *[expand_branch(b, depth) for b in alive_branches]
            )
            total_tokens += sum(expand_tokens)

            # --- Score: LLM self-evaluates each alive branch (parallel) ---
            async def score_branch(branch: _Branch) -> int:
                """Score a branch 0-10.  Returns tokens used."""
                steps_text = "\n".join(f"- {s}" for s in branch.steps) or "(none)"
                score_prompt = f"""Evaluate the following reasoning path for answering the query.

QUERY: {query}
APPROACH: {branch.approach}
REASONING STEPS SO FAR:
{steps_text}

Score this reasoning path from 0-10 based on:
- Relevance to the query (0-4 points)
- Logical correctness so far (0-3 points)
- Likely to lead to a correct final answer (0-3 points)

Respond ONLY with valid JSON:
{{"score": 7.5, "rationale": "brief justification"}}"""

                score_messages = [
                    {"role": "system", "content": "You are a reasoning evaluator. Always respond with valid JSON only."},
                    {"role": "user", "content": score_prompt},
                ]
                try:
                    resp = await llm_client.complete(
                        messages=score_messages,
                        temperature=0.2,
                        max_tokens=256,
                    )
                    toks = self._extract_tokens(resp)
                    parsed_score = json.loads(llm_client.extract_text(resp))
                    branch.score = float(parsed_score.get("score", 5.0))
                    return toks
                except Exception as exc:
                    log.warning("tot.score_failed", error=str(exc))
                    branch.score = 5.0  # Neutral score on error
                    return 0

            score_tokens = await asyncio.gather(
                *[score_branch(b) for b in alive_branches]
            )
            total_tokens += sum(score_tokens)

            # --- Prune: kill branches below the beam width threshold ---
            alive_branches.sort(key=lambda b: b.score, reverse=True)
            for i, branch in enumerate(alive_branches):
                if i >= self._beam_width:
                    branch.alive = False

            log.debug(
                "tot.depth_complete",
                depth=depth + 1,
                alive=sum(1 for b in branches if b.alive),
                scores=[round(b.score, 2) for b in alive_branches],
            )

        # ------------------------------------------------------------------ #
        # Final step: Conclude from best surviving branch
        # ------------------------------------------------------------------ #
        surviving = [b for b in branches if b.alive]
        if not surviving:
            # All pruned (shouldn't happen, but be defensive)
            surviving = sorted(branches, key=lambda b: b.score, reverse=True)

        best_branch = max(surviving, key=lambda b: b.score)
        steps_text = "\n".join(f"- {s}" for s in best_branch.steps) or "(none)"

        conclude_prompt = f"""Based on the following reasoning path, provide a final, definitive answer.

QUERY: {query}
CONTEXT:
{context[:1500] if context else "(none)"}
BEST APPROACH: {best_branch.approach}
REASONING STEPS:
{steps_text}

Respond ONLY with valid JSON:
{{
    "final_answer": "Complete answer to the query here",
    "confidence": 0.0
}}

Rules:
- final_answer: a complete, well-reasoned answer
- confidence: 0.0-1.0 confidence in this answer
- Respond ONLY with JSON, no additional text"""

        conclude_messages = [
            {"role": "system", "content": "You are a reasoning conclusion specialist. Always respond with valid JSON only."},
            {"role": "user", "content": conclude_prompt},
        ]

        final_answer = ""
        final_confidence = 0.5

        try:
            conclude_response = await llm_client.complete(
                messages=conclude_messages,
                temperature=0.3,
                max_tokens=self._max_tokens,
            )
            total_tokens += self._extract_tokens(conclude_response)
            conclude_parsed = json.loads(llm_client.extract_text(conclude_response))
            final_answer = conclude_parsed.get("final_answer", "")
            final_confidence = float(conclude_parsed.get("confidence", 0.5))

        except Exception as exc:
            log.error("tot.conclude_failed", error=str(exc))
            final_answer = " ".join(best_branch.steps) or "Unable to form conclusion"
            final_confidence = best_branch.score / 10.0

        # Blend branch score into confidence: final = 0.6*llm_confidence + 0.4*(score/10)
        blended_confidence = 0.6 * final_confidence + 0.4 * (best_branch.score / 10.0)

        log.info(
            "tot.complete",
            best_score=best_branch.score,
            blended_confidence=blended_confidence,
            total_tokens=total_tokens,
        )

        # ------------------------------------------------------------------ #
        # Assemble result
        # ------------------------------------------------------------------ #
        steps_summary: list[str] = []
        for branch in branches:
            status = "BEST" if branch is best_branch else ("pruned" if not branch.alive else "survived")
            steps_summary.append(
                f"[{status} score={branch.score:.1f}] {branch.approach}"
            )
            for j, step in enumerate(branch.steps, 1):
                steps_summary.append(f"  Step {j}: {step}")

        reasoning_chain = [
            {
                "approach": b.approach,
                "steps": b.steps,
                "score": b.score,
                "survived": b.alive,
                "is_best": b is best_branch,
            }
            for b in branches
        ]

        return ReasoningResult(
            answer=final_answer,
            confidence=blended_confidence,
            steps=steps_summary,
            strategy_name=self.name,
            token_count=total_tokens,
            reasoning_chain=reasoning_chain,
            metadata={
                "num_branches": self._num_branches,
                "max_depth": self._max_depth,
                "beam_width": self._beam_width,
                "best_branch_score": best_branch.score,
                "branch_scores": [b.score for b in branches],
            },
        )
