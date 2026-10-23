> Current experiment summaries and supported paths: [docs/README.md](docs/README.md) and [docs/project_structure.md](docs/project_structure.md). Research artifacts live under `research_results/`; the older workflow notes below are retained for context.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AdvText2SQL is a text-to-SQL benchmarking framework for evaluating student-written tools against BIRD and Ambrosia benchmarks. Students implement a `Text2SQLGenerator` and run it against remote PostgreSQL databases.

## Commands

### Setup
```bash
pip install uv
cp .env.example .env  # Fill in DB_USER, DB_PASS, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_NAME
# Start SSH tunnel to remote DB (required before running benchmarks):
bash local/start_tunnel.sh   # persistent tunnel with auto-reconnect watchdog
# or manually: ssh -N -L 5444:10.11.1.6:5444 user_name@lnsigo.mipt.ru -p2278
```

**`.env` format**: no spaces around `=`, no quotes around values. Both cause 401 auth errors:
```
LLM_API_KEY=your_key_here    # correct
LLM_API_KEY = "your_key"     # WRONG — spaces + quotes break parsing
```

### Running Benchmarks
```bash
uv run --env-file .env bird_benchmark.py        # SQL generation test
uv run --env-file .env ambrosia_benchmark.py    # Ambiguity detection test
uv run --env-file .env bird_evaluate_only.py    # Re-evaluate saved results (no LLM calls)
```

### Linting & Tests
```bash
ruff check .
pytest
```

## Architecture

### Benchmark Pipeline

`BenchmarkBIRD` / `BenchmarkAmbrosia` (in `benchmarks/`) runs three phases:

1. **build** — creates one `Text2SQLGenerator` instance per unique `db_id`, extracts schema, initializes system prompt
2. **predict** — iterates dataset questions, calls `tool.query(question)` → `ToolResponse`
3. **evaluate** — executes gold + predicted SQL on remote DB, compares result sets

Each LLM client is wrapped in `TokenTrackingClient` (`benchmarks/token_tracking.py`) for token aggregation.

### Tool Interface (what students implement)

`Text2SQLGenerator` in `src/adv_text2sql/mcp_servers/text2sql_tool/src/text2sql_implementation.py`:

```python
class Text2SQLGenerator:
    def __init__(self, db_uri: str, llm_client): ...
    def build(self): ...  # Extract schema, build system prompt
    def query(self, user_request: str) -> dict: ...
    # Must return: {"status": "success", "query": sql}
    #           or {"status": "ambiguous"}
    #           or {"status": "error"}
```

### Key Data Flow

```
query(user_request)
  → if FEAT_32:
      _taxonomy_stop_filter(query)  # regex pre-filter — returns False (not ambiguous) for
                                    # structural patterns like "and also list", "for each X,",
                                    # "in common", "shared between" — before any LLM call
      → if not stopped: _detect_ambiguity_by_taxonomy()  # LLM taxonomy (A1/A3 types)
  → else: _check_ambiguity()        # classic LLM ambiguity check
  → if ambiguous: return {"status": "ambiguous"}
  → for up to MAX_RETRIES:
      generate_sql()          # LLM generates SQL
      sanitize_sql()          # Fix known LLM output patterns
      _validate_sql()         # sqlglot parse + forbid DROP/DELETE/UPDATE
    → return {"status": "success", "query": sql}
```

**Taxonomy stop_filter** (`_taxonomy_stop_filter` static method) runs before every taxonomy LLM call. It catches ~31/40 false-positive patterns from the large Ambrosia dataset that the LLM prompt cannot reliably handle. Prompt-based STOP rules were tried and made results worse — regex is the right tool for structural syntactic patterns.

### SQL Validation & Sanitization

`evaluate_bird.py` transpiles SQLite → PostgreSQL using sqlglot before execution. The generator also uses sqlglot to validate and sanitize LLM output:
- Wraps `SELECT DISTINCT ... ORDER BY` in subquery (PostgreSQL requirement)
- Forbids DROP, DELETE, UPDATE
- Converts SQLite-style casts
- Strips `TO_DATE()` on DATE columns (PostgreSQL rejects it — column already is DATE)
- Adds `NULLS LAST` / `NULLS FIRST` to `ORDER BY` (PostgreSQL default is opposite to SQLite)
- Injects `::NUMERIC` into `ROUND(expr, n)` (PostgreSQL rejects `double precision` first arg)
- Lowercases simple quoted identifiers (`"cardKingdomId"` → `"cardkingdomid"`) — columns are created unquoted/lowercase

