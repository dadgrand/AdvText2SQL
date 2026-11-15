# BIRD in PostgreSQL: the dataset hand-off

`datasets/bird_pg/` is the ready-to-use artifact (Russian README with the full
reasoning: `datasets/bird_pg/README.ru.md`). This page is the short technical
summary for a reader of this repository.

## What ships

| Split | Rows | Guarantee |
|---|---:|---|
| `data/bird_dev_pg.jsonl` | 241 | every gold query **executed** on the course PostgreSQL |
| `data/bird_dev_clean.jsonl` | 228 | same row set as the original SQLite execution (7 of them after repairing a damaged join key) |
| `data/bird_dev_divergent.jsonl` | 13 | executes, but the two engines answer differently (see §Data defects) |
| `data/bird_train_pg.jsonl` | 5,230 | executed on PostgreSQL (no SQLite copy exists for these 63 databases) |
| `data/bird_train_excluded.jsonl` | 1,371 | the failures, each with its error |
| `data/synthetic_bird_like.jsonl` | 3,595 | synthetic BIRD-like pairs over **train** schemas only; `question_tier` = `hand` (3,504, written per question with schema+SQL+rows in view) or `model` (91, local 7B) |
| `schemas/` | 77 databases | runnable DDL + `schema.json` (types, PK/FK, row-count estimates) |
| `tools/verify_dataset.py`, `tools/to_sft.py` | — | integrity/live check, chat-SFT converter |
| `build/` | — | the builders that produced all of the above |

## The three numbers that matter

1. **241 of 241 dev gold queries execute.** The shipped
   `benchmarks/evaluate_bird.py` destroys 30 of them: its regex
   `(\b\w+\b)\s*/\s*(\b\w+\b)` → `\1 / NULLIF(\2,0)` fires *after* sqlglot has
   already emitted `NULLIF`, producing `NULLIF(NULLIF,0)(COUNT(...), 0)`, which
   PostgreSQL cannot parse — so gold never runs and every submission scores 0 on
   that question. Three more (ids 4, 35, 572) keep parsing but mean something
   else. The AST rewriter in `dev_tools/fixed_eval.py` fixes all of it; nothing is
   dropped for this reason in the dataset.
2. **216 of 241 return exactly the SQLite row set.** The other 25 diverge because
   the databases on the course server were loaded with typed columns (`bigint`,
   `numeric`) while BIRD's SQLite stores the same data as `TEXT`: text-vs-number
   comparison, lexicographic vs numeric `ORDER BY`, case-sensitive `LIKE`. Same
   schema drift that `db_schemas.drift.json` recorded in round 1.
3. **106 of 114 overlapping questions match BIRD's own `mini_dev_postgresql.json`** — and the drop from 109 is the point: seven `california_schools` questions now use the *repaired* join key, so they answer like BIRD's SQLite label instead of like the official PostgreSQL file, which on this server joins on keys that lost their leading zero.
   when both are executed on the server. The five disagreements are in
   `reports/official_crosscheck.json`; two of them are cases where BIRD's own
   PostgreSQL file answers differently from its SQLite gold, one is a `CAST`
   difference (`SUM` over an integer column), one is data typing, one is rounding.

## Two data defects found on the server (worth knowing before comparing numbers)

* **Lost leading zeros in join keys.** In `california_schools` 211 of 2,269 `satscores.cds`
  values are 13 characters long instead of 14 (the leading zero is gone), so
  `satscores.cds = frpm.CDSCode` matches **1,620** rows instead of **1,782**. Eight dev
  questions join on it and answer differently from the BIRD original; seven are repaired
  by casting both sides to `bigint` (field `pg_sql_repairs`), the eighth is a tie-break
  case. The per-table comparison for all eight dev databases is in
  `reports/drift_report.json`.
* **`toxicology` is a superset.** Its tables hold more rows than BIRD's original (numbers
  above), which alone explains every divergence in that database.

## One translation bug worth remembering

`SUBSTR(x, -2)` (SQLite: the last two characters) is rendered by sqlglot as
`SUBSTRING(x FROM -2)`, which PostgreSQL reads as "start at position -2" and
silently returns an empty string. Question 239 therefore returned `0` instead of
`498`. Fixed in `delivery/common.py` by rewriting negative `SUBSTR` starts into
`SUBSTRING(x FROM LENGTH(x) - 1)`; one case in dev, seven in train.

## Synthetic corpus

* Generated **only over the 69 train databases** — the 8 dev databases are
  excluded on purpose, so nothing derived from the dev schemas can leak into a
  team member's training run for the same dev benchmark.
* SQL: template synthesis over real schemas and real values mined from
  `pg_stats`, executed, 1–30 rows, deterministic `ORDER BY`, unique result set
  (1,000 of 1,279 attempts kept).
* Text: local `Qwen2.5-Coder-7B-Instruct` (4-bit), then a hard filter — valid
  JSON, 6–40 words, a real question mark, no SQL keywords, at most two raw
  identifiers, every SQL literal present in the text, 5-gram Jaccard < 0.6
  against real BIRD questions; a final pass checks that the interrogative matches
  the answer shape (a query returning `AVG(...)` must not be asked as "how many").
  the local-model tier kept 651 of 1,000; the hand tier then produced 3,504 accepted
  rows after the same gate, a curation screen that drops id-only questions, and a
  cap of two phrasings per SQL.
* Two tiers are shipped with a `question_tier` field: `hand` (written per question by
  a subagent that saw the schema, the SQL and the first result rows) and `model`
  (local 7B). Both passed the identical validation gate; the hand tier is the one to
  use for training.

## Reproduce

`datasets/bird_pg/build/README.md` lists the commands (`build.py --only dev`,
`--only train`, `synth.py`, `verbalize.py`, `crosscheck_official.py`,
`assemble.py`) and the four non-obvious traps that cost time: one connection per
worker thread (SSH channel budget), `rollback()` after every failed statement,
SQLite's missing statement timeout, and `pg_stats`' `anyarray` columns.
