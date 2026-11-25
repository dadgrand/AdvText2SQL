# Schema-family splits and decontamination

Implements `docs/playbook/POSTGRES_TEXT2SQL_LORA.md` section 2 item 1: one manifest that maps
every schema family to exactly one split, the closed evaluation registry, and the offline
tests that must stay green before generation or training.

| File | What |
|---|---|
| `schema_family_split.json` | manifest: 80 database entries (77 from `bird_pg_unified` + 3 held-out Mini-Dev), each with a stable `db_id`, a lineage fingerprint of tables/keys/column types, and a split |
| `eval_questions.jsonl` | closed evaluation registry, 1,260 questions: 217 reviewed dev (`internal_valid`), 876 OOD (`ood_reserved`), 167 held-out Mini-Dev PostgreSQL (`external_test`) |
| `splits_report.json` | recomputed overlap numbers, per-split counts, excluded rows, renamed-copy candidates, trainable-file audit, question-overlap audit |
| `build_splits.py` | builds the three files above (`python3 datasets/splits/build_splits.py`) |
| `test_no_overlap.py` | 10 offline checks; exits non-zero on any violation (`python3 datasets/splits/test_no_overlap.py`) |

## Splits

| Split | Schema families | db_ids | Real rows (train pool) | Synthetic rows (active pool) | Eval questions |
|---|---:|---:|---:|---:|---:|
| `train` | 52 | 52 | 4,216 | 2,691 | 0 |
| `internal_valid` | 8 | 8 | 0 | 0 | 217 |
| `ood_reserved` | 10 | 11 | 894 | 124 | 876 |
| `external_test` | 9 | 9 | 0 | 0 | 167 |

* `internal_valid` = the eight reviewed dev-217 databases; checkpoint selection only.
* `ood_reserved` = the ten historical OOD databases plus one lineage sibling
  (`simpson_episodes`, see below). None of these rows may be trained on.
* `external_test` = three held-out Mini-Dev PostgreSQL schemas (`formula_1`,
  `european_football_2`, `thrombosis_prediction`, 167 official questions with PostgreSQL
  gold, fingerprints from `minidev/MINIDEV/dev_tables.json`) plus six empty registry
  placeholders (`trains`, `university`, `video_games`, `works_cycles`, `world`,
  `world_development_indicators`) whose schema was never captured. All nine are closed:
  no row on them exists in any trainable file.

## Playbook claim, recomputed

The playbook claims 745 real + 112 synthetic rows over 9 databases overlap the historical OOD
list, leaving 4,365 + 2,703. Recomputed from the current files:

| Measure | Playbook | Recomputed |
|---|---:|---:|
| real OOD-overlap rows | 745 | **745** |
| synthetic OOD-overlap rows | 112 | **112** |
| databases with real/synthetic overlap | 9 | **9** (both) |
| real rows left | 4,365 | **4,365** |
| synthetic rows left | 2,703 | **2,703** |
| rows in the tenth OOD database (`talkingdata`) | 0 | **0** (only in excluded diagnostics; 115 eval questions) |

The verdict is `confirmed` with zero mismatches (`splits_report.json → claim_check`).

**Lineage adjustment.** Fingerprinting (tables + keys + column types) found one near-copy:
`simpson_episodes` shares 6 of 7 tables and 76.6% of table-qualified columns with the
OOD-reserved `law_episode` (table Jaccard 0.857, column Jaccard 0.766). Exact fingerprints
differ because columns were renamed, so the candidate is reported rather than silently
merged; it is assigned to `ood_reserved` so the schema family is never split across
train/eval. That removes 149 real + 12 synthetic rows from train, so the lineage-aware split
counts are 4,216 real / 2,691 synthetic. Both the strict playbook numbers and the
lineage-aware numbers are in the report.

No exact renamed copies (identical fingerprints) and no cross-source structural matches
were found beyond this pair.

## Decontamination status of the current training file

`cluster/sft_all_v2.jsonl` (the corpus handed to the 14B run) still contains **1,018 rows
from the `ood_reserved` split** — 857 on the ten named OOD databases and 161 on the lineage
sibling. Their ids are in `splits_report.json → excluded_rows`. The file must be filtered by
that list before a final run; otherwise OOD-876 scores are contaminated. No trainable file
checked contains an `external_test` schema or an `internal_valid` schema.

## Reproducing

```bash
cd research/advtext2sql
python3 datasets/splits/build_splits.py      # rebuild manifest, registry, report
python3 datasets/splits/test_no_overlap.py   # 10/10 PASS expected; non-zero on violations
```

The tests are offline and read only local files; `pytest` also collects them when installed.

## Native-gold drift annotations (t15)

`native_gold_drift.json` carries a per-row drift classification for the audited dev rows
(exact match / legitimate engine difference / translation defect / server-side data
defect). `build_splits.py` embeds it into the manifest (`native_gold_drift` block) and
annotates every `internal_valid` row of `eval_questions.jsonl` with
`native_gold_drift_class`, `native_gold_usable` and `native_gold_drift_reason`. The one
server-data defect (`toxicology` 309) is listed in `must_not_use_as_native_gold`; no
non-exact row is silently kept.

```bash
python3 datasets/native_pg/drift_audit.py   # audits, writes flags, rebuilds this manifest
```