### Prompts

All LLM prompts are in `prompts.py` and written **in Russian** (the target domain is Russian banking analytics). There are four templates: system, SQL generation, ambiguity detection, and SQL verification.

### Schema Extraction

Two modes in `text2sql_implementation.py`:
- `_get_db_schema_light()` — table/column names + types only (fast, low token cost)
- `_get_db_schema_heavy()` — adds sample rows, unique values, cardinality (high token cost)

### MCP Server

`src/adv_text2sql/mcp_servers/text2sql_tool/main.py` wraps the generator as a FastMCP tool for use as an MCP server. This is separate from the benchmark pipeline.

## Important Constraints

- Remote DB is always accessed via SSH tunnel on `localhost:5444`
- All column data in the DB is TEXT type — explicit casting required for numeric/date comparisons
- Only SELECT queries are permitted (enforced by `_validate_sql()`)
- Student branches should be created off `main` and never pushed directly to `main`
- `MAX_RETRIES` defaults to 7; overridable via env var

### Connection Pool (large datasets)

With 55 databases built simultaneously, default SQLAlchemy pool_size=5 exhausts server connections. Use:
```python
create_engine(db_uri, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_timeout=30)
```
Server `max_connections=400` (raised by admin). Our footprint: 55 DBs × 5 connections = 275 max.

### Result Set Comparison

`evaluate_bird.py` compares result sets via `set()`. PostgreSQL can return `list` (arrays) or `dict` (JSONB) column values which are unhashable. Use `_to_hashable()` to recursively convert before set comparison:
```python
def _to_hashable(v):
    if isinstance(v, list): return tuple(_to_hashable(i) for i in v)
    if isinstance(v, dict): return tuple(sorted((k, _to_hashable(val)) for k, val in v.items()))
    return v
```
Both `evaluate_bird.py` and `bird_evaluate_only.py` have this fix applied.

## Ablation Feature Flags

Feature flags gate the schema/stats enrichment in `Text2SQLGenerator.build()`. They are read at **module import time** — not per-call — so they must be set before any `uv run`.

FEAT_N maps 1:1 to Practice #N in `practices_doc.md`. Flags are read at **module import time** — must be set before `uv run`.

