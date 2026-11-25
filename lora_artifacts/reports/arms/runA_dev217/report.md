> **Historical decoding note.** This report was produced with **batch 8, left padding** (t14). It is superseded by the batch-1 serving ledger in `../paired_ood876` (dev: `../paired_dev216`) and must not be quoted as a serving number.

# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/runs/preds/runA_dev217.json`
- Baseline: none (single configuration)
- Generated: 2026-11-25T04:08:52+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `tcp:127.0.0.1:5444`
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 0 live queries, 512 cache hits, 0 execution errors, 0 skipped (offline)

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dev217` | primary | clean split | 216 | 216 | 216 | 0 | 87/216 = 40.28 % [34.0–46.9, Wilson] | 87/216 = 40.28 % | 87/216 = 40.28 % | 0.3789 (8) | no baseline |

EX/gold-executable is the headline; EX/all is shown only so the two denominators are never confused. A missing or non-executing prediction stays in EX/gold-executable as a wrong answer. A row whose contamination status is not `clean` is not a result, whatever its EX.

## Native-gold drift policy (t15)

Policy: a row whose native PostgreSQL gold cannot be compared with its BIRD SQLite original is **not scored** (the SQLite data is the official reference for dev); a reviewed correction stays usable as the corrected native gold but is flagged `translation_defect`. A shard where the SQLite originals are not available locally gets no drift audit, and says so instead of implying one.

| shard | denominator | before the policy | excluded | reviewed corrections | drift audit |
|---|---|---|---|---|---|
| `dev217` | 216 | 217 rows | 1 | 7 | applied (8 SQLite databases) |

The denominator column is the shard size after the policy (216 drift-clean, 241 historical, 876 OOD-876). EX is computed on the executable-gold denominator where it differs: `ex241` 209 of 241, `ood876` 811 of 876.

## `dev217` — PG-reviewed dev-216 (native PG gold, drift-clean)

- Reviewed dev answers; gold is native PostgreSQL (pg_gold_sql) and is executed verbatim.
- Denominator after the native-gold drift policy: 216 (question 309 is excluded, see the drift policy section); 217 appears only as the pre-policy size. 7 of the 216 rows are reviewed corrections.
- Do not compare a percentage here with EX/241: different gold and a different denominator.

**Contamination status: `clean` — clean split.** Result usable: **True**.

> arms A and B are the first runs trained only on datasets/native_pg/train_main.jsonl (train split of the t9 manifest, 6,499 rows, 1,018 reserved-OOD rows filtered by the loader)

> Finding on record: t9 found 1,018 reserve/OOD rows inside cluster/sft_all_v2.jsonl; train_main is that corpus with those rows removed, so a clean status is claimed against the hashed manifest

> Split manifest: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/datasets/splits/schema_family_split.json`, sha256 `65e6ca1c47060b5c08d7fb7999ff88ee4d848d659e24dafa08f7c7ad2a6cbb02`.

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/bird_pg_unified/data/bird_dev_pg.jsonl` field `pg_gold_sql`, rewrite `none`, sha256 `b333af44f3aa124d…`, criterion `execution_ex`.

### Native-gold drift policy

- Denominator after the policy: **216** (the shard file holds 217 rows; 1 excluded).
- Excluded: `309` (toxicology) — server_side_data_defect/superset_volume: the PostgreSQL toxicology database holds 12,333 atom rows against 9,111 in the BIRD SQLite original, so any count-based answer differs from the official BIRD gold by data volume alone; the gold also carries a reviewed correction (TR346). Source: cluster/drift_native.json (t15 native-gold drift audit).
  It scored 0 under the pre-policy harness; that point is **removed** from the corrected number.
- Before the drift policy: **87/217 = 40.09 %** — shown only as the pre-policy number, never as a result. After the policy: **87/216 = 40.28 %**.
- Reviewed corrections: **7 of 216** scored rows (`1496`, `1525`, `1529`, `1531`, `445`, `571`, `694`) are reviewed corrections, kept usable and flagged `translation_defect`; **209** rows are original SQLite-equivalent gold.
- Drift audit: exact match 197, legitimate engine difference 12, translation defect 7, server side data defect 1, not comparable 0 over 217 rows (8 SQLite databases compared).

### Denominators

| name | n | definition |
|---|---|---|
| `shard_size` | 216 | cases in the shard file; denominator of EX/all |
| `predictions_present` | 216 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 0 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 216 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 0 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 216 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 167 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 49 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 0 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, native gold)

Executed **216** of **216** gold queries (100.00 %); **0** unexecutable, removed from the EX denominator.

Historical SQLite rewrite applied to this native gold (offline check, no execution): it would alter **39** of 216 queries and break **24** of them at the PostgreSQL parse level — which is exactly why this shard's gold is executed verbatim.

Execution-level check on those 39 altered queries: **11** return exactly the native rows, **2** would change the answer, **26** would fail although native gold runs (denominator `gold queries altered by the historical rewrite`).

Predictions: **167** executed, **49** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 216); 0 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 33 |
| `undefined_function` | 6 |
| `syntax_error` | 4 |
| `sql_error` | 3 |
| `ambiguous_column` | 1 |
| `invalid_column_reference` | 1 |
| `undefined_table` | 1 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `california_schools` | 23 | 23 | 3 | 13.04 % | 13.04 % | 11 |
| `card_games` | 28 | 28 | 11 | 39.29 % | 39.29 % | 6 |
| `codebase_community` | 26 | 26 | 15 | 57.69 % | 57.69 % | 4 |
| `debit_card_specializing` | 19 | 19 | 4 | 21.05 % | 21.05 % | 3 |
| `financial` | 27 | 27 | 2 | 7.41 % | 7.41 % | 12 |
| `student_club` | 30 | 30 | 18 | 60.00 % | 60.00 % | 7 |
| `superhero` | 36 | 36 | 23 | 63.89 % | 63.89 % | 4 |
| `toxicology` | 27 | 27 | 11 | 40.74 % | 40.74 % | 2 |

Micro: 87/216 = 40.28 % (denominator `gold_executable`). Macro: 0.3789 over 8 of 8 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| challenging | 54 | 54 | 18 | 33.33 % |
| moderate | 79 | 79 | 28 | 35.44 % |
| simple | 83 | 83 | 41 | 49.40 % |

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/arms/runA_dev217/per_question_dev217.jsonl`

