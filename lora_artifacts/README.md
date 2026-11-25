# LoRA track artifacts

Everything reproducible from the night of 25.11.2026, except model weights.

## What is here

| Directory | Contents |
|---|---|
| `reports/` | run records (A/B, numerics), evaluation reports for all four shards, batch report, provenance audit, drift audit, blocked-task records |
| `predictions/` | id-level predictions for the A and B arms on dev-216 and OOD-876 |
| `splits/` | schema-family split manifest, decontamination tests, native-gold drift flags |
| `pg_hard/` | frozen PG-hard set v1: 96 questions on 12 new schemas, gold, hashes, changelog |
| `external/` | screened external sources with per-row provenance and licences |
| `judge/` | semantic judge, calibration, verdicts |
| `native_pg/` | loader, renderer, schema selector, report scripts (the large JSONL corpora are rebuilt by `loader.py`, not stored) |
| `INTERIM_REPORT.md` | current results, what is quotable, what is not |
| `TASK_PLAN.md`, `OVERNIGHT_LOG.md` | plan and the work diary |

## What is NOT here

**Adapter checkpoints.** Each one is 161 MB (7B) or 275 MB (14B), above GitHub's 100 MB
per-file limit, so they live on the owner's machine and on the cluster. Their hashes and
paths are recorded in `CHECKPOINTS.md`. Rebuilding them is deterministic from the recorded
configs and the corpus hashes.

**Corpora** (`train_main.jsonl` 20 MB, `native_pg_train.jsonl` 23 MB). They are rebuilt by
`native_pg/loader.py` from `datasets/bird_pg_unified` plus `splits/schema_family_split.json`;
the loader asserts the row counts (4,216 real + 2,691 synthetic, 1,018 filtered).
