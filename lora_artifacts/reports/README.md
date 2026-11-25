# Four-shard evaluation artifacts (t11; native-gold drift policy from t32)

`cluster/eval_shards.py` is the harness; this directory holds its dry-run output.
Every percentage in every report carries the denominator it was computed on
(`report.json` → `shards.<key>.denominators`; `report.md` → the "Denominators" table).

## What is here

| path | what |
|---|---|
| `dryrun_14b-ev/report.md`, `report.json`, `per_question_*.jsonl` | dry run over the frozen `repo/runs/t2/14b-ev/query_results.json`: all four shards, gold executability, self-comparison (delta exactly zero) |
| `dryrun_ood-scale7b-v2/report.md`, `report.json`, `per_question_ood876.jsonl` | the same harness on the frozen OOD predictions (`repo/runs/ood/ood-scale7b-v2/`), native PG gold, paired against `repo/runs/ood/ood-base7b-292/` |
| `drift_policy.md` | the native-gold drift policy (t15 → t32): the excluded dev row, the 7 reviewed corrections, the denominators 216 / 241 and the OOD limitation |
| `pghard_smoke.md` + `pghard_smoke_14b-ev/`, `pghard_smoke_7b-base/`, `pghard_selfcheck/` | PG-hard smell test (t33): frozen 14B and 7B-base files scored on the frozen set, labelled **smoke, pre-run-B**, plus a gold self-check proving the criterion resolves |
| `../eval_cache/exec_cache.json` | execution cache (row digests, not rows), keyed by `(db_id, sql)` at cache version 2; it is what makes `--offline` replay and id-level pairing exact |

`python cluster/verify_eval_reports.py` re-checks the contract on these artifacts
(81 checks: four shards with named denominators, PG-hard wired to the frozen gold on its socket instance, the smoke label and the 96/96 self-check, native gold for dev217/ood876, measured gold
executability, self-comparison delta exactly zero with an interval containing zero, OOD
frozen-ledger match at id level, per-question ledger lengths, contamination statuses, and the
drift policy: 216 with the exclusion reason, 136/217 present only as the pre-policy number, the
7 reviewed corrections flagged, the OOD limitation stated).

## The self-comparison (acceptance)

Both dry runs score the same file as current and baseline, so every paired delta must be
exactly zero and the interval must contain zero — and the harness *checks* it, exiting
non-zero if not:

```
dev217  n=216  ΔEX = 0.00 %  95 % paired-bootstrap CI [0.00 %, 0.00 %]
ex241   n=209  ΔEX = 0.00 %  95 % paired-bootstrap CI [0.00 %, 0.00 %]
```

`mcnemar_exact_p = 1`, `b = c = 0`. The same check is in `--selftest` (44 checks, including
parity with `dev_tools.fixed_eval` for `wilson_ci`, `mcnemar_exact_p`, `paired_bootstrap_ci`
and `repo_rewrite`, and the drift-policy invariants).

## Native-gold drift policy (t32, applying t15)

`dev217` is the **drift-clean shard: 216 questions**. One row is excluded before any number is
quoted:

| excluded | database | why |
|---|---|---|
| `309` | `toxicology` | server-side data defect (superset volume): the PostgreSQL database holds **12,333** `atom` rows against **9,111** in the BIRD SQLite original, so any count-based answer differs from the official BIRD gold by data volume alone (the gold also carries a reviewed correction, TR346). Source: `cluster/drift_native.json` |

* **216** = drift-clean dev denominator (217 rows in the file; 217 is quoted **only** as the
  pre-policy size).
* **241** = historical `ex241` denominator, continuity only; the drift policy is not applied
  there, so 241 is explicitly not drift-clean.
* 7 of the 216 rows are reviewed corrections (`445`, `571`, `694`, `1496`, `1525`, `1529`, `1531`),
  kept usable and flagged `translation_defect`; 209 rows are original SQLite-equivalent gold.
* OOD-876 has **no SQLite originals locally** (train databases are not shipped), so no drift audit
  is possible there; native PG gold stays the reference and the 65 unexecutable ids stay listed.
* full policy, evidence and reproduce steps: [`drift_policy.md`](drift_policy.md).

## Dry run A — frozen 14b-ev predictions (mechanics + continuity)

