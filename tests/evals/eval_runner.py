"""Evaluation runner for Enterprise Architecture golden dataset.

Loads golden_dataset.json, calls the agent API for each query, evaluates
responses against expected criteria, and produces a scored JSON report.

Usage:
    python tests/evals/eval_runner.py --api-url http://localhost:8000 --output results.json
    python tests/evals/eval_runner.py --dry-run  # validate dataset only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class GoldenEntry:
    """A single entry from the golden dataset."""

    id: str
    query: str
    expected_specialist: str
    expected_keywords: list[str]
    expected_data_sources: list[str]
    difficulty: str
    category: str
    verified_answer_summary: str


@dataclass
class QueryScore:
    """Evaluation score for a single query."""

    id: str
    query: str
    category: str
    difficulty: str
    expected_specialist: str
    actual_specialist: str | None = None
    specialist_match: bool = False
    keyword_coverage: float = 0.0
    keywords_found: list[str] = field(default_factory=list)
    keywords_missing: list[str] = field(default_factory=list)
    data_source_match: float = 0.0
    data_sources_found: list[str] = field(default_factory=list)
    response_quality: float = 0.0
    response_length: int = 0
    latency_ms: int = 0
    error: str | None = None
    response_text: str | None = None


@dataclass
class AggregateResults:
    """Aggregate evaluation results."""

    total_queries: int = 0
    specialist_accuracy: float = 0.0
    avg_keyword_coverage: float = 0.0
    avg_data_source_match: float = 0.0
    avg_response_quality: float = 0.0
    composite_score: float = 0.0
    pass_verdict: bool = False
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_difficulty: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dataset loading and validation
# ---------------------------------------------------------------------------

VALID_SPECIALISTS = {
    "document_analyst",
    "procedure_expert",
    "data_analyst",
    "quality_inspector",
    "maintenance_advisor",
    "generalist",
}

VALID_DIFFICULTIES = {"simple", "medium", "complex"}
VALID_CATEGORIES = {"procedure", "quality", "maintenance", "data", "document"}
VALID_DATA_SOURCES = {"rag", "sap", "mes"}


def load_dataset(path: Path) -> list[GoldenEntry]:
    """Load and parse the golden dataset JSON file."""
    with open(path) as f:
        raw = json.load(f)

    entries: list[GoldenEntry] = []
    for item in raw:
        entries.append(
            GoldenEntry(
                id=item["id"],
                query=item["query"],
                expected_specialist=item["expected_specialist"],
                expected_keywords=item["expected_keywords"],
                expected_data_sources=item["expected_data_sources"],
                difficulty=item["difficulty"],
                category=item["category"],
                verified_answer_summary=item["verified_answer_summary"],
            )
        )
    return entries


def validate_dataset(entries: list[GoldenEntry]) -> list[str]:
    """Validate dataset entries and return list of issues."""
    issues: list[str] = []
    seen_ids: set[str] = set()

    for entry in entries:
        # Unique IDs
        if entry.id in seen_ids:
            issues.append(f"{entry.id}: Duplicate ID")
        seen_ids.add(entry.id)

        # Valid specialist
        if entry.expected_specialist not in VALID_SPECIALISTS:
            issues.append(
                f"{entry.id}: Invalid specialist '{entry.expected_specialist}'. "
                f"Valid: {VALID_SPECIALISTS}"
            )

        # Valid difficulty
        if entry.difficulty not in VALID_DIFFICULTIES:
            issues.append(
                f"{entry.id}: Invalid difficulty '{entry.difficulty}'. "
                f"Valid: {VALID_DIFFICULTIES}"
            )

        # Valid category
        if entry.category not in VALID_CATEGORIES:
            issues.append(
                f"{entry.id}: Invalid category '{entry.category}'. "
                f"Valid: {VALID_CATEGORIES}"
            )

        # Valid data sources
        for ds in entry.expected_data_sources:
            if ds not in VALID_DATA_SOURCES:
                issues.append(
                    f"{entry.id}: Invalid data source '{ds}'. "
                    f"Valid: {VALID_DATA_SOURCES}"
                )

        # Non-empty fields
        if not entry.query.strip():
            issues.append(f"{entry.id}: Empty query")
        if not entry.expected_keywords:
            issues.append(f"{entry.id}: No expected keywords")
        if not entry.verified_answer_summary.strip():
            issues.append(f"{entry.id}: Empty verified_answer_summary")

    # Distribution check
    category_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    specialist_counts: dict[str, int] = {}

    for entry in entries:
        category_counts[entry.category] = category_counts.get(entry.category, 0) + 1
        difficulty_counts[entry.difficulty] = (
            difficulty_counts.get(entry.difficulty, 0) + 1
        )
        specialist_counts[entry.expected_specialist] = (
            specialist_counts.get(entry.expected_specialist, 0) + 1
        )

    if len(entries) != 50:
        issues.append(f"Expected 50 entries, found {len(entries)}")

    return issues


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------


def call_agent_api(
    query: str,
    api_url: str,
    tenant_id: str,
    user_id: str,
    api_key: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """Send a query to the agent chat API and return the response.

    Returns a dict with keys: response, agent_id, citations, model_used,
    latency_ms, error.
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Tenant-ID": tenant_id,
        "X-User-ID": user_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"message": query}

    try:
        start = time.monotonic()
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{api_url}/chat", json=payload, headers=headers)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            return {
                "response": "",
                "agent_id": None,
                "citations": [],
                "model_used": None,
                "latency_ms": elapsed_ms,
                "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
            }

        data = resp.json()
        return {
            "response": data.get("response", ""),
            "agent_id": data.get("agent_id") or _extract_agent_id(data),
            "citations": data.get("citations", []),
            "model_used": data.get("model_used"),
            "latency_ms": data.get("latency_ms", elapsed_ms),
            "error": None,
        }

    except Exception as exc:
        return {
            "response": "",
            "agent_id": None,
            "citations": [],
            "model_used": None,
            "latency_ms": 0,
            "error": str(exc),
        }


