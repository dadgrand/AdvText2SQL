# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/t2/14b-ev/query_results.json`
- Baseline: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/t2/14b-ev/query_results.json`
- Generated: 2026-11-25T03:30:23+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `tcp:127.0.0.1:5444`
- Instances: `default` → `tcp:127.0.0.1:5444` (15.15 (Debian 15.15-1.pgdg13+1)); `pghard_` → `unix:/home/stonemonkey/.cache/advtext2sql/pghard_pgdata` (16.2)
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 0 live queries, 2615 cache hits, 0 execution errors, 0 skipped (offline)
- Frozen-ledger continuity check (ex241 vs `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/t2/scores/14b-ev.json`): MATCH

> **None of the rows below is a clean-split result.** Contaminated or unknown rows are harness mechanics and continuity checks only; they must not be quoted as OOD/result numbers. A usable row requires `contamination=clean` and a recorded split-manifest hash.

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dev217` | primary | not certified clean | 216 | 216 | 216 | 0 | 135/216 = 62.50 % [55.9–68.7, Wilson] | 135/216 = 62.50 % | 135/216 = 62.50 % | 0.5997 (8) | 0.00 % (216) |
| `ex241` | continuity_only | not certified clean | 241 | 241 | 209 | 32 | 130/209 = 62.20 % [55.5–68.5, Wilson] | 130/241 = 53.94 % | 130/209 = 62.20 % | 0.5967 (8) | 0.00 % (209) |
| `ood876` | primary | contaminated_run, not a result | 876 | 0 | 811 | 65 | — (no predictions) | — (no predictions) | — | — | not computable (n=0) |
| `pghard` | primary | not certified clean | 96 | 0 | 96 | 0 | — (no predictions) | — (no predictions) | — | — | not computable (n=0) |

EX/gold-executable is the headline; EX/all is shown only so the two denominators are never confused. A missing or non-executing prediction stays in EX/gold-executable as a wrong answer. A row whose contamination status is not `clean` is not a result, whatever its EX.

## Native-gold drift policy (t15)

Policy: a row whose native PostgreSQL gold cannot be compared with its BIRD SQLite original is **not scored** (the SQLite data is the official reference for dev); a reviewed correction stays usable as the corrected native gold but is flagged `translation_defect`. A shard where the SQLite originals are not available locally gets no drift audit, and says so instead of implying one.

| shard | denominator | before the policy | excluded | reviewed corrections | drift audit |
|---|---|---|---|---|---|
| `dev217` | 216 | 217 rows | 1 | 7 | applied (8 SQLite databases) |
| `ex241` | 241 | 241 rows | 0 | 0 | not applied (continuity) |
| `ood876` | 876 | 876 rows | 0 | 0 | **not possible** (no SQLite originals) |
| `pghard` | 96 | 96 rows | 0 | 0 | not applicable (hand-authored set) |

The denominator column is the shard size after the policy (216 drift-clean, 241 historical, 876 OOD-876). EX is computed on the executable-gold denominator where it differs: `ex241` 209 of 241, `ood876` 811 of 876.

## `dev217` — PG-reviewed dev-216 (native PG gold, drift-clean)

- Reviewed dev answers; gold is native PostgreSQL (pg_gold_sql) and is executed verbatim.
- Denominator after the native-gold drift policy: 216 (question 309 is excluded, see the drift policy section); 217 appears only as the pre-policy size. 7 of the 216 rows are reviewed corrections.
- Do not compare a percentage here with EX/241: different gold and a different denominator.

**Contamination status: `unknown` — not certified clean.** Result usable: **False**.

> Dry-run mechanics on the frozen 14b-ev file. This run predates the dedupe fix, so no row here is a clean-split result; dev217 and ex241 are continuity references only, and any OOD number from this run is contaminated. The pghard row only shows the frozen denominators (the file has no PG-hard answers).

> Finding on record: t9: the corpus of the cancelled 14B run contained 1,018 rows from reserved OOD schemas — 857 on the ten named OOD databases plus 161 on simpson_episodes (same schema family as OOD-reserved law_episode).

