# External text-to-SQL sources (t34)

Ingested and screened in the order of `docs/playbook/POSTGRES_TEXT2SQL_LORA.md` §2.
Every row keeps `source_id`, `source_row_id`, `source_url`, `revision`, `license`, and was
executed on PostgreSQL before being kept (1–200 result rows). Rows duplicate a train
question at 5-gram Jaccard ≥ 0.8 or use an eval-split schema are excluded.

| Source | Decision | Rows contributed | License | Revision |
|---|---|---:|---|---|
| omeryentur/text-to-postgresql | used | 339 | MIT | `33cc31467640859ee20b1ce97a7fac5b2ef24858` |
| gretelai/synthetic_text_to_sql | used | 192 | Apache-2.0 | `740ab236e64503fba51be1101df7a1be83bf455d` |
| zhangxiang666/DS-NL2SQL (PostgreSQL slice) | used | 151 | MIT | `66c7008d899b9df319b9b2327dc4027f8dd6e7d9` |
| pgexercises.com | partially used | 55 | CC-BY-SA-3.0 | site snapshot 2026-11-25 |

Total: 737 rows, all executed and returned 1–200 rows. The three used sources each
contribute ≥100 rows; PGExercises has only 61 read-only exercises at this site revision
(55 executed), so it is marked partially used with the measured reason. DS-NL2SQL reports
the real PostgreSQL slice (2,218 rows, 2,139 after eval/dedupe screen), not the
six-dialect total; 151 rows were raised from the source SQLite databases and executed.

`external_report.json` carries per-source before/after counts, reject reasons, skill
coverage, dialect/classification notes, execution shares and the independent verification
(provenance, dedupe, eval-schema and execution checks).

Reproduce (network/caches under `data_cache/external`; PostgreSQL tunnel on 127.0.0.1:5444):

```bash
.venv-tools/bin/python datasets/external/ingest.py --source gretelai
.venv-tools/bin/python datasets/external/ingest.py --source ds_nl2sql
.venv-tools/bin/python datasets/external/ingest.py --source pgexercises
.venv-tools/bin/python datasets/external/verify_external.py
```

omeryentur was ingested before t31 and is preserved unchanged; `--source omeryentur`
re-screens it only on explicit request.
