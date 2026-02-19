# Evaluation Framework

Golden dataset evaluation for the Enterprise Architecture specialist agent system. Tests routing accuracy, response quality, and data source usage across all 5 specialist domains.

## Quick Start

### Validate the dataset (no API calls)

```bash
python tests/evals/eval_runner.py --dry-run
```

### Run the full evaluation

```bash
# Start the API server first
python tests/evals/eval_runner.py --api-url http://localhost:8000 --output results.json
```

### Run specific queries or categories

```bash
# Specific IDs
python tests/evals/eval_runner.py --ids GD-001 GD-011 GD-021

# By category
python tests/evals/eval_runner.py --category quality --verbose
```

## Files

| File | Purpose |
|------|---------|
| `golden_dataset.json` | 50 manufacturing queries with expected outcomes |
| `eval_runner.py` | Evaluation script that calls API and scores responses |
| `eval_config.yaml` | Configuration for API access, scoring thresholds, LLM judge |
| `results.json` | Output file (generated after a run) |

## Golden Dataset Structure

Each entry in `golden_dataset.json`:

```json
{
  "id": "GD-001",
  "query": "User's natural language question",
  "expected_specialist": "procedure_expert",
  "expected_keywords": ["keyword1", "keyword2"],
  "expected_data_sources": ["rag", "sap", "mes"],
  "difficulty": "simple|medium|complex",
  "category": "procedure|quality|maintenance|data|document",
  "verified_answer_summary": "What a correct answer should contain"
}
```

### Distribution

- 10 queries per specialist domain (50 total)
- Difficulty mix: simple (direct lookup), medium (some reasoning), complex (multi-step analysis)
- Categories map to specialists: procedure, quality, maintenance, data, document

### Valid Specialists

| ID | Agent Class | Domain |
|----|-------------|--------|
| `procedure_expert` | ProcedureExpertAgent | SOPs, safety, step-by-step procedures |
| `quality_inspector` | QualityInspectorAgent | SPC, Cpk, defects, quality reports |
| `maintenance_advisor` | MaintenanceAdvisorAgent | Troubleshooting, PM schedules, work orders |
| `data_analyst` | DataAnalystAgent | SAP data, KPIs, cost analysis, production orders |
| `document_analyst` | DocumentAnalystAgent | Document search, comparison, classification |

### Valid Data Sources

| Source | Description |
|--------|-------------|
| `rag` | Document retrieval (ingested documents, SOPs, manuals) |
| `sap` | SAP OData queries (production orders, inventory, cost centers) |
| `mes` | MES REST API (real-time machine data, SPC, cycle times) |

## Scoring Methodology

### 1. Specialist Match (weight: 30%)

Binary check: did the orchestrator route the query to the expected specialist agent? The agent_id from the API response is compared against `expected_specialist`.

### 2. Keyword Coverage (weight: 25%)

Percentage of `expected_keywords` that appear in the response text. Uses case-insensitive matching with support for multi-word keyword partial matching (all constituent words must appear).

### 3. Data Source Usage (weight: 20%)

Heuristic check for whether the response indicates usage of expected data sources. Looks for source-specific indicators in the response text and citations (e.g., "SAP", "production order" for sap; "SPC", "control chart" for mes).

### 4. Response Quality (weight: 25%)

LLM-as-judge scoring on a 1-5 scale using the rubric defined in `eval_config.yaml`. When no LLM judge API key is configured, falls back to heuristic scoring based on response length, keyword overlap with expected summary, and structural indicators.

### Composite Score

```
composite = (specialist_accuracy * 0.30) +
            (keyword_coverage * 0.25) +
            (data_source_match * 0.20) +
            (response_quality / 5.0 * 0.25)
```

### Pass/Fail Thresholds (configurable in eval_config.yaml)

| Metric | Minimum |
|--------|---------|
| Specialist routing accuracy | 85% |
| Average keyword coverage | 60% |
| Data source match rate | 70% |
| Average response quality | 3.5/5.0 |
| Composite score | 0.75 |

## Adding New Golden Queries

1. Add a new entry to `golden_dataset.json` with the next sequential ID (e.g., GD-051).
2. Use valid values for `expected_specialist`, `difficulty`, `category`, and `expected_data_sources`.
3. Choose realistic, specific keywords that a correct answer must mention.
4. Write `verified_answer_summary` describing what a correct response looks like.
5. Run `--dry-run` to validate the updated dataset.

```bash
python tests/evals/eval_runner.py --dry-run
```

## Configuring the LLM Judge

Set the judge API key via environment variable or in `eval_config.yaml`:

```bash
export EVAL_JUDGE_API_KEY="sk-..."
python tests/evals/eval_runner.py --verbose
```

Without a judge API key, the runner uses heuristic scoring (less accurate but functional).

## Interpreting Results

The output `results.json` contains:

- `metadata`: timestamp, total queries, error count
- `aggregate`: overall metrics, pass/fail verdict, breakdowns by category and difficulty
- `per_query`: individual scores for each query including keywords found/missing

Look at the `by_category` breakdown to identify which specialist domains need improvement. The `by_difficulty` breakdown shows if the system handles complex multi-step queries well.