> Split manifest: none recorded (no clean-split claim is made for this row).

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/bird_pg_unified/data/bird_dev_pg.jsonl` field `pg_gold_sql`, rewrite `none`, sha256 `b333af44f3aa124d…`, criterion `execution_ex`.

### Native-gold drift policy

- Denominator after the policy: **216** (the shard file holds 217 rows; 1 excluded).
- Excluded: `309` (toxicology) — server_side_data_defect/superset_volume: the PostgreSQL toxicology database holds 12,333 atom rows against 9,111 in the BIRD SQLite original, so any count-based answer differs from the official BIRD gold by data volume alone; the gold also carries a reviewed correction (TR346). Source: cluster/drift_native.json (t15 native-gold drift audit).
  It scored 1 under the pre-policy harness; that point is **removed** from the corrected number.
- Before the drift policy: **136/217 = 62.67 %** — shown only as the pre-policy number, never as a result. After the policy: **135/216 = 62.50 %**.
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
| `prediction_executed` | 211 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 5 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 0 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, native gold)

Executed **216** of **216** gold queries (100.00 %); **0** unexecutable, removed from the EX denominator.

Historical SQLite rewrite applied to this native gold (offline check, no execution): it would alter **39** of 216 queries and break **24** of them at the PostgreSQL parse level — which is exactly why this shard's gold is executed verbatim.

Execution-level check on those 39 altered queries: **11** return exactly the native rows, **2** would change the answer, **26** would fail although native gold runs (denominator `gold queries altered by the historical rewrite`).

Predictions: **211** executed, **5** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 216); 0 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 5 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `california_schools` | 23 | 23 | 4 | 17.39 % | 17.39 % | 4 |
| `card_games` | 28 | 28 | 17 | 60.71 % | 60.71 % | 0 |
| `codebase_community` | 26 | 26 | 16 | 61.54 % | 61.54 % | 1 |
| `debit_card_specializing` | 19 | 19 | 11 | 57.89 % | 57.89 % | 0 |
| `financial` | 27 | 27 | 12 | 44.44 % | 44.44 % | 0 |
| `student_club` | 30 | 30 | 23 | 76.67 % | 76.67 % | 0 |
| `superhero` | 36 | 36 | 34 | 94.44 % | 94.44 % | 0 |
| `toxicology` | 27 | 27 | 18 | 66.67 % | 66.67 % | 0 |

Micro: 135/216 = 62.50 % (denominator `gold_executable`). Macro: 0.5997 over 8 of 8 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| challenging | 54 | 54 | 32 | 59.26 % |
| moderate | 79 | 79 | 43 | 54.43 % |
| simple | 83 | 83 | 60 | 72.29 % |

### Paired difference vs baseline

Paired on **216** independent cases (denominator `paired_cases`: ids present in both files whose native gold executed (one case per question id, resampled by question id)).

- ΔEX = 0.00 % (95 % paired-bootstrap CI 0.00 % … 0.00 %; 10000 resamples, seed 0)
- Discordant: b = 0 (current right / baseline wrong), c = 0; exact McNemar p = 1
- Excluded: 0 common ids with unexecutable gold, 0 with a missing prediction
- Missing ids: 0 in baseline, 0 in current

### Self-comparison check

- ΔEX exactly zero: **True**; interval contains zero: **True**; CI [0.0, 0.0]; n = 216

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/dryrun_14b-ev/per_question_dev217.jsonl`

## `ex241` — Historical EX/241 (own evaluator, continuity only)

- Historical shard: SQLite-dialect gold scored through the repo's own repo_rewrite (sqlglot sqlite->postgres plus the shipped NULLIF regex). Continuity with the frozen ledger only; this gold pipeline is NOT used for any other shard.
- Contains the 217 reviewed dev ids (216 of them scored after the drift policy) plus 24 excluded ids; the denominators differ from dev217.

**Contamination status: `unknown` — not certified clean.** Result usable: **False**.

> Dry-run mechanics on the frozen 14b-ev file. This run predates the dedupe fix, so no row here is a clean-split result; dev217 and ex241 are continuity references only, and any OOD number from this run is contaminated. The pghard row only shows the frozen denominators (the file has no PG-hard answers).

> Finding on record: t9: the corpus of the cancelled 14B run contained 1,018 rows from reserved OOD schemas — 857 on the ten named OOD databases plus 161 on simpson_episodes (same schema family as OOD-reserved law_episode).

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
| `prediction_executed` | 204 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 5 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 0 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, historical rewrite — continuity)

Executed **209** of **241** gold queries (86.72 %); **32** unexecutable, removed from the EX denominator.

| gold error category | n |
|---|---|
| `syntax_error` | 30 |
| `undefined_column` | 2 |

This shard's own evaluator applies the historical rewrite on purpose (continuity with the frozen ledger): it rewrites **128** of 241 queries and **28** stop parsing as PostgreSQL under it — the cost the other shards do not pay.

