# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/runs/preds/smoke_runA_dev217.json`
- Baseline: none (single configuration)
- Generated: 2026-11-25T03:26:53+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `127.0.0.1:5444`
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 1 live queries, 8 cache hits, 1 execution errors, 0 skipped (offline)

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dev217` | primary | clean split | 4 | 2 | 4 | 0 | 0/4 = 0.00 % [0.0–49.0, Wilson] | 0/4 = 0.00 % | 0/4 = 0.00 % | 0.0000 (1) | no baseline |

EX/gold-executable is the headline; EX/all is shown only so the two denominators are never confused. A missing or non-executing prediction stays in EX/gold-executable as a wrong answer. A row whose contamination status is not `clean` is not a result, whatever its EX.

## Native-gold drift policy (t15)

Policy: a row whose native PostgreSQL gold cannot be compared with its BIRD SQLite original is **not scored** (the SQLite data is the official reference for dev); a reviewed correction stays usable as the corrected native gold but is flagged `translation_defect`. A shard where the SQLite originals are not available locally gets no drift audit, and says so instead of implying one.

| shard | denominator | before the policy | excluded | reviewed corrections | drift audit |
|---|---|---|---|---|---|
| `dev217` | 4 | 4 rows | 0 | 0 | applied (8 SQLite databases) |

The denominator column is the shard size after the policy (216 drift-clean, 241 historical, 876 OOD-876). EX is computed on the executable-gold denominator where it differs: `ex241` 209 of 241, `ood876` 811 of 876.

## `dev217` — PG-reviewed dev-216 (native PG gold, drift-clean)

- Reviewed dev answers; gold is native PostgreSQL (pg_gold_sql) and is executed verbatim.
- Denominator after the native-gold drift policy: 216 (question 309 is excluded, see the drift policy section); 217 appears only as the pre-policy size. 7 of the 216 rows are reviewed corrections.
- Do not compare a percentage here with EX/241: different gold and a different denominator.

**Contamination status: `clean` — clean split.** Result usable: **True**.

> smoke test of the arm pipeline

> Finding on record: t9 reserved-OOD finding

> Split manifest: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/datasets/splits/schema_family_split.json`, sha256 `65e6ca1c47060b5c08d7fb7999ff88ee4d848d659e24dafa08f7c7ad2a6cbb02`.

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/bird_pg_unified/data/bird_dev_pg.jsonl` field `pg_gold_sql`, rewrite `none`, sha256 `b333af44f3aa124d…`, criterion `execution_ex`.

### Native-gold drift policy

- Denominator after the policy: **4** (the shard file holds 4 rows; 0 excluded).
- Before the drift policy: **0/4 = 0.00 %** — shown only as the pre-policy number, never as a result. After the policy: **0/4 = 0.00 %**.
- Reviewed corrections: **0 of 4** scored rows (none) are reviewed corrections, kept usable and flagged `translation_defect`; **4** rows are original SQLite-equivalent gold.
- Drift audit: exact match 197, legitimate engine difference 12, translation defect 7, server side data defect 1, not comparable 0 over 4 rows (8 SQLite databases compared).

### Denominators

| name | n | definition |
|---|---|---|
| `shard_size` | 4 | cases in the shard file; denominator of EX/all |
| `predictions_present` | 2 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 2 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 4 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 0 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 4 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 1 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 1 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 2 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, native gold)

Executed **4** of **4** gold queries (100.00 %); **0** unexecutable, removed from the EX denominator.

Historical SQLite rewrite applied to this native gold (offline check, no execution): it would alter **2** of 4 queries and break **0** of them at the PostgreSQL parse level — which is exactly why this shard's gold is executed verbatim.

Execution-level check on those 2 altered queries: **1** return exactly the native rows, **0** would change the answer, **1** would fail although native gold runs (denominator `gold queries altered by the historical rewrite`).

Predictions: **1** executed, **1** failed to execute, **2** missing, **0** returned the ambiguity marker (denominator: gold_executable = 4); 0 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 1 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `california_schools` | 4 | 4 | 0 | 0.00 % | 0.00 % | 3 |

Micro: 0/4 = 0.00 % (denominator `gold_executable`). Macro: 0.0000 over 1 of 1 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| moderate | 1 | 1 | 0 | 0.00 % |
| simple | 3 | 3 | 0 | 0.00 % |

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/arms/smoke_dev217/per_question_dev217.jsonl`