| shard | gold | gold executed | EX/gold-executable | EX/all | micro | macro |
|---|---|---|---|---|---|---|
| `dev217` (native PG gold, drift-clean) | reviewed dev, **216** cases (217 rows − question 309) | **216 / 216** | **135/216 = 62.50 %** (Wilson 55.9–68.7) | 135/216 | 135/216 | 0.5997 over 8 dbs |
| `ex241` (historical evaluator) | `repo/data/bird_large.json`, 241 cases | **209 / 241** (32 unexec: 30 syntax_error, 2 undefined_column) | **130/209 = 62.20 %** (55.5–68.5) | 130/241 = 53.94 % | 130/209 | 0.5967 over 8 dbs |
| `ood876` (native PG gold) | 876 cases | **811 / 876** (65 unexec) | not computed — **0 predictions** in this file | — | — | — |
| `pghard` (frozen set, socket instance) | `datasets/pg_hard/gold.jsonl`, 96 cases | **96 / 96** | not computed — **0 predictions** in this file | — | — | — |

- The `ex241` row reproduces the frozen audited ledger **at id level**: `130/241`,
  `130/209`, `n_gold_unexecutable = 32`, and the same 32 unexecutable ids
  (`report.md` → "Frozen-ledger continuity").
- **dev217 before the drift policy was 136/217 = 62.67 %**; that number may appear only as the
  pre-policy value. After the policy it is **135/216 = 62.50 %** (question 309 was scored correct,
  so both the numerator and the denominator drop by one). Both are printed in `report.md`.
- `dev217` (216) and `ex241` (241) are different gold and different denominators. Their
  percentages are not comparable; only the paired delta with the same file is meaningful (and it
  is zero here).
- `ood876` in this file shows only the executable-gold denominator (811/876) plus the
  per-database breakdown; the file has no OOD predictions. Error categories:
  `undefined_function 31`, `undefined_column 22`, `sql_error 5`, `syntax_error 5`,
  `datatype_mismatch 1`, `invalid_text_representation 1`.
- `pghard` is wired to the frozen set (t33): `datasets/pg_hard/gold.jsonl` (gold sha256
  `ed380a08…`, 96 questions, 12 schema families) on a **bundled PostgreSQL 16.2 instance over a
  Unix socket**, not through the course tunnel. All 96 gold queries execute; this file has no
  PG-hard answers, so the row reports denominators only. Scoring criterion: a question scores 1
  only if the prediction matches the native PG oracle on **all** fixtures whose gold executes
  (one default fixture per question today, so raw pass@1 and the all-fixtures criterion coincide).
- PG-hard runs before t14 run B are **smoke, pre-run-B** and are kept in
  [`pghard_smoke.md`](pghard_smoke.md); they are not model results.

## PG-hard fixture instance: persistent location and reboot procedure

The fixtures live in `~/.cache/advtext2sql/pghard_pgdata` (override `--pghard-datadir` or
`PGHARD_PGDATA`), **not** `/tmp`, so a reboot does not discard them. The harness reaches it
through its Unix socket with the read-only role `pghard_readonly`; the databases are
`pghard_<slug>` and the frozen gold names them in `fixtures[].db_id`.

```bash
# after a reboot: start the existing instance (data are already in the datadir)
python datasets/pg_hard/build_pg_hard.py --ensure

# if the datadir is lost: rebuild it — deterministic, frozen hashes do not change
python datasets/pg_hard/build_pg_hard.py

# verify the frozen content (hashes + live gold)
python datasets/pg_hard/build_pg_hard.py --verify   # re-executes all 96 gold, compares the frozen hashes
python datasets/pg_hard/verify_pg_hard.py           # 68 checks: file hashes, catalog fingerprints, row counts, overlap
```

The rebuild was checked after the move: `questions 429fdaec…` and `gold ed380a08…` are unchanged,
the manifest stayed `version 1`, and a second build was a no-op.

## Dry run B — OOD-876 with native PG gold (mechanics only, contaminated run)

`repo/runs/ood/ood-scale7b-v2/query_results.json` vs `repo/runs/ood/ood-base7b-292/`:

- EX **620/811 = 76.45 %** (Wilson 73.4–79.2), EX/all 620/876 = 70.78 %, micro 620/811,
  macro 0.7344 over 10 databases; predictions: 761 executed, 50 failed.
- Paired vs the 7B base on the 292-id subsample: **ΔEX +74.01 pp** (95 % paired-bootstrap CI
  68.59–79.06), `n_paired = 277`, `b = 206`, `c = 1`, exact McNemar p = 2.02e-60.
