# PG-hard smoke run (pre-run-B)

**Label: `smoke, pre-run-B`.** These numbers exercise the harness wiring and the frozen
denominators on the PG-hard set. They are **not a model result** and must not be quoted as one:
no model has been run on PG-hard yet, and neither frozen predictions file contains a single
PG-hard answer. A real PG-hard number can only come from t14 run B onwards (clean split, own
inference run), scored with `--contamination pghard=clean --split-manifest <manifest>`.

## What was scored

| field | value |
|---|---|
| shard | `pghard` — frozen PG-hard v1 (`datasets/pg_hard/gold.jsonl`, gold sha256 `ed380a08…`) |
| set | 12 hand-authored schemas, 96 questions, one default fixture per question, all 96 gold executable |
| instance | bundled PostgreSQL 16.2 on a **Unix socket**, datadir `~/.cache/advtext2sql/pghard_pgdata` (persistent, not `/tmp`) |
| criterion | `raw_pass_at_1` (single default fixture) and `oracle_all_fixtures` — identical here because every question has one fixture |
| scoring time | 2026-11-25T03:29Z (smoke), harness `cluster/eval_shards.py` |

## Results

| run | predictions file | predictions present | raw pass@1 | oracle/all fixtures | label |
|---|---|---:|---:|---:|---|
| frozen 14B (t11 baseline file) | `repo/runs/t2/14b-ev/query_results.json` | **0 / 96** | 0/96 | 0/96 | `smoke, pre-run-B` |
| 7B base | `repo/runs/ood/ood-base7b-292/query_results.json` | **0 / 96** | 0/96 | 0/96 | `smoke, pre-run-B` |
| machinery self-check (gold fed back as prediction) | `pghard_selfcheck/gold_as_predictions.json` | 96 / 96 | **96/96 = 100 %** | 96/96 = 100 % | `machinery self-check, not a model result` |

Read the first two rows as **coverage, not accuracy**: both files are dev/OOD prediction files
whose ids are `4…1531` / `ood:<db>:<n>`, while PG-hard ids are `pghard-<slug>-NN`; not one id
matches. Raw pass@1 is therefore 0/96 by construction. The third row proves the opposite
direction — when a prediction *is* the oracle, the harness scores 96/96, so the denominator,
the fixture routing and the comparison-mode handling all resolve. That is the only thing this
smoke run establishes.

If the numbers are needed in a table, write them as:
`PG-hard (smoke, pre-run-B; no PG-hard predictions yet): 0/96 coverage — not a result.`

## Per-skill denominators (frozen set, measured on the fixture instance)

| skill | questions | gold executed | self-check correct |
|---|---:|---:|---:|
| `grain_fanout_antijoin` | 12 | 12 | 12 |
| `topn_ties` | 12 | 12 | 12 |
| `asof_state` | 12 | 12 | 12 |
| `periods_timezones` | 12 | 12 | 12 |
| `window_frames_cohorts` | 13 | 13 | 13 |
| `jsonb_arrays` | 12 | 12 | 12 |
| `compositional` | 12 | 12 | 12 |
| `hierarchies` | 11 | 11 | 11 |
| **total** | **96** | **96** | **96** |

Per-database denominators are in each report (`### By database`): 8 questions per schema family,
12 families, all executable.

## Artifacts

| directory | run |
|---|---|
| `pghard_smoke_14b-ev/` | frozen 14B predictions, smoke label |
| `pghard_smoke_7b-base/` | 7B base predictions, smoke label |
| `pghard_selfcheck/` | machinery self-check (not a model result); the gold-as-predictions file is kept next to the report as `gold_as_predictions.json` |

Each directory has `report.md`, `report.json` and `per_question_pghard.jsonl`. The run label is
stamped in the report header and in every shard section, and the contamination status is
`unknown` with `result_usable = false`, so the rows cannot be mistaken for results.

## Persistent instance (survives a reboot)

The fixture instance moved from `/tmp/pghard_pgdata` to
`~/.cache/advtext2sql/pghard_pgdata` (override: `--pgdata` / `PGHARD_PGDATA`). Rebuilding there
reproduced the frozen hashes byte-for-byte (`questions 429fdaec…`, `gold ed380a08…`), stayed
version 1, and a second build was a no-op — the set is idempotent.

After a reboot:

```bash
# 1. start the instance on the existing datadir (databases are already there)
python datasets/pg_hard/build_pg_hard.py --ensure

# 2. if the datadir is gone, rebuild it (deterministic, frozen hashes unchanged)
python datasets/pg_hard/build_pg_hard.py

# 3. verify the frozen content
python datasets/pg_hard/build_pg_hard.py --verify     # re-executes all 96 gold
python datasets/pg_hard/verify_pg_hard.py             # 68 checks: hashes, fingerprints, overlap
```

Then score PG-hard through the harness (socket, not TCP):

```bash
python cluster/eval_shards.py \
  --predictions <runB>/query_results.json \
  --shards pghard \
  --run-label "run B, clean split" \
  --contamination pghard=clean --split-manifest <manifest> \
  --out-dir cluster/eval_reports/runB_pghard
```

## Reproduce this smoke run

```bash
python cluster/eval_shards.py --predictions repo/runs/t2/14b-ev/query_results.json \
  --shards pghard --run-label "smoke, pre-run-B" --contamination pghard=unknown \
  --contamination-note "..." --out-dir cluster/eval_reports/pghard_smoke_14b-ev

python cluster/eval_shards.py --predictions repo/runs/ood/ood-base7b-292/query_results.json \
  --shards pghard --run-label "smoke, pre-run-B" --contamination pghard=unknown \
  --contamination-note "..." --out-dir cluster/eval_reports/pghard_smoke_7b-base
```
