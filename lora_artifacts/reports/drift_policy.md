# Native-gold drift policy applied to the evaluation shards

Status: **applied** (t32) to `dev217`. Source audit: `cluster/drift_native.json` (t15, generated
2026-11-25T03:14:03Z). Harness: `cluster/eval_shards.py`. Regenerated artifacts:
`dryrun_14b-ev/` (all four shards) and `dryrun_ood-scale7b-v2/` (OOD-876).

This note exists so that **no dev-217 number is quoted before the drift correction is stated with
it**. The headline denominator is **216**, not 217; 217 survives only as the pre-policy size.

## The policy

A native PostgreSQL gold answer is only usable when it answers the same question as the official
BIRD gold. t15 compared every dev row's native PostgreSQL result with the BIRD SQLite original
(the SQLite files are present locally for the eight dev databases) and classified each
non-exact row:

| class | usable as native gold? | treatment here |
|---|---|---|
| `exact_match` (197) | yes | scored |
| `legitimate_engine_difference` (12: float/timestamp formatting, rounding) | yes — equal after the project normaliser | scored |
| `translation_defect` / `reviewed_correction` (7) | yes — the correction is deliberate | **scored but flagged `translation_defect`** |
| `server_side_data_defect` (1) | **no** — the server data differs | **excluded from scoring** |
| `not_comparable` (0) | no | — |

Rule for every shard:

* a row whose native gold cannot be compared with its SQLite original is **not scored** — it is
  removed from the numerator and every denominator, and kept in the id-level ledger with
  `excluded_from_scoring: true` and its reason, so the pre-policy number stays reproducible;
* a reviewed correction stays usable as the corrected native gold but carries
  `translation_defect: true` and `gold_origin: "reviewed_correction"`, so the report can state how
  many of the shard's rows are reviewed rather than original;
* a shard whose SQLite originals are not available locally gets **no** drift audit, and says so
  explicitly instead of implying one.

## The excluded row: question 309 (toxicology)

| field | value |
|---|---|
| question id | `309` |
| database | `toxicology` |
| class / subtype | `server_side_data_defect` / `superset_volume` |
| measured | PostgreSQL `atom` holds **12,333** rows; the BIRD SQLite `atom` holds **9,111** |
| result shape | 8 rows on PostgreSQL vs 5 rows on SQLite for this question |
| reason | the server carries a superset of the SQLite molecules, so any count-based answer differs from the official BIRD gold by data volume alone; the gold also carries a reviewed correction (TR346) |
| source | `cluster/drift_native.json` → `must_not_use_as_native_gold[0]`, `flagged_rows[qid=309]` |
| pre-policy score (frozen 14B) | 1 — it was counted as correct; that point is removed |

The atom counts were re-measured for this note: `SELECT count(*) FROM atom` returns 9,111 on
`data_cache/dev_databases/toxicology/toxicology.sqlite` and 12,333 on the server's `toxicology`
database through the tunnel.

## Denominators after the policy

| shard | denominator | note |
|---|---:|---|
| `dev217` | **216** | drift-clean: 217 rows − question 309 |
| `ex241` | **241** | historical continuity only; drift policy **not** applied (its ledger stays comparable to the frozen 130/241 report). 209 of the 241 have executable gold |
| `ood876` | **876** | 811 executable gold; no drift audit possible — see below |
| `pghard` | 96 | hand-authored set, no SQLite original by construction; drift policy not applicable |

**216 and 241 are different shards with different gold and different evaluators. Their
percentages must never be compared with each other; only the paired delta on the same ids is
meaningful.**

## Corrected baseline (frozen 14B predictions, `repo/runs/t2/14b-ev/query_results.json`)

| number | value | status |
|---|---|---|
| before the drift policy | **136/217 = 62.67 %** (Wilson 56.1–68.8) | pre-policy only — do not quote as a result |
| after the drift policy | **135/216 = 62.50 %** (Wilson 55.9–68.7) | the corrected baseline for the clean shard |
| gold executable | 216/216 | question 309 executed fine; it is excluded for *data* drift, not for an execution failure |
| predictions | 211 executed, 5 failed (`undefined_column`), 0 missing | the 5 failures stay in the denominator as wrong answers |
| micro / macro | 135/216 · 0.5997 over 8 databases | same denominator |
| paired self-comparison | ΔEX 0.00 %, 95 % CI [0.00 %, 0.00 %], n = 216 | machinery check, unchanged |