def _extract_agent_id(data: dict[str, Any]) -> str | None:
    """Try to extract agent_id from various response formats."""
    # Check nested structures
    for key in ("metadata", "debug", "trace"):
        nested = data.get(key, {})
        if isinstance(nested, dict) and "agent_id" in nested:
            return nested["agent_id"]
    return None


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def score_specialist_match(expected: str, actual: str | None) -> bool:
    """Check if the correct specialist handled the query."""
    if actual is None:
        return False
    # Normalize: strip suffixes like _agent, compare core names
    normalize = lambda s: s.replace("_agent", "").replace("-", "_").lower().strip()
    return normalize(expected) == normalize(actual)


def score_keyword_coverage(
    expected_keywords: list[str], response_text: str
) -> tuple[float, list[str], list[str]]:
    """Calculate what fraction of expected keywords appear in the response.

    Returns (coverage_ratio, found_keywords, missing_keywords).
    """
    if not expected_keywords:
        return 1.0, [], []

    response_lower = response_text.lower()
    found: list[str] = []
    missing: list[str] = []

    for kw in expected_keywords:
        # Allow partial matching for compound terms
        kw_lower = kw.lower()
        if kw_lower in response_lower:
            found.append(kw)
        else:
            # Try individual words for multi-word keywords
            words = kw_lower.split()
            if len(words) > 1 and all(w in response_lower for w in words):
                found.append(kw)
            else:
                missing.append(kw)

    coverage = len(found) / len(expected_keywords)
    return coverage, found, missing