- Native gold reproduces the frozen ledger exactly: 620/811, the same 65 unexecutable ids,
  0 ids only in the ledger and 0 only in this run.

**This row is not an OOD result.** The 7B scale adapter predates the dedupe fix
(t9: 1,018 rows from reserved OOD schemas — 857 on the ten OOD databases, 161 on
`simpson_episodes`). It exists to prove the native-gold path end to end and to reconcile the
old number, nothing else.

## Contamination gate (mandatory, per shard)

`--contamination` is required for every scored shard; values are `clean`,
`contaminated_run`, `unknown`, `no_shard`. `clean` additionally requires `--split-manifest`
and the harness hashes that file into the report — an OOD claim without a manifest hash is
refused. `result_usable` is true only for `clean`.

A real OOD-876 claim must therefore be produced as:

```bash
python cluster/eval_shards.py \
  --predictions <t14-runB>/query_results.json \
  --baseline <14b-old>/query_results.json \
  --shards ood876 \
  --contamination ood876=clean \
  --split-manifest <the split manifest run B was trained on> \
  --out-dir cluster/eval_reports/runB_ood876
```

## Two findings worth carrying into the meeting

1. **dev217 must not reuse the 241 gold pipeline.** Native `pg_gold_sql` executes 216/216 scored
   rows (217 rows in the file before the drift exclusion).
   The historical SQLite rewrite would alter 39 of those gold queries, of which **26 fail
   although native gold runs** and **2 would silently change the answer** (execution-level
   audit in the report). The 241 shard pays that cost on purpose: 32/241 gold queries are
   unexecutable there.
2. **Removing secondary transpilation changes no OOD-876 number.** On OOD-876 the historical
   rewrite alters 28 gold queries: 23 return identical rows, 0 change the answer, and the 5
   that fail also fail on native gold. Native gold and the old transpiled pipeline agree
   exactly (620/811, same 65 ids). So the playbook's premise holds for the 241 shard but not
   for OOD-876 — do not present a "fixed OOD denominator" that did not move. The gain of the
   native path on this track is on dev217, plus the auditable denominators themselves.

## Reproduce

```bash
# harness only, no database
python cluster/eval_shards.py --selftest

# dry run A (DB via the SSH tunnel; python research/advtext2sql/ssh_tunnel.py)
python cluster/eval_shards.py \
  --predictions repo/runs/t2/14b-ev/query_results.json --self-compare \
  --ledger repo/runs/t2/scores/14b-ev.json --ledger-shard ex241 \
  --shards dev217,ex241,ood876,pghard \
  --contamination dev217=unknown,ex241=unknown,ood876=contaminated_run,pghard=unknown \
  --contamination-note "frozen pre-dedupe-fix run: continuity/dry-run mechanics only" \
  --out-dir cluster/eval_reports/dryrun_14b-ev

# dry run B (OOD, native gold, contaminated run: mechanics only)
python cluster/eval_shards.py \
  --predictions repo/runs/ood/ood-scale7b-v2/query_results.json \
  --baseline repo/runs/ood/ood-base7b-292/query_results.json --shards ood876 \
  --contamination ood876=contaminated_run \
  --ledger repo/runs/ood/scores/ood-scale7b-v2.json --ledger-shard ood876 \
  --out-dir cluster/eval_reports/dryrun_ood-scale7b-v2

# PG-hard smoke (socket instance; frozen set)
python cluster/eval_shards.py \
  --predictions repo/runs/t2/14b-ev/query_results.json --shards pghard \
  --run-label "smoke, pre-run-B" --contamination pghard=unknown \
  --out-dir cluster/eval_reports/pghard_smoke_14b-ev

# PG-hard after run B (the only kind of row that may be quoted as a result)
python cluster/eval_shards.py \
  --predictions <runB>/query_results.json --shards pghard \
  --run-label "run B, clean split" \
  --contamination pghard=clean --split-manifest <manifest> \
  --out-dir cluster/eval_reports/runB_pghard

# replay without a database (from the cache)
python cluster/eval_shards.py --offline --predictions ... --shards dev217,ex241 \
  --contamination dev217=<status>,ex241=<status> --out-dir ...
```

Course shards use PostgreSQL 15.15 (Debian) through `127.0.0.1:5444`; PG-hard uses the bundled
PostgreSQL 16.2 on its Unix socket. `sqlglot 30.23.0`; 10 000 bootstrap resamples, seed 0.