| Flag | Default | Practice | Effect when `true` |
|------|---------|----------|--------------------|
| `FEAT_1`  | `true`  | #1  | FK/PK relationships in prompt |
| `FEAT_2`  | `true`  | #2  | Column statistics (min/max/top-K) |
| `FEAT_3`  | `true`  | #3  | Regex type detection for TEXT columns (requires FEAT_2) |
| `FEAT_4`  | `true`  | #4  | `pg_stat` row count (requires FEAT_2) |
| `FEAT_5`  | `true`  | #5  | Light schema (table/column names + types) |
| `FEAT_6`  | `false` | #6  | Heavy schema — sample rows + enums (off by default) |
| `FEAT_7`  | `true`  | #7  | Compact stats formatting (requires FEAT_2) |
| `FEAT_8`  | `true`  | #8  | Rollback guards on failed PG transactions |
| `FEAT_9`  | `true`  | #9  | Dump `db_schemas.json` after build phase |
| `FEAT_10` | `true`  | #10 | `build()` timing + prompt-size log |
| `FEAT_11` | `true`  | #11 | JSON structured logging to file |
| `FEAT_12` | `true`  | #12 | Optimistic ambiguity fallback — generate SQL even when flagged ambiguous |
| `FEAT_13` | `true`  | #13 | Few-shot examples in ambiguity prompt |
| `FEAT_14` | `true`  | #14 | Strict SQL rules in generation prompt |
| `FEAT_15` | `true`  | #15 | `sanitize_sql` clean-up (markdown, DISTINCT, CAST fixes) |
| `FEAT_16` | `true`  | #16 | sqlglot validate + forbid DROP/DELETE/UPDATE |
| `FEAT_17` | `true`  | #17 | Retry loop up to `MAX_RETRIES` |
| `FEAT_18` | `true`  | #18 | Exponential backoff on 429 rate-limit |
| `FEAT_19` | `true`  | #19 | `asyncio.sleep` throttle between benchmark questions |
| `FEAT_20` | `true`  | #20 | LLM-as-judge verify in retry loop |
| `FEAT_27` | `true`  | #27 | True self-correction — pass error text into retry prompt |
| `FEAT_29` | `true`  | #29 | Complexity-based prompt selection (simple/complex) |
| `FEAT_32` | `false` | #32 | AmbiSQL Stage 1: taxonomy detection (A1-A6) |
| `FEAT_33` | `false` | #33 | AmbiSQL Stage 2a: clarification questions (requires FEAT_32) |
| `FEAT_34` | `false` | #34 | AmbiSQL Stage 2b: query rewriting / auto-resolve (requires FEAT_33) |
| `FEAT_35` | `true`  | #35 | TSV schema format (30–40% fewer tokens vs JSON) |
| `FEAT_36` | `true`  | #36 | Smart LIMIT 101 injection (N+1 pagination guard) |
| `FEAT_25` | `false` | #25 | Per-question schema pruning — 1 extra LLM call to drop irrelevant tables |
| `FEAT_26` | `false` | #26 | learnt_hints — persist successful SQL patterns in `hints/<db_id>.json` |
| `FEAT_28` | `false` | #28 | Self-consistency — 3 SQL samples + majority vote (3× token cost) |
| `FEAT_21` | `false` | #21 | MinHash column similarity — implicit join hints (extra DB queries at build) |
| `FEAT_23` | `false` | #23 | NL table descriptions via LLM — cached in `nl_descriptions/{db_id}.json` |
| `FEAT_30` | `false` | #30 | CoT decomposition — break complex queries into sub-steps (1 extra LLM call) |

**Critical**: flags default to `"true"` unless noted. Any flag absent from the environment is treated as enabled. To run ablation, all flags must be explicitly set. Exceptions: FEAT_6/32/33/34/25/26/28 default `false` (experimental or expensive).

### Ablation methodology (v2 — leave-one-in)

**Baseline = all FEAT flags false** (equivalent to main branch behavior). Each feature is tested with ONLY that flag enabled. Intra-feature dependencies are kept (FEAT_3/4/7 require FEAT_2; FEAT_33 requires FEAT_32; FEAT_34 requires FEAT_32+33).

Code guarantees that all-false is a working state:
- `build()`: always falls back to light schema even when FEAT_5/6/35 all false
- `AMBIGUITY_PROMPT_SIMPLE`: has core "default=unambiguous" rules (not a bare prompt)
- Benchmarks: always sleep 2s base; FEAT_19 adds configurable extra interval on top

```bash
# One feature at a time:
bash local/run_single.sh "FEAT_2" bird
bash local/run_single.sh "FEAT_2 FEAT_3" bird   # FEAT_3 depends on FEAT_2
bash local/run_single.sh baseline both           # all-false baseline

# Large dataset ablation (33 features × 2 benchmarks = 66 runs, ~4 days):
bash local/ablation_large_bird.sh
bash local/ablation_large_ambrosia.sh

# Baseline for large dataset (saves to research_results/ablation_results_large/feat_baseline/):
bash local/run_baseline_large.sh both
```

Small-dataset results → `ablation_results/<feat_name>/`. Large-dataset results → `research_results/ablation_results_large/<feat_name>_bird/` and `<feat_name>_ambrosia/`. Runs are skipped if `summary.txt` already exists.

**Checkpoint/resume**: `bird.py` and `ambrosia.py` save predictions to `query_results.json` after every question. On restart, completed question IDs are skipped. The marker files `.ablation_current_feat` / `.ablation_current_feat_ambrosia` tell the ablation script whether to keep or clear the checkpoint when resuming. When running `both` benchmarks, `query_results.json` is cleared between Ambrosia and BIRD runs to prevent cross-contamination of question IDs.