Predictions: **204** executed, **5** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 209); 32 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 5 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `california_schools` | 24 | 20 | 6 | 30.00 % | 25.00 % | 4 |
| `card_games` | 31 | 24 | 13 | 54.17 % | 41.94 % | 0 |
| `codebase_community` | 27 | 23 | 13 | 56.52 % | 48.15 % | 0 |
| `debit_card_specializing` | 26 | 21 | 11 | 52.38 % | 42.31 % | 0 |
| `financial` | 29 | 26 | 12 | 46.15 % | 41.38 % | 1 |
| `student_club` | 33 | 33 | 24 | 72.73 % | 72.73 % | 0 |
| `superhero` | 36 | 30 | 29 | 96.67 % | 80.56 % | 0 |
| `toxicology` | 35 | 32 | 22 | 68.75 % | 62.86 % | 0 |

Micro: 130/209 = 62.20 % (denominator `gold_executable`). Macro: 0.5967 over 8 of 8 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| challenging | 61 | 41 | 24 | 58.54 % |
| moderate | 89 | 78 | 43 | 55.13 % |
| simple | 91 | 90 | 63 | 70.00 % |

### Paired difference vs baseline

Paired on **209** independent cases (denominator `paired_cases`: ids present in both files whose native gold executed (one case per question id, resampled by question id)).

- ΔEX = 0.00 % (95 % paired-bootstrap CI 0.00 % … 0.00 %; 10000 resamples, seed 0)
- Discordant: b = 0 (current right / baseline wrong), c = 0; exact McNemar p = 1
- Excluded: 32 common ids with unexecutable gold, 0 with a missing prediction
- Missing ids: 0 in baseline, 0 in current

### Self-comparison check

- ΔEX exactly zero: **True**; interval contains zero: **True**; CI [0.0, 0.0]; n = 209

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/dryrun_14b-ev/per_question_ex241.jsonl`

## `ood876` — OOD-876 (native PG gold, no secondary transpilation)

- PostgreSQL gold is executed verbatim: no sqlglot transpile, no repo_rewrite, no regex. The old OOD report re-transpiled this gold with the SQLite rewrite; this harness measures what that rewrite would change instead of assuming it (see the rewrite audit and the frozen-ledger check).
- Question ids are normalised by numeric suffix (ood:disney:0000 == ood:disney:0) so a run recorded with padded ids still pairs.
- A usable OOD number must come from a model trained on the clean split (t14 run B onwards) and must be scored with --contamination ood876=clean --split-manifest <manifest>; the manifest hash is then printed in the report. Pre-dedupe-fix runs are labelled contaminated_run, not a result.

**Contamination status: `contaminated_run` — contaminated_run, not a result.** Result usable: **False**.

> Dry-run mechanics on the frozen 14b-ev file. This run predates the dedupe fix, so no row here is a clean-split result; dev217 and ex241 are continuity references only, and any OOD number from this run is contaminated. The pghard row only shows the frozen denominators (the file has no PG-hard answers).

> Finding on record: t9: the corpus of the cancelled 14B run contained 1,018 rows from reserved OOD schemas — 857 on the ten named OOD databases plus 161 on simpson_episodes (same schema family as OOD-reserved law_episode).

> Split manifest: none recorded (no clean-split claim is made for this row).

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/ood/ood_heldout.json` field `SQL`, rewrite `none`, sha256 `32f876bc51cc3134…`, criterion `execution_ex`.

### Native-gold drift policy

- **Drift audit: not possible on this shard.** The OOD-876 databases are BIRD train databases; their SQLite originals are not shipped locally (only the eight dev SQLite files are present), so a SQLite-vs-PostgreSQL drift audit cannot be run. Native PostgreSQL gold stays the reference; the 65 unexecutable gold ids are listed in this report.
- The 65 unexecutable gold ids stay listed (they leave the EX denominator, they are not silently dropped):

  ```
  ood:disney:0, ood:disney:2, ood:disney:37, ood:european_football_1:103, ood:european_football_1:112, ood:menu:212, ood:movie_3:278, ood:movie_3:317
  ood:movie_3:363, ood:movie_3:371, ood:movie_3:388, ood:movie_3:413, ood:movie_3:421, ood:movie_3:430, ood:movie_3:461, ood:movie_3:464
  ood:movie_3:468, ood:movies_4:551, ood:movies_4:554, ood:movies_4:575, ood:movies_4:580, ood:movies_4:581, ood:sales_in_weather:643, ood:sales_in_weather:646
  ood:sales_in_weather:660, ood:sales_in_weather:661, ood:sales_in_weather:662, ood:sales_in_weather:664, ood:sales_in_weather:665, ood:sales_in_weather:669, ood:sales_in_weather:671, ood:sales_in_weather:672
  ood:sales_in_weather:677, ood:sales_in_weather:679, ood:sales_in_weather:687, ood:sales_in_weather:692, ood:sales_in_weather:696, ood:sales_in_weather:698, ood:social_media:707, ood:social_media:708
  ood:social_media:712, ood:social_media:713, ood:social_media:714, ood:social_media:718, ood:social_media:719, ood:social_media:720, ood:social_media:726, ood:social_media:727
  ood:social_media:728, ood:social_media:735, ood:social_media:736, ood:social_media:740, ood:social_media:745, ood:social_media:746, ood:social_media:749, ood:social_media:750
  ood:social_media:752, ood:social_media:755, ood:social_media:759, ood:talkingdata:769, ood:talkingdata:806, ood:talkingdata:811, ood:talkingdata:815, ood:talkingdata:854
  ood:talkingdata:856
  ```