The excluded row was scored correct, so removing it lowers the numerator by one and the
denominator by one: 136/217 (62.67 %) becomes 135/216 (62.50 %). Both numbers are printed in
`dryrun_14b-ev/report.md` under "Native-gold drift policy", with 136/217 labelled pre-policy.

## The 7 reviewed corrections (kept usable, flagged)

| question id | database | flag |
|---|---|---|
| 445 | card_games | `translation_defect` / `reviewed_correction` |
| 571 | codebase_community | `translation_defect` / `reviewed_correction` |
| 694 | codebase_community | `translation_defect` / `reviewed_correction` |
| 1496 | debit_card_specializing | `translation_defect` / `reviewed_correction` |
| 1525 | debit_card_specializing | `translation_defect` / `reviewed_correction` |
| 1529 | debit_card_specializing | `translation_defect` / `reviewed_correction` |
| 1531 | debit_card_specializing | `translation_defect` / `reviewed_correction` |

So of the 216 scored rows: **209 are original SQLite-equivalent gold, 7 are reviewed
corrections** (`report.json → shards.dev217.drift.row_origin`). The flags are also on every
id-level record in `per_question_dev217.jsonl`.

## OOD-876: no drift audit is possible, and that is stated

The OOD-876 databases are BIRD **train** databases. Their SQLite originals are **not shipped
locally** — only the eight dev SQLite files are present under `data_cache/dev_databases/` — so the
SQLite-vs-PostgreSQL comparison that excluded question 309 cannot be run for this shard.

* Native PostgreSQL gold stays the reference for OOD-876 (there is no alternative to compare
  against).
* No drift-clean claim is made. If a shard-wide drift audit is required later, it needs the BIRD
  train SQLite archives; until then the limitation is part of the report text, not an assumption.
* The **65 unexecutable gold ids remain listed** in `dryrun_14b-ev/report.md` (OOD-876 section)
  and in `report.json → shards.ood876.gold_side.unexecutable_ids`; they leave the EX denominator
  (811 of 876) and are not silently dropped.

## How this is enforced

* the exclusion, its reason and the atom counts live in the shard definition
  (`SHARDS["dev217"].exclusions`, `exclude_ids`), not in prose only;
* `expected_size` is 216 and `pre_policy_size` is 217; `load_gold` checks the *scored* size;
* the excluded row is still executed and scored, so `drift.pre_policy` is recomputed from the
  same ledger (it reproduces 136/217 exactly) rather than copied from an old report;
* `python cluster/eval_shards.py --selftest` fails if the exclusion, the 7 reviewed ids, the
  216/217 sizes or the OOD limitation drift;
* `python cluster/verify_eval_reports.py` (64 checks) fails if any report stops stating 216 with
  its reason, or if the pre-policy number appears as anything but pre-policy.

## Reproduce

```bash
python research/advtext2sql/ssh_tunnel.py &      # DB tunnel 127.0.0.1:5444

python cluster/eval_shards.py \
  --predictions repo/runs/t2/14b-ev/query_results.json --self-compare \
  --ledger repo/runs/t2/scores/14b-ev.json --ledger-shard ex241 \
  --shards dev217,ex241,ood876,pghard \
  --contamination dev217=unknown,ex241=unknown,ood876=contaminated_run,pghard=no_shard \
  --contamination-note "frozen pre-dedupe-fix run: continuity/dry-run mechanics only" \
  --out-dir cluster/eval_reports/dryrun_14b-ev

python cluster/verify_eval_reports.py
```

## What this changes for the meeting table

* quote **135/216 = 62.50 %** for the frozen 14B baseline, with "216 after the native-gold drift
  policy, question 309 excluded for a server-data superset (12,333 vs 9,111 atoms)";
* if the older **136/217 = 62.67 %** appears anywhere, it must be labelled "before the drift
  policy";
* the historical **241** is a separate continuity column, never a comparison with 216;
* OOD-876 rows must not imply a drift audit; they are 876 questions with 811 executable gold and
  65 listed unexecutable ids.