### Feature Attribution Dataset (Phase 0.2)

```bash
# Build per-question attribution matrices from ablation results:
python scripts/build_feature_dataset.py \
  --large --benchmark bird \
  --ablation-dir research_results/ablation_results_large \
  --data-file data/bird_large.json \
  --out data/feature_labels_large.json

python scripts/build_feature_dataset.py \
  --large --benchmark ambrosia \
  --ablation-dir research_results/ablation_results_large \
  --data-file data/ambrosia_large.json \
  --out data/feature_labels_ambrosia_large.json
```

Output: `data/feature_labels_large.json` (241×33 BIRD) and `data/feature_labels_ambrosia_large.json` (240×33 Ambrosia). Each entry records `feature_scores[FEAT_N] = 1` if that feature correctly answered the question. Used to train a multilabel classifier for dynamic feature selection (Phase 2 roadmap).

The script looks for `research_results/ablation_results_large/feat_baseline/` as the all-false reference. For Ambrosia, scores are recomputed from `ambrosia_query_results.json` (no per-question dump in logs); for BIRD, parsed from the printed results list in `bird.log`.

### Additional env vars

- `MAX_RETRIES` — SQL generation retries (default: 7; use 5 for ablation)
- `LLM_MIN_INTERVAL` — extra seconds added when FEAT_19=true (default: 3)
- `LLM_BASE_INTERVAL` — always-on floor sleep between questions (default: 2; use **15** for ablation to avoid 429 rate limits)
- `DATASET_SIZE` — `small` or `large`, controls which data file is loaded (default: `large` in `run_single.sh`)

### run_single.sh env var priority

`run_single.sh` strips `LLM_BASE_INTERVAL`, `MAX_RETRIES`, and `DATASET_SIZE` from the `.env` copy and writes them explicitly with shell-variable-or-default logic:
```bash
LLM_BASE_INTERVAL=${LLM_BASE_INTERVAL:-15}
MAX_RETRIES=${MAX_RETRIES:-5}
DATASET_SIZE=${DATASET_SIZE:-large}
```
**Priority**: shell export > script default. Values in `.env` are ignored for these three keys. To override, export before running:
```bash
DATASET_SIZE=small bash local/run_single.sh "FEAT_2 FEAT_3" both
```

## Best Known Feature Combination (ablation v2)

Best small dataset results (LLM_BASE_INTERVAL=15, MAX_RETRIES=5):
- **BIRD small**: 54.55% | **Ambrosia small**: 95.83%

```
FEAT_2 FEAT_3 FEAT_4 FEAT_8 FEAT_12 FEAT_17 FEAT_18 FEAT_19 FEAT_27 FEAT_30 FEAT_32 FEAT_33 FEAT_35
```

FEAT_29 (complexity routing) is neutral — omitting saves 1 LLM call/question.  
FEAT_32+33 (taxonomy) requires `_taxonomy_stop_filter` to avoid FP over-triggering on large dataset.

Large dataset results with same combination:
- **BIRD large**: 44.8% (108/241) | **Ambrosia large**: ~71-80%

BIRD breakdown by difficulty (large): simple 58.2%, moderate 34.8%, challenging 26.2%.

## Baseline Accuracy (reference)
- **BIRD large** all-false baseline: **38.2%** (92/241)
- **Ambrosia large** all-false baseline: **55.9%** (127/227, 3 DBs consistently fail due to server init race)
- BIRD: ~20–35% with gpt-3.5-turbo

### Top single-feature gains over baseline (large dataset)
| Feature | BIRD +Δ | Ambrosia +Δ |
|---------|---------|-------------|
| FEAT_14 (strict SQL rules) | +25 q | +6 q |
| FEAT_30 (CoT decomposition) | +19 q | +5 q |
| FEAT_2 (column stats) | +15 q | +30 q |
| FEAT_26 (learnt hints) | +15 q | +5 q |
| FEAT_13 (few-shot) | +10 q | +40 q |
| FEAT_35 (TSV format) | **−7 q** (below baseline in isolation) | +13 q |

FEAT_35 hurts BIRD in isolation but helps in combination with FEAT_2 — nonlinear interaction.
