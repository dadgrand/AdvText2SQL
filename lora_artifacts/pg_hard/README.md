# PG-hard: the pre-closed hard test set (t16)

`dev-217` and `OOD-120` had already been used inside the project, and every captured BIRD schema
is already assigned to exactly one split by
[`schema_family_split.json`](../splits/schema_family_split.json). A credible final claim therefore
needs a set that **did not exist before it was frozen**. This directory is that set: twelve
hand-authored PostgreSQL schemas, ninety-six hand-authored analytical questions over them, and
gold answers executed on a real PostgreSQL instance, written once with hashes.

The playbook (`docs/playbook/POSTGRES_TEXT2SQL_LORA.md` §6) calls these "рукотворные PG-hard
задачи" and scores them as the share of questions where a prediction matches the oracle on **all**
fixtures.

## Artifacts

| file | what |
|---|---|
| `questions.jsonl` | 96 rows: `qid, db_id, skill, difficulty, question, evidence, comparison, tie_policy`. `question` and `evidence` are the student-visible surface; the rest is evaluation metadata. |
| `gold.jsonl` | 96 rows: `gold_sql`, `result_sha256` (under the question's comparison mode), `bag_sha256`, `ordered_sha256`, `n_rows`, `n_columns`, column names/types, `executed_twice`, and `fixtures[{fixture_id: "default", db_id}]`. |
| `manifest.json` | version, freeze block with file hashes, schema registry (stable `db_id`, lineage fingerprint, DDL hash, data hash, row counts), per-skill counts, server facts, changelog rule. |
| `report.json` | per-skill and per-database counts, measured executable/deterministic share, contamination checks against every trainable file and the closed eval registry, dropped-skill register. |
| `CHANGELOG.md` | one entry per frozen version. |
| `build_pg_hard.py` | rebuilds the databases, executes the gold twice, writes the artifacts, refuses to overwrite a frozen set. |
| `verify_pg_hard.py` | 68 acceptance checks against the live fixtures and the frozen files. |
| `schemas/*.py` | one module per domain: DDL, fixtures, questions, dropped skills, notes. |

## The set

12 schemas × 8 questions = 96. Every schema carries a fact table with `timestamptz`, a `jsonb`
attribute column and a `text[]` column, a history table for as-of questions, a parent/child
relationship, and enough related tables for fan-out and anti-join questions.

| skill (playbook §3) | questions |
|---|---:|
| grain, fan-out, anti-join | 12 |
| top-N and windows with ties | 12 |
| state as of a point in time | 12 |
| periods, timezone, intervals | 12 |
| window frames, cohorts | 13 |
| JSONB and arrays | 12 |
| compositional analytics | 12 |
| hierarchies | 11 |

Comparison modes: **73 bag** (multiset of rows, duplicates counted, order irrelevant) and
**23 ordered** (sequence). Every question states a `tie_policy`; every ordered question pins a
deterministic tie-break, and each ordered top-N has a genuine tie **at the selection cutoff** in
its fixtures (two rows tie for the last slot, the stated tie-break decides who is excluded).

`telecom` cannot support a hierarchy honestly: its network inventory is flat (a region label, no
parent relationship). The hierarchy question is **dropped, not weakened**, and replaced by a second
window question; the drop is recorded with its reason in `report.json → dropped_skills`. Every
other schema covers all eight skills.

Difficulty labels are the author's: 75 `challenging`, 21 `moderate`. No `simple` question is
included; PG-hard is meant to be hard.

PostgreSQL-native features used by the gold SQL (one question can use several;
`report.json → feature_usage` is generated from the frozen `gold.jsonl`):

| feature | questions |
|---|---:|
| `with_cte` | 21 |
| `recursive_cte` | 11 |
| `window_over` | 12 |
| `aggregate_filter` | 13 |
| `lateral_join` | 10 |
| `distinct_on` | 2 |
| `jsonb_access` | 12 |
| `array_or_jsonb_containment` | 11 |
| `array_literal` | 12 |
| `at_time_zone` | 12 |
| `range_frame` | 9 |
| `rows_frame` | 2 |
| `row_number` | 1 |
| `exists_subquery` | 20 |
| `recursive_union_all` | 10 |

`DISTINCT ON` appears in the two as-of questions that pick the latest history row per key;
`LATERAL` covers the other as-of questions, so both idioms are exercised.

## Freeze policy

`questions.jsonl` and `gold.jsonl` are written once. `manifest.json → freeze` holds their sha256.
A rebuild that would change either file **refuses to write** (exit 1) and leaves the live fixture
databases untouched, because the rebuild happens in `pghard_<slug>__stage` databases that are only
renamed into place after the artifacts are accepted. An intended change is published as:

```bash
# edit a schema module, then:
python datasets/pg_hard/build_pg_hard.py --new-version   # v+1 + CHANGELOG entry
```

## Fixtures

The set runs on a bundled PostgreSQL 16.2 instance (`pgserver`), datadir
`~/.cache/advtext2sql/pghard_pgdata` — persistent, **not** `/tmp` (override with `--pgdata` or
`PGHARD_PGDATA`) — one database per schema named `pghard_<slug>`, session timezone `UTC`,
`statement_timeout` 20 s. The role `pghard_readonly`
(`pg_read_all_data`, `default_transaction_read_only=on`) exists for evaluation:

```
host=~/.cache/advtext2sql/pghard_pgdata user=pghard_readonly password=pghard_readonly dbname=pghard_ride_hailing
```

The fixtures are small on purpose (452 rows over 63 tables): every question's answer is
hand-checkable, and the edge cases (ties, duplicate joins, NULLs, price/tariff changes, DST
boundaries, half-hour offsets, recursive prerequisites) are explicit in the data rather than
statistical.

## Rebuild and verify

```bash
python datasets/pg_hard/build_pg_hard.py --ensure    # after a reboot: start the instance on the existing datadir
python datasets/pg_hard/build_pg_hard.py             # if the datadir is gone: rebuild (idempotent, frozen hashes unchanged)
python datasets/pg_hard/build_pg_hard.py --verify    # re-execute frozen gold, compare the frozen hashes
python datasets/pg_hard/verify_pg_hard.py            # 68 acceptance checks
```

The databases live in the datadir, so a reboot only needs `--ensure`; a lost datadir is rebuilt
deterministically and reproduces `questions 429fdaec…` / `gold ed380a08…` byte-for-byte. Moving
the instance from `/tmp` to the cache directory was verified this way (version stayed 1, a second
build was a no-op). The evaluation harness consumes this set directly:
`cluster/eval_shards.py --shards pghard` reads `datasets/pg_hard/gold.jsonl` and connects through
the Unix socket.

The verifier recomputes from the live databases: frozen file hashes, 10-15 schemas × 8-12
questions, skill coverage, comparison/tie-policy presence, two-run determinism, gold hashes,
catalog fingerprints, row counts, schema overlap with the split manifest (no `db_id`, no
fingerprint, structure Jaccard < 0.5 against all 77 BIRD schemas) and question duplication
(exact normalised match and 5-gram Jaccard ≥ 0.8 against 1,260 registry questions and 35,573
trainable questions; measured worst Jaccard 0.0556).

## Contamination result

`report.json → contamination.verdict == "clean"`: all 12 `db_id`s are new, no lineage
fingerprint collides with the 80 split-manifest families, the closest structure Jaccard is 0.27
(`retail_orders` vs BIRD `retail_world`, generic table names only), and no question duplicates or
near-duplicates any of the 36,833 questions in the registry and the trainable pools. Table names
shared with BIRD schemas are listed as informational only; the decisive checks are the schema
fingerprint and the structure Jaccard.

## Evaluating predictions against this set

A predictions file is a mapping `{qid: sql}`. A question scores 1 only if the prediction returns
exactly the gold result under the question's comparison mode on **every** fixture listed in
`gold.jsonl`; questions whose gold does not execute are excluded from the denominator and
reported. The evaluation harness reads `gold.jsonl` with `gold_field = "pg_gold_sql"`-style
semantics: here the field is `gold_sql`, and `fixtures[].db_id` names the database to connect to,
so `cluster/eval_shards.py --pghard datasets/pg_hard/gold.jsonl` is the intended integration
(its `pghard` shard still points at the placeholder path and needs that one-line change plus a
`--db-host/--db-port` path for a Unix socket).

## Limits, stated plainly

* The schemas are synthetic but modelled on real analytics patterns; they are not a random sample
  of production PostgreSQL.
* Row counts are small; the difficulty is in the semantics, not in scale.
* The fixture instance is local (`pgserver`) on a persistent datadir, not the course server,
  because the course account is read-only. DDL and data are fully reproducible from
  `schemas/*.py`, and `manifest.json` records their hashes.
* Only the `telecom` hierarchy skill is dropped; all other skills have at least eleven questions.