def score_data_source_usage(
    expected_sources: list[str],
    response_text: str,
    citations: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    """Check if the response used the expected data sources.

    Heuristic: look for source indicators in the response text and citations.
    Returns (match_ratio, sources_found).
    """
    if not expected_sources:
        return 1.0, []

    response_lower = response_text.lower()
    found: list[str] = []

    source_indicators = {
        "rag": [
            "document",
            "doc:",
            "[doc:",
            "sop",
            "manual",
            "[quality:",
            "[manual:",
            "[sop:",
            "[p&id:",
        ],
        "sap": [
            "sap",
            "production order",
            "purchase",
            "cost center",
            "material master",
            "plant",
            "warehouse",
            "inventory",
            "odata",
        ],
        "mes": [
            "mes",
            "real-time",
            "realtime",
            "machine data",
            "cycle time",
            "oee",
            "spc",
            "control chart",
            "measurement",
        ],
    }

    for source in expected_sources:
        indicators = source_indicators.get(source, [])
        if any(ind in response_lower for ind in indicators):
            found.append(source)
        elif source == "rag" and citations:
            # If there are citations, RAG was likely used
            found.append(source)

    match_ratio = len(found) / len(expected_sources)
    return match_ratio, found


def score_response_quality_llm(
    query: str,
    response_text: str,
    expected_summary: str,
    rubric: str,
    judge_config: dict[str, Any],
) -> float:
    """Use an LLM as judge to score response quality (1-5).

    Falls back to heuristic scoring if LLM judge is not configured.
    """
    api_key = judge_config.get("api_key") or os.getenv("EVAL_JUDGE_API_KEY", "")
    api_base = judge_config.get("api_base", "")

    if not api_key:
        # Fallback to heuristic scoring
        return _heuristic_quality_score(query, response_text, expected_summary)

    model = judge_config.get("model", "gpt-4o-mini")
    temperature = judge_config.get("temperature", 0.1)
    max_tokens = judge_config.get("max_tokens", 512)

    judge_prompt = f"""You are evaluating an enterprise AI agent response for a manufacturing query.

{rubric}

**User Query:** {query}

**Expected Answer Summary:** {expected_summary}

**Actual Agent Response:**
{response_text[:3000]}

Respond with ONLY a JSON object: {{"score": <1-5>, "reasoning": "<brief explanation>"}}"""

    try:
        base_url = api_base or "https://api.openai.com/v1"
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a precise evaluation judge. Respond only with valid JSON.",
                        },
                        {"role": "user", "content": judge_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            # Extract JSON from response
            match = re.search(r"\{[^}]+\}", content)
            if match:
                result = json.loads(match.group())
                score = float(result.get("score", 3))
                return max(1.0, min(5.0, score))

    except Exception:
        pass

    # Fallback
    return _heuristic_quality_score(query, response_text, expected_summary)


def _heuristic_quality_score(
    query: str, response_text: str, expected_summary: str
) -> float:
    """Simple heuristic scoring when LLM judge is unavailable.

    Scores based on response length, keyword overlap with expected summary,
    and structural indicators.
    """
    if not response_text or len(response_text) < 20:
        return 1.0

    score = 2.0  # Base score for any non-trivial response

    # Length bonus (reasonable responses are 200-2000 chars)
    length = len(response_text)
    if length > 200:
        score += 0.5
    if length > 500:
        score += 0.5

    # Summary keyword overlap
    summary_words = set(expected_summary.lower().split())
    response_words = set(response_text.lower().split())
    overlap = len(summary_words & response_words)
    overlap_ratio = overlap / max(len(summary_words), 1)
    score += overlap_ratio * 1.5

    # Structure indicators (numbered lists, headers, citations)
    structure_patterns = [
        r"\d+\.\s",  # Numbered list
        r"\*\*[^*]+\*\*",  # Bold text
        r"\[.+?\]",  # Citations/references
        r"#{1,3}\s",  # Headers
    ]
    for pattern in structure_patterns:
        if re.search(pattern, response_text):
            score += 0.1

    return max(1.0, min(5.0, score))


# ---------------------------------------------------------------------------
# Evaluation pipeline
# ---------------------------------------------------------------------------


def evaluate_entry(
    entry: GoldenEntry,
    api_url: str,
    tenant_id: str,
    user_id: str,
    api_key: str,
    timeout: int,
    judge_config: dict[str, Any],
    rubric: str,
) -> QueryScore:
    """Evaluate a single golden dataset entry against the API."""
    score = QueryScore(
        id=entry.id,
        query=entry.query,
        category=entry.category,
        difficulty=entry.difficulty,
        expected_specialist=entry.expected_specialist,
    )

    # Call API
    result = call_agent_api(
        query=entry.query,
        api_url=api_url,
        tenant_id=tenant_id,
        user_id=user_id,
        api_key=api_key,
        timeout=timeout,
    )

    if result["error"]:
        score.error = result["error"]
        return score

    response_text = result["response"]
    score.response_text = response_text
    score.response_length = len(response_text)
    score.latency_ms = result["latency_ms"]
    score.actual_specialist = result["agent_id"]

    # Score: specialist match
    score.specialist_match = score_specialist_match(
        entry.expected_specialist, result["agent_id"]
    )

    # Score: keyword coverage
    coverage, found, missing = score_keyword_coverage(
        entry.expected_keywords, response_text
    )
    score.keyword_coverage = coverage
    score.keywords_found = found
    score.keywords_missing = missing

    # Score: data source usage
    ds_match, ds_found = score_data_source_usage(
        entry.expected_data_sources, response_text, result["citations"]
    )
    score.data_source_match = ds_match
    score.data_sources_found = ds_found

    # Score: response quality (LLM judge or heuristic)
    score.response_quality = score_response_quality_llm(
        query=entry.query,
        response_text=response_text,
        expected_summary=entry.verified_answer_summary,
        rubric=rubric,
        judge_config=judge_config,
    )

    return score


def compute_aggregates(
    scores: list[QueryScore],
    config: dict[str, Any],
) -> AggregateResults:
    """Compute aggregate metrics from individual query scores."""
    results = AggregateResults(total_queries=len(scores))

    if not scores:
        return results

    # Filter out errored queries for scoring
    valid = [s for s in scores if s.error is None]
    if not valid:
        return results

    # Specialist accuracy
    specialist_correct = sum(1 for s in valid if s.specialist_match)
    results.specialist_accuracy = specialist_correct / len(valid)

    # Average keyword coverage
    results.avg_keyword_coverage = sum(s.keyword_coverage for s in valid) / len(valid)

    # Average data source match
    results.avg_data_source_match = sum(s.data_source_match for s in valid) / len(valid)

    # Average response quality
    results.avg_response_quality = sum(s.response_quality for s in valid) / len(valid)

    # Composite score
    weights = config.get("scoring", {}).get(
        "weights",
        {
            "specialist_match": 0.30,
            "keyword_coverage": 0.25,
            "data_source_match": 0.20,
            "response_quality": 0.25,
        },
    )
    results.composite_score = (
        results.specialist_accuracy * weights["specialist_match"]
        + results.avg_keyword_coverage * weights["keyword_coverage"]
        + results.avg_data_source_match * weights["data_source_match"]
        + (results.avg_response_quality / 5.0) * weights["response_quality"]
    )

    # Pass/fail verdict
    thresholds = config.get("scoring", {}).get("thresholds", {})
    results.pass_verdict = (
        results.specialist_accuracy >= thresholds.get("specialist_accuracy_min", 0.85)
        and results.avg_keyword_coverage
        >= thresholds.get("keyword_coverage_min", 0.60)
        and results.avg_data_source_match
        >= thresholds.get("data_source_accuracy_min", 0.70)
        and results.avg_response_quality
        >= thresholds.get("response_quality_min", 3.5)
        and results.composite_score >= thresholds.get("composite_score_min", 0.75)
    )

    # By category breakdown
    categories: dict[str, list[QueryScore]] = {}
    for s in valid:
        categories.setdefault(s.category, []).append(s)

    for cat, cat_scores in categories.items():
        n = len(cat_scores)
        results.by_category[cat] = {
            "count": n,
            "specialist_accuracy": sum(1 for s in cat_scores if s.specialist_match) / n,
            "avg_keyword_coverage": sum(s.keyword_coverage for s in cat_scores) / n,
            "avg_response_quality": sum(s.response_quality for s in cat_scores) / n,
            "avg_latency_ms": sum(s.latency_ms for s in cat_scores) / n,
        }

    # By difficulty breakdown
    difficulties: dict[str, list[QueryScore]] = {}
    for s in valid:
        difficulties.setdefault(s.difficulty, []).append(s)

    for diff, diff_scores in difficulties.items():
        n = len(diff_scores)
        results.by_difficulty[diff] = {
            "count": n,
            "specialist_accuracy": sum(1 for s in diff_scores if s.specialist_match)
            / n,
            "avg_keyword_coverage": sum(s.keyword_coverage for s in diff_scores) / n,
            "avg_response_quality": sum(s.response_quality for s in diff_scores) / n,
            "avg_latency_ms": sum(s.latency_ms for s in diff_scores) / n,
        }

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(
    scores: list[QueryScore],
    aggregates: AggregateResults,
    config: dict[str, Any],
    include_responses: bool = True,
) -> dict[str, Any]:
    """Generate the final evaluation report as a JSON-serializable dict."""
    report: dict[str, Any] = {
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_queries": aggregates.total_queries,
            "errors": sum(1 for s in scores if s.error is not None),
        },
        "aggregate": asdict(aggregates),
        "per_query": [],
    }

    for s in scores:
        entry: dict[str, Any] = {
            "id": s.id,
            "query": s.query,
            "category": s.category,
            "difficulty": s.difficulty,
            "expected_specialist": s.expected_specialist,
            "actual_specialist": s.actual_specialist,
            "specialist_match": s.specialist_match,
            "keyword_coverage": round(s.keyword_coverage, 3),
            "keywords_found": s.keywords_found,
            "keywords_missing": s.keywords_missing,
            "data_source_match": round(s.data_source_match, 3),
            "data_sources_found": s.data_sources_found,
            "response_quality": round(s.response_quality, 2),
            "response_length": s.response_length,
            "latency_ms": s.latency_ms,
            "error": s.error,
        }
        if include_responses and s.response_text:
            entry["response_text"] = s.response_text[:2000]  # Truncate for report size
        report["per_query"].append(entry)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict[str, Any]:
    """Load eval config YAML file."""
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def print_summary(aggregates: AggregateResults) -> None:
    """Print a human-readable summary to stdout."""
    verdict = "PASS" if aggregates.pass_verdict else "FAIL"
    print("\n" + "=" * 60)
    print(f"  EVALUATION RESULTS: {verdict}")
    print("=" * 60)
    print(f"  Total queries:         {aggregates.total_queries}")
    print(f"  Specialist accuracy:   {aggregates.specialist_accuracy:.1%}")
    print(f"  Avg keyword coverage:  {aggregates.avg_keyword_coverage:.1%}")
    print(f"  Avg data source match: {aggregates.avg_data_source_match:.1%}")
    print(f"  Avg response quality:  {aggregates.avg_response_quality:.2f}/5.0")
    print(f"  Composite score:       {aggregates.composite_score:.3f}")
    print()

    if aggregates.by_category:
        print("  By Category:")
        for cat, metrics in sorted(aggregates.by_category.items()):
            print(
                f"    {cat:15s}  n={metrics['count']:2d}  "
                f"specialist={metrics['specialist_accuracy']:.0%}  "
                f"keywords={metrics['avg_keyword_coverage']:.0%}  "
                f"quality={metrics['avg_response_quality']:.1f}"
            )
        print()

    if aggregates.by_difficulty:
        print("  By Difficulty:")
        for diff, metrics in sorted(aggregates.by_difficulty.items()):
            print(
                f"    {diff:15s}  n={metrics['count']:2d}  "
                f"specialist={metrics['specialist_accuracy']:.0%}  "
                f"keywords={metrics['avg_keyword_coverage']:.0%}  "
                f"quality={metrics['avg_response_quality']:.1f}"
            )

    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run golden dataset evaluation against the Enterprise Architecture agent API."
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="Base URL for the agent API (default: from config or http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authentication (default: from config or EVAL_API_KEY env var)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output file for JSON results (default: from config or tests/evals/results.json)",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Path to golden dataset JSON (default: tests/evals/golden_dataset.json)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to eval config YAML (default: tests/evals/eval_config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate dataset only, do not call the API",
    )
    parser.add_argument(
        "--ids",
        nargs="*",
        help="Only run specific query IDs (e.g., --ids GD-001 GD-005)",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Only run queries for a specific category",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-query results as they complete",
    )

    args = parser.parse_args()

    # Resolve paths
    evals_dir = Path(__file__).parent
    config_path = Path(args.config) if args.config else evals_dir / "eval_config.yaml"
    dataset_path = (
        Path(args.dataset) if args.dataset else evals_dir / "golden_dataset.json"
    )

    # Load config
    config = load_config(config_path)
    api_config = config.get("api", {})
    scoring_config = config.get("scoring", {})
    judge_config = config.get("llm_judge", {})
    output_config = config.get("output", {})

    # Resolve settings with CLI overrides
    api_url = args.api_url or api_config.get("url", "http://localhost:8000")
    api_key = (
        args.api_key
        or api_config.get("api_key")
        or os.getenv("EVAL_API_KEY", "")
    )
    tenant_id = api_config.get("tenant_id", "00000000-0000-0000-0000-000000000099")
    user_id = api_config.get("user_id", "00000000-0000-0000-0000-000000000099")
    timeout = api_config.get("timeout_seconds", 60)
    delay = api_config.get("request_delay_seconds", 1.0)
    rubric = judge_config.get("rubric", "Score 1-5 based on accuracy and relevance.")
    output_path = Path(
        args.output
        or output_config.get("default_path", "tests/evals/results.json")
    )
    include_responses = output_config.get("include_responses", True)

    # Load dataset
    print(f"Loading dataset from {dataset_path}")
    try:
        entries = load_dataset(dataset_path)
    except Exception as exc:
        print(f"ERROR: Failed to load dataset: {exc}", file=sys.stderr)
        return 1

    print(f"Loaded {len(entries)} entries")

    # Validate
    issues = validate_dataset(entries)
    if issues:
        print(f"\nDataset validation found {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        if args.dry_run:
            return 1
        print("\nContinuing despite validation issues...")
    else:
        print("Dataset validation: OK")

    if args.dry_run:
        print("\n-- DRY RUN --")
        print("Dataset is valid. No API calls made.")

        # Print distribution summary
        cat_counts: dict[str, int] = {}
        diff_counts: dict[str, int] = {}
        spec_counts: dict[str, int] = {}
        for e in entries:
            cat_counts[e.category] = cat_counts.get(e.category, 0) + 1
            diff_counts[e.difficulty] = diff_counts.get(e.difficulty, 0) + 1
            spec_counts[e.expected_specialist] = (
                spec_counts.get(e.expected_specialist, 0) + 1
            )

        print(f"\nDistribution (n={len(entries)}):")
        print("  By category:", json.dumps(cat_counts, indent=4))
        print("  By difficulty:", json.dumps(diff_counts, indent=4))
        print("  By specialist:", json.dumps(spec_counts, indent=4))
        return 0

    # Filter entries if requested
    if args.ids:
        id_set = set(args.ids)
        entries = [e for e in entries if e.id in id_set]
        print(f"Filtered to {len(entries)} entries by ID")
    if args.category:
        entries = [e for e in entries if e.category == args.category]
        print(f"Filtered to {len(entries)} entries by category '{args.category}'")

    if not entries:
        print("No entries to evaluate after filtering.")
        return 0

    # Run evaluation
    print(f"\nRunning evaluation against {api_url}")
    print(f"Evaluating {len(entries)} queries...\n")

    scores: list[QueryScore] = []
    for i, entry in enumerate(entries, 1):
        if args.verbose:
            print(f"  [{i}/{len(entries)}] {entry.id}: {entry.query[:60]}...")

        query_score = evaluate_entry(
            entry=entry,
            api_url=api_url,
            tenant_id=tenant_id,
            user_id=user_id,
            api_key=api_key,
            timeout=timeout,
            judge_config=judge_config,
            rubric=rubric,
        )
        scores.append(query_score)

        if args.verbose:
            status = "ERROR" if query_score.error else "OK"
            match_str = "Y" if query_score.specialist_match else "N"
            print(
                f"           {status}  specialist={match_str}  "
                f"keywords={query_score.keyword_coverage:.0%}  "
                f"quality={query_score.response_quality:.1f}"
            )

        # Rate limit delay
        if i < len(entries):
            time.sleep(delay)

    # Compute aggregates
    aggregates = compute_aggregates(scores, config)

    # Print summary
    print_summary(aggregates)

    # Generate and save report
    report = generate_report(scores, aggregates, config, include_responses)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nDetailed results saved to {output_path}")

    return 0 if aggregates.pass_verdict else 1


if __name__ == "__main__":
    sys.exit(main())