### Denominators

| name | n | definition |
|---|---|---|
| `shard_size` | 876 | cases in the shard file; denominator of EX/all |
| `predictions_present` | 0 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 876 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 811 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 65 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 811 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 0 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 0 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 811 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, native gold)

Executed **811** of **876** gold queries (92.58 %); **65** unexecutable, removed from the EX denominator.

| gold error category | n |
|---|---|
| `undefined_function` | 31 |
| `undefined_column` | 22 |
| `sql_error` | 5 |
| `syntax_error` | 5 |
| `datatype_mismatch` | 1 |
| `invalid_text_representation` | 1 |

Historical SQLite rewrite applied to this native gold (offline check, no execution): it would alter **28** of 876 queries and break **0** of them at the PostgreSQL parse level — which is exactly why this shard's gold is executed verbatim.

Execution-level check on those 28 altered queries: **23** return exactly the native rows, **0** would change the answer, **0** would fail although native gold runs (denominator `gold queries altered by the historical rewrite`).

**No predictions for this shard in this file** (0 of 876 ids covered; 811 gold-executable cases have no prediction), so EX is not computed here — only the executable-gold denominator is reported. A run that covers this shard fills the row.

| database | cases | gold executed | gold unexecutable |
|---|---|---|---|
| `disney` | 77 | 74 | 3 |
| `european_football_1` | 38 | 36 | 2 |
| `law_episode` | 89 | 89 | 0 |
| `menu` | 69 | 68 | 1 |
| `movie_3` | 200 | 189 | 11 |
| `movies_4` | 139 | 134 | 5 |
| `music_tracker` | 31 | 31 | 0 |
| `sales_in_weather` | 58 | 42 | 16 |
| `social_media` | 60 | 39 | 21 |
| `talkingdata` | 115 | 109 | 6 |

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/dryrun_14b-ev/per_question_ood876.jsonl`

## `pghard` — PG-hard (frozen hand-authored set, native PG gold on an isolated fixture instance)

- Frozen set (v1): 12 hand-authored schemas, 96 questions, questions sha256 429fdaec…, gold sha256 ed380a08…. The gold file is datasets/pg_hard/gold.jsonl; it carries gold_sql, the per-question comparison mode, and fixtures[{fixture_id: default, db_id: pghard_<slug>}].
- The fixtures live on a bundled PostgreSQL 16.2 instance reached through its Unix socket, not through the course tunnel: databases pghard_<slug>, datadir ~/.cache/advtext2sql/pghard_pgdata (override with --pghard-datadir). Rebuild after a reboot with datasets/pg_hard/build_pg_hard.py --ensure, or rebuild+verify with build_pg_hard.py and verify_pg_hard.py; the frozen hashes do not change.
- Scoring criterion: a question scores 1 only if the prediction matches the native PG oracle on ALL fixtures whose gold executes. The frozen set currently has one fixture per question, so raw pass@1 (single default fixture) and the all-fixtures criterion coincide; the report states both.
- Runs on this shard before t14 run B are smoke runs: they exercise the wiring and the denominators, not a model result.

**Contamination status: `unknown` — not certified clean.** Result usable: **False**.

> Dry-run mechanics on the frozen 14b-ev file. This run predates the dedupe fix, so no row here is a clean-split result; dev217 and ex241 are continuity references only, and any OOD number from this run is contaminated. The pghard row only shows the frozen denominators (the file has no PG-hard answers).

> Finding on record: t9: the corpus of the cancelled 14B run contained 1,018 rows from reserved OOD schemas — 857 on the ten named OOD databases plus 161 on simpson_episodes (same schema family as OOD-reserved law_episode).

> Split manifest: none recorded (no clean-split claim is made for this row).

Gold: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/datasets/pg_hard/gold.jsonl` field `gold_sql`, rewrite `none`, sha256 `ed380a082b2edacc…`, criterion `all_fixtures`.

