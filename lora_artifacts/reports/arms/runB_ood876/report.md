# Four-shard evaluation report

- Predictions: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/runs/preds/runB_ood876.json`
- Baseline: none (single configuration)
- Generated: 2026-11-25T04:08:57+00:00
- Server: 15.15 (Debian 15.15-1.pgdg13+1) · host `tcp:127.0.0.1:5444`
- Paired bootstrap: 10000 resamples, seed 0
- Native-gold drift policy (t15, applied here): `cluster/eval_reports/drift_policy.md`
- Execution: 0 live queries, 1799 cache hits, 0 execution errors, 0 skipped (offline)

## Summary (each percentage carries its denominator)

| shard | quality | contamination | cases | predictions present | gold executed | gold unexec. | EX/gold-exec | EX/all | micro | macro (dbs) | paired Δ (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ood876` | primary | clean split | 876 | 876 | 811 | 65 | 638/811 = 78.67 % [75.7–81.3, Wilson] | 638/876 = 72.83 % | 638/811 = 78.67 % | 0.7767 (10) | no baseline |

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

**Contamination status: `clean` — clean split.** Result usable: **True**.

> arms A and B are the first runs trained only on datasets/native_pg/train_main.jsonl (train split of the t9 manifest, 6,499 rows, 1,018 reserved-OOD rows filtered by the loader)

> Finding on record: t9 found 1,018 reserve/OOD rows inside cluster/sft_all_v2.jsonl; train_main is that corpus with those rows removed, so a clean status is claimed against the hashed manifest

> Split manifest: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/datasets/splits/schema_family_split.json`, sha256 `65e6ca1c47060b5c08d7fb7999ff88ee4d848d659e24dafa08f7c7ad2a6cbb02`.

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
| `prediction_executed` | 755 | predictions that executed, among gold-executable cases |
| `prediction_unexecutable` | 56 | predictions that were attempted and failed to execute, among gold-executable cases; counted as wrong |
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

Predictions: **755** executed, **56** failed to execute, **0** missing, **0** returned the ambiguity marker (denominator: gold_executable = 811); 65 not attempted because the gold failed.

| prediction error category | n |
|---|---|
| `undefined_column` | 38 |
| `invalid_text_representation` | 6 |
| `sql_error` | 6 |
| `undefined_function` | 2 |
| `invalid_datetime_format` | 1 |
| `query_timeout` | 1 |
| `syntax_error` | 1 |
| `undefined_table` | 1 |

### By database (micro and macro)

| database | cases | gold executed | correct | EX/gold-exec | EX/all | pred. unexec. |
|---|---|---|---|---|---|---|
| `disney` | 77 | 74 | 47 | 63.51 % | 61.04 % | 13 |
| `european_football_1` | 38 | 36 | 26 | 72.22 % | 68.42 % | 2 |
| `law_episode` | 89 | 89 | 67 | 75.28 % | 75.28 % | 4 |
| `menu` | 69 | 68 | 47 | 69.12 % | 68.12 % | 5 |
| `movie_3` | 200 | 189 | 145 | 76.72 % | 72.50 % | 14 |
| `movies_4` | 139 | 134 | 114 | 85.07 % | 82.01 % | 6 |
| `music_tracker` | 31 | 31 | 26 | 83.87 % | 83.87 % | 0 |
| `sales_in_weather` | 58 | 42 | 28 | 66.67 % | 48.28 % | 6 |
| `social_media` | 60 | 39 | 35 | 89.74 % | 58.33 % | 0 |
| `talkingdata` | 115 | 109 | 103 | 94.50 % | 89.57 % | 6 |

Micro: 638/811 = 78.67 % (denominator `gold_executable`). Macro: 0.7767 over 10 of 10 databases (denominator `n_databases`).

### By difficulty

| difficulty | cases | gold executed | correct | EX/gold-exec |
|---|---|---|---|---|
| unknown | 876 | 811 | 638 | 78.67 % |

Per-question ledger: `/home/stonemonkey/Documents/TalentHub/research/advtext2sql/cluster/eval_reports/arms/runB_ood876/per_question_ood876.jsonl`

