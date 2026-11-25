# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/ood/ood-scale7b-v2/query_results.json`
- Baseline: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/ood/ood-base7b-292/query_results.json`
- Generated: 2026-11-25T03:24:22+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `127.0.0.1:5444`
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 0 live queries, 2076 cache hits, 0 execution errors, 0 skipped (offline)
- Frozen-ledger continuity check (ood876 vs `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/ood/scores/ood-scale7b-v2.json`): MATCH

> **None of the rows below is a clean-split result.** Contaminated or unknown rows are harness mechanics and continuity checks only; they must not be quoted as OOD/result numbers. A usable row requires `contamination=clean` and a recorded split-manifest hash.

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ood876` | primary | contaminated_run, not a result | 876 | 876 | 811 | 65 | 620/811 = 76.45 % [73.4–79.2, Wilson] | 620/876 = 70.78 % | 620/811 = 76.45 % | 0.7344 (10) | +74.01 % (277) |

EX/gold-executable is the headline; EX/all is shown only so the two denominators are never confused. A missing or non-executing prediction stays in EX/gold-executable as a wrong answer. A row whose contamination status is not `clean` is not a result, whatever its EX.

## Native-gold drift policy (t15)

Policy: a row whose native PostgreSQL gold cannot be compared with its BIRD SQLite original is **not scored** (the SQLite data is the official reference for dev); a reviewed correction stays usable as the corrected native gold but is flagged `translation_defect`. A shard where the SQLite originals are not available locally gets no drift audit, and says so instead of implying one.

| shard | denominator | before the policy | excluded | reviewed corrections | drift audit |
|---|---|---|---|---|---|
| `ood876` | 876 | 876 rows | 0 | 0 | **not possible** (no SQLite originals) |

The denominator column is the shard size after the policy (216 drift-clean, 241 historical, 876 OOD-876). EX is computed on the executable-gold denominator where it differs: `ex241` 209 of 241, `ood876` 811 of 876.

## `ood876` — OOD-876 (native PG gold, no secondary transpilation)

- PostgreSQL gold is executed verbatim: no sqlglot transpile, no repo_rewrite, no regex. The old OOD report re-transpiled this gold with the SQLite rewrite; this harness measures what that rewrite would change instead of assuming it (see the rewrite audit and the frozen-ledger check).
- Question ids are normalised by numeric suffix (ood:disney:0000 == ood:disney:0) so a run recorded with padded ids still pairs.
- A usable OOD number must come from a model trained on the clean split (t14 run B onwards) and must be scored with --contamination ood876=clean --split-manifest <manifest>; the manifest hash is then printed in the report. Pre-dedupe-fix runs are labelled contaminated_run, not a result.

**Contamination status: `contaminated_run` — contaminated_run, not a result.** Result usable: **False**.

> The 7B scale adapter predates the dedupe fix, so this file is NOT an OOD result. It is used to exercise the native-PG-gold path end to end and to reconcile the frozen 620/811 ledger.

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
| `predictions_present` | 876 | ids in the shard that the predictions file covers (after id normalisation) |
| `predictions_missing` | 0 | ids in the shard with no prediction; they stay in EX/all and count as wrong |
| `gold_executed` | 811 | gold queries that executed on the live server; denominator of EX/gold-executable |
| `gold_unexecutable` | 65 | gold queries that failed to execute; removed from the EX denominator, kept in EX/all |
| `ex_denominator` | 811 | EX/gold-executable denominator (== gold_executed); a prediction that is missing or fails to run scores 0 here |
| `prediction_executed` | 761 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 50 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
| `prediction_missing_among_gold_executable` | 0 | gold-executable cases with no prediction at all (missing from the predictions file) |
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

Predictions: **761** executed, **50** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 811); 65 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 40 |
| `undefined_function` | 3 |
| `undefined_table` | 3 |
| `sql_error` | 2 |
| `data_error` | 1 |
| `invalid_text_representation` | 1 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `disney` | 77 | 74 | 38 | 51.35 % | 49.35 % | 12 |
| `european_football_1` | 38 | 36 | 26 | 72.22 % | 68.42 % | 0 |
| `law_episode` | 89 | 89 | 62 | 69.66 % | 69.66 % | 4 |
| `menu` | 69 | 68 | 46 | 67.65 % | 66.67 % | 5 |
| `movie_3` | 200 | 189 | 152 | 80.42 % | 76.00 % | 13 |
| `movies_4` | 139 | 134 | 110 | 82.09 % | 79.14 % | 8 |
| `music_tracker` | 31 | 31 | 15 | 48.39 % | 48.39 % | 1 |
| `sales_in_weather` | 58 | 42 | 34 | 80.95 % | 58.62 % | 1 |
| `social_media` | 60 | 39 | 34 | 87.18 % | 56.67 % | 1 |
| `talkingdata` | 115 | 109 | 103 | 94.50 % | 89.57 % | 5 |

Micro: 620/811 = 76.45 % (denominator `gold_executable`). Macro: 0.7344 over 10 of 10 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| unknown | 876 | 811 | 620 | 76.45 % |

### Paired difference vs baseline

Paired on **277** independent cases (denominator `paired_cases`: ids present in both files whose native gold executed (one case per question id, resampled by question id)).

- ΔEX = 74.01 % (95 % paired-bootstrap CI 68.59 % … 79.06 %; 10000 resamples, seed 0)
- Discordant: b = 206 (current right / baseline wrong), c = 1; exact McNemar p = 2.022e-60
- Excluded: 15 common ids with unexecutable gold, 0 with a missing prediction
- Missing ids: 584 in baseline, 0 in current

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/dryrun_ood-scale7b-v2/per_question_ood876.jsonl`

## Frozen-ledger continuity

Compared shard `ood876` against `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/repo/runs/ood/scores/ood-scale7b-v2.json` (the audited `dev_tools.fixed_eval` output).

| field | frozen ledger | this run | match |
|---|---|---|---|
| `ex_all.correct` | 620 | 620 | yes |
| `ex_all.n` | 876 | 876 | yes |
| `ex_gold_executable.correct` | 620 | 620 | yes |
| `ex_gold_executable.n` | 811 | 811 | yes |
| `n_gold_unexecutable` | 65 | 65 | yes |
| `gold_unexecutable_ids` | 65 | 65 | yes |

Gold-unexecutable id sets: overlap **65** of 65 (frozen ledger) / 65 (this run); only in ledger 0, only in this run 0.