### Native-gold drift policy

- **Drift policy: not applicable.** hand-authored schemas and fixtures: there is no BIRD SQLite original to compare against, so the native-gold drift policy does not apply

### Denominators

| name | n | definition |
|---|---|---|
| `shard_size` | 96 | cases in the shard file; denominator of EX/all |
| `predictions_present` | 0 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 96 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 96 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 0 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 96 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 0 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 0 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 96 | gold-executable cases with no prediction at all (missing from the predictions file) |
| `not_executed` | 0 | cases whose execution was skipped (offline replay without a cache entry); never counted as gold-executable |

### Gold executability (measured, native gold)

Executed **96** of **96** gold queries (100.00 %); **0** unexecutable, removed from the EX denominator.

Historical SQLite rewrite applied to this native gold (offline check, no execution): it would alter **84** of 96 queries and break **0** of them at the PostgreSQL parse level — which is exactly why this shard's gold is executed verbatim.

Execution-level check on those 84 altered queries: **72** return exactly the native rows, **0** would change the answer, **12** would fail although native gold runs (denominator `gold queries altered by the historical rewrite`).

**No predictions for this shard in this file** (0 of 96 ids covered; 96 gold-executable cases have no prediction), so EX is not computed here — only the executable-gold denominator is reported. A run that covers this shard fills the row.

| database | cases | gold executed | gold unexecutable |
|---|---|---|---|
| `pghard_airline` | 8 | 8 | 0 |
| `pghard_banking` | 8 | 8 | 0 |
| `pghard_education` | 8 | 8 | 0 |
| `pghard_energy` | 8 | 8 | 0 |
| `pghard_healthcare` | 8 | 8 | 0 |
| `pghard_logistics` | 8 | 8 | 0 |
| `pghard_manufacturing` | 8 | 8 | 0 |
| `pghard_marketplace` | 8 | 8 | 0 |
| `pghard_retail_orders` | 8 | 8 | 0 |
| `pghard_ride_hailing` | 8 | 8 | 0 |
| `pghard_streaming` | 8 | 8 | 0 |
| `pghard_telecom` | 8 | 8 | 0 |

### By skill

| skill | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| `asof_state` | 12 | 12 | 0 | 0.00 % |
| `compositional` | 12 | 12 | 0 | 0.00 % |
| `grain_fanout_antijoin` | 12 | 12 | 0 | 0.00 % |
| `hierarchies` | 11 | 11 | 0 | 0.00 % |
| `jsonb_arrays` | 12 | 12 | 0 | 0.00 % |
| `periods_timezones` | 12 | 12 | 0 | 0.00 % |
| `topn_ties` | 12 | 12 | 0 | 0.00 % |
| `window_frames_cohorts` | 13 | 13 | 0 | 0.00 % |

Pass@1 over the executable denominator is 0/96 by construction here: no question in this file has a prediction. That is a coverage artefact, not a model score.

### Scoring criteria

| criterion | correct | denominator | definition |
|---|---|---|---|
| `raw_pass_at_1` | 0 | 96 | the prediction executed and returned exactly the oracle rows on the single default fixture |
| `oracle_all_fixtures` | 0 | 96 | the prediction matched the native PG oracle on every fixture whose gold executed |

Fixtures per question: min 1, max 1, total 96. the frozen set has one fixture per question, so raw pass@1 and the all-fixtures criterion coincide; they diverge only when a question carries several fixtures

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/dryrun_14b-ev/per_question_pghard.jsonl`

## Frozen-ledger continuity

Compared shard `ex241` against `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/t2/scores/14b-ev.json` (the audited `dev_tools.fixed_eval` output).

| field | frozen ledger | this run | match |
|---|---|---|---|
| `ex_all.correct` | 130 | 130 | yes |
| `ex_all.n` | 241 | 241 | yes |
| `ex_gold_executable.correct` | 130 | 130 | yes |
| `ex_gold_executable.n` | 209 | 209 | yes |
| `n_gold_unexecutable` | 32 | 32 | yes |
| `gold_unexecutable_ids` | 32 | 32 | yes |

Gold-unexecutable id sets: overlap **32** of 32 (frozen ledger) / 32 (this run); only in ledger 0, only in this run 0.

