# BIRD in PostgreSQL — cleaned dev/train splits, schemas and synthetic data

This folder is the dataset hand-off for the course project. Full details,
reasoning and limitations are in **[README.ru.md](README.ru.md)** (Russian); this
file is only the map.

| Path | Rows | What |
|---|---:|---|
| `data/bird_dev_pg.jsonl` | 241 | course dev split, gold SQL in PostgreSQL, every query executed on the live server |
| `data/bird_dev_clean.jsonl` | 216 | the subset whose PostgreSQL result set is identical to the original SQLite run |
| `data/bird_dev_divergent.jsonl` | 25 | executes but the two engines answer differently (server columns are typed; see README §3) |
| `data/bird_train_pg.jsonl` | — | filtered BIRD train release translated to PostgreSQL, executability verified |
| `data/bird_train_excluded.jsonl` | — | what did not pass and why |
| `data/synthetic_bird_like.jsonl` | — | synthetic BIRD-like examples, built over **train** schemas only |
| `schemas/schema.json`, `schemas/<db_id>.sql` | 77 dbs | tables/columns/types/keys, plus runnable DDL |
| `reports/*.json` | — | primary measurement reports every number comes from |
| `tools/verify_dataset.py` | — | integrity check + optional live execution/hash check |
| `tools/to_sft.py` | — | converter to the chat-SFT format used by `training/` |

```bash
python tools/verify_dataset.py .
python tools/to_sft.py data/bird_dev_clean.jsonl out/dev_sft.jsonl
```

Headline facts: with the audited AST rewriter **all 241 dev gold queries execute**
(the shipped harness destroys 30 of them with a regex — see README §2), and the
translation agrees with BIRD's own published PostgreSQL gold on **109 of 114**
overlapping questions.
