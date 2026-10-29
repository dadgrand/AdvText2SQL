"""Offline tests for the schema-linking variant sweep (``dev_tools/schema_link_sweep.py``).

No database, no model, no network: the sweep's measurement path is pure Python over a
schema fixture and a couple of questions, and the sample-values renderer is exercised
with a hand-written sample cache. What is pinned is the *shape* of the deliverable:
the 24-cell grid, deterministic cells, the recall/token measurement, the honest
"EX blocked" marker (so an unmeasured cell can never look measured), and the sample
renderer adding text only for columns that are actually in the block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from dev_tools import schema_link_sweep as sweep  # noqa: E402

SCHEMA = (
    "TABLE schools\n"
    "  - CDSCode (TEXT)\n"
    "  - School Name (TEXT)\n"
    "  - County (TEXT)\n"
    "  - City (TEXT)\n"
    "\n"
    "TABLE satscores\n"
    "  - cds (TEXT)\n"
    "  - AvgScrMath (INTEGER)\n"
    "  - AvgScrRead (INTEGER)\n"
)

DEV = [
    {
        "question_id": 1,
        "db_id": "fixture",
        "question": "What is the average SAT math score of schools in Alameda County?",
        "evidence": "AvgScrMath is the math score",
        "SQL": "SELECT AVG(`AvgScrMath`) FROM satscores",
    },
    {
        "question_id": 2,
        "db_id": "fixture",
        "question": "List the counties of all schools.",
        "evidence": "",
        "SQL": "SELECT County FROM schools",
    },
]
SCHEMAS = {"fixture": SCHEMA}


def test_grid_is_the_documented_24_cells() -> None:
    cells = sweep.build_cells()
    assert len(cells) == 24
    ids = [cell["id"] for cell in cells]
    assert len(set(ids)) == 24, "cell ids must be unique"
    for required in (
        "k6-tables-min1-samples0",
        "k6-columns-min1-samples3",
        "k12-columns-min0-samples0",
        "k20-tables-min0-samples3",
    ):
        assert required in ids, required
    assert {cell["k"] for cell in cells} == {6, 12, 20}
    assert {cell["mode"] for cell in cells} == {"tables", "columns"}
    assert {cell["min_score"] for cell in cells} == {0.0, 1.0}
    assert {cell["samples"] for cell in cells} == {0, 3}


def test_measure_cell_scores_recall_and_fallbacks() -> None:
    cell = {"id": "fixture-cell", "k": 6, "mode": "tables", "min_score": 1.0, "samples": 0}
    row = sweep.measure_cell(cell, schemas=SCHEMAS, dev=DEV, samples_cache={})
    assert row["questions"] == 2
    assert row["fallbacks"] == 0
    assert row["gold_table_recall_count"] == "2/2"
    assert row["gold_table_recall"] == 1.0
    assert row["schema_tokens_mean"] and row["schema_tokens_mean"] < row["baseline_tokens_mean"]
    assert row["token_share_of_baseline"] < 1.0
    assert row["tables_mean"] >= 1.0  # every question selects at least its gold table


def test_measure_cell_is_deterministic() -> None:
    cell = {"id": "fixture-cell", "k": 12, "mode": "columns", "min_score": 0.0, "samples": 0}
    first = sweep.measure_cell(cell, schemas=SCHEMAS, dev=DEV, samples_cache={})
    second = sweep.measure_cell(cell, schemas=SCHEMAS, dev=DEV, samples_cache={})
    assert first == second


def test_measure_cell_counts_a_fallback_when_nothing_matches() -> None:
    dev = [{**DEV[0], "question": "zzz qqq", "evidence": "", "SQL": "SELECT 1 FROM satscores"}]
    cell = {"id": "fallback", "k": 6, "mode": "tables", "min_score": 1.0, "samples": 0}
    row = sweep.measure_cell(cell, schemas=SCHEMAS, dev=dev, samples_cache={})
    assert row["fallbacks"] == 1
    assert row["token_share_of_baseline"] == pytest.approx(1.0), "fallback sends the baseline block"
    assert row["gold_table_recall_count"] == "0/1"


def test_render_with_samples_only_touches_columns_in_the_block() -> None:
    block = "TABLE schools\n  - CDSCode (TEXT)\n  - School Name (TEXT)"
    samples = {"schools": {"CDSCode": ["A1", "B2"], "Unused": ["x"]}}
    rendered = sweep.render_with_samples(block, samples)
    lines = rendered.splitlines()
    assert lines[1].endswith("-- e.g. A1, B2")
    assert "-- e.g." not in lines[2], "a column without samples is untouched"
    assert "Unused" not in rendered


def test_render_with_samples_without_a_cache_changes_nothing() -> None:
    block = "TABLE schools\n  - CDSCode (TEXT)"
    assert sweep.render_with_samples(block, {}) == block


def test_markdown_marks_ex_as_blocked_and_never_invents_it() -> None:
    cells = [sweep.baseline_cell(DEV, SCHEMAS)]
    cells.append(
        sweep.measure_cell(
            {"id": "c1", "k": 6, "mode": "tables", "min_score": 1.0, "samples": 0},
            schemas=SCHEMAS,
            dev=DEV,
            samples_cache={},
        )
    )
    table = sweep.markdown_table(cells, sample_measured=False)
    assert "EX/241" in table
    assert table.count("blocked") == 1
    assert "— (baseline)" in table
    # no EX number can appear: the only numbers in the EX column are the markers
    for line in table.splitlines()[2:]:
        ex_column = line.rsplit("|", 2)[1].strip()
        assert ex_column == "— (blocked: no endpoint)" or ex_column == "— (baseline)"


def test_emit_commands_carries_the_env_switch_per_cell(tmp_path: Path) -> None:
    cells = sweep.build_cells()[:2]
    text = sweep.emit_commands(cells, tmp_path / "schemas.json")
    for cell in cells:
        assert cell["id"] in text
        assert f'TEXT2SQL_SCHEMA_LINK="{cell["mode"]}"' in text
        assert f'TEXT2SQL_SCHEMA_LINK_K="{cell["k"]}"' in text
    assert "run_benchmark" in text and "--allow-llm-spend" in text
    assert "min per cell" in text


def test_gold_tables_parses_sqlite_identifiers() -> None:
    assert sweep.gold_tables("SELECT AVG(`AvgScrMath`) FROM `satscores`") == {"satscores"}
    assert sweep.gold_tables("not sql at all") == set()
