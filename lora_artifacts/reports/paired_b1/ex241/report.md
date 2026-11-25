# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/preds/ex241_B.json`
- Baseline: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/preds/ex241_A.json`
- Generated: 2026-11-25T05:00:35+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `tcp:127.0.0.1:5444`
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 0 live queries, 691 cache hits, 0 execution errors, 0 skipped (offline)

> **None of the rows below is a clean-split result.** Contaminated or unknown rows are harness mechanics and continuity checks only; they must not be quoted as OOD/result numbers. A usable row requires `contamination=clean` and a recorded split-manifest hash.

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ex241` | continuity_only | not certified clean | 241 | 241 | 209 | 32 | 99/209 = 47.37 % [40.7–54.1, Wilson] | 99/241 = 41.08 % | 99/209 = 47.37 % | 0.4538 (8) | +7.66 % (209) |

EX/gold-executable is the headline; EX/all is shown only so the two denominators are never confused. A missing or non-executing prediction stays in EX/gold-executable as a wrong answer. A row whose contamination status is not `clean` is not a result, whatever its EX.

## Native-gold drift policy (t15)

Policy: a row whose native PostgreSQL gold cannot be compared with its BIRD SQLite original is **not scored** (the SQLite data is the official reference for dev); a reviewed correction stays usable as the corrected native gold but is flagged `translation_defect`. A shard where the SQLite originals are not available locally gets no drift audit, and says so instead of implying one.

| shard | denominator | before the policy | excluded | reviewed corrections | drift audit |
|---|---|---|---|---|---|
| `ex241` | 241 | 241 rows | 0 | 0 | not applied (continuity) |

The denominator column is the shard size after the policy (216 drift-clean, 241 historical, 876 OOD-876). EX is computed on the executable-gold denominator where it differs: `ex241` 209 of 241, `ood876` 811 of 876.

## `ex241` — Historical EX/241 (own evaluator, continuity only)

- Historical shard: SQLite-dialect gold scored through the repo's own repo_rewrite (sqlglot sqlite->postgres plus the shipped NULLIF regex). Continuity with the frozen ledger only; this gold pipeline is NOT used for any other shard.
- Contains the 217 reviewed dev ids (216 of them scored after the drift policy) plus 24 excluded ids; the denominators differ from dev217.

**Contamination status: `unknown` — not certified clean.** Result usable: **False**.

> historical continuity shard, own evaluator; batch-1 serving contract (t4); not a result.

> Split manifest: none recorded (no clean-split claim is made for this row).

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/data/bird_large.json` field `SQL`, rewrite `repo_rewrite`, sha256 `54e57841bf5bf142…`, criterion `execution_ex`.

### Native-gold drift policy

- **Drift policy: not applied here.** historical continuity shard: it keeps the pre-policy 241 denominator so the frozen ledger stays comparable. Its drift classes are recorded by t15 (9 server-side data defects, 5 translation defects, 13 legitimate engine differences) but are not applied here; 241 is explicitly not drift-clean.

### Denominators

| name | n | definition |
|---|---|---|
| `shard_size` | 241 | cases in the shard file; denominator of EX/all |
| `predictions_present` | 241 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 0 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 209 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 32 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 209 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 181 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 28 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 0 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, historical rewrite — continuity)

Executed **209** of **241** gold queries (86.72 %); **32** unexecutable, removed from the EX denominator.

| gold error category | n |
|---|---|
| `syntax_error` | 30 |
| `undefined_column` | 2 |

This shard's own evaluator applies the historical rewrite on purpose (continuity with the frozen ledger): it rewrites **128** of 241 queries and **28** stop parsing as PostgreSQL under it — the cost the other shards do not pay.

Predictions: **181** executed, **28** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 209); 32 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 23 |
| `undefined_function` | 3 |
| `syntax_error` | 2 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `california_schools` | 24 | 20 | 2 | 10.00 % | 8.33 % | 6 |
| `card_games` | 31 | 24 | 11 | 45.83 % | 35.48 % | 4 |
| `codebase_community` | 27 | 23 | 12 | 52.17 % | 44.44 % | 3 |
| `debit_card_specializing` | 26 | 21 | 9 | 42.86 % | 34.62 % | 1 |
| `financial` | 29 | 26 | 9 | 34.62 % | 31.03 % | 10 |
| `student_club` | 33 | 33 | 21 | 63.64 % | 63.64 % | 3 |
| `superhero` | 36 | 30 | 22 | 73.33 % | 61.11 % | 1 |
| `toxicology` | 35 | 32 | 13 | 40.62 % | 37.14 % | 0 |

Micro: 99/209 = 47.37 % (denominator `gold_executable`). Macro: 0.4538 over 8 of 8 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| challenging | 61 | 41 | 17 | 41.46 % |
| moderate | 89 | 78 | 31 | 39.74 % |
| simple | 91 | 90 | 51 | 56.67 % |

### Paired difference vs baseline

Paired on **209** independent cases (denominator `paired_cases`: ids present in both files whose native gold executed (one case per question id, resampled by question id)).

- ΔEX = 7.66 % (95 % paired-bootstrap CI 0.96 % … 13.88 %; 10000 resamples, seed 0)
- Discordant: b = 32 (current right / baseline wrong), c = 16; exact McNemar p = 0.0293
- Excluded: 32 common ids with unexecutable gold, 0 with a missing prediction
- Missing ids: 0 in baseline, 0 in current

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/paired_b1/ex241/per_question_ex241.jsonl`

