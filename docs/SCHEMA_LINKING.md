# Schema linking at inference — five modes, OFF by default

A procedure-only arm: the model's prompt usually carries every table of a database,
most of which the question never touches. This selector decides which tables (and,
optionally, which columns) the model sees. It changes no weights, calls no model and
costs no extra LLM call.

Default is **`off`**, and `off` is *byte-identical* to the pre-change prompt: the
tool keeps calling `schema_pruning.prune_schema` with the same arguments, and
`tests/test_schema_linking.py` pins the rendered system and user messages against a
reference built from the templates. An arm that cannot be switched off cleanly
cannot be measured cleanly.

| mode | what the model sees | measured prompt cost (8 dev DBs, 241 questions) |
|---|---|---|
| `off` | today's block (pruning only above 20 tables) | 100 % (404 cl100k tokens mean for the schema block) |
| `tables` | top-`k` scored tables + join closure, whole | 83 % of the block (337 tokens), 92.9 % of gold tables kept |
| `columns` | same tables, unmatched non-key columns dropped | 44 % of the block (178 tokens), 92.9 % of gold tables kept |

## How it works

```
question + evidence ──tokens──▶ table scores ──top-k / min_score──▶ join closure
                                                                        │
                                          tables mode: render whole ◀────┤
                                          columns mode: filter columns ◀─┘
                                                     │
                              empty? ──▶ fall back to prune_schema (never no schema)
```

### Tables stage

* Identifiers are split on camelCase, snake_case, dashes and spaces and compared as
  stemmed tokens (`schema_pruning.tokenize` plus a plural fold, so `accounts` matches
  `account`). A small stopword list removes the question's glue words.
* A table-name token hit counts 3.0, a column token hit 1.0. A question token of 3+
  characters found inside a *flattened* identifier counts 0.5–1.5, which is what lets
  `sat`/`scores` reach the table `satscores` (concatenated names are common in BIRD).
* **Optional semantic boost**: `BOOST_WEIGHT (2.0) × cosine(question, table name +
  columns)`. `auto` tries a local sentence-transformer (`local_files_only=True`, no
  network) and falls back to the deterministic hashing embedder **recording why**;
  the reason is written to the metadata. On this machine the weights are absent, so
  `auto` is the lexical arm and says so — that is why every number in this document
  was measured with `embedder=hash`/`auto` under lexical scoring.
* `k` tables are kept (default 6), each must score at least `min_score` (default 1.0).

### Join closure (recall, not precision)

Execution accuracy is made of recall: a question about a client's payments names only
`client`/`loan`, while the gold query also joins `account` and `disp`. When fewer than
`k` tables were selected, the linker appends tables that share a **multi-word key
column** (`account_id`, `client_id`, `payment_id`) with a selected table, transitively
and up to `k`. Single-word keys (`id`) are ignored on purpose — in a database where
every table has an `id`, they would link everything to everything. Closure never
replaces a scored table; it only fills spare slots, and the added names are recorded
(`join_closure`).

### Columns stage

Inside each selected table, columns whose tokens match the question are kept, plus
columns that look like join keys (`id`/`key`/`code` token). "When in doubt, keep the
whole table": a table with ≤ 6 columns, a table the question does not touch, a table
whose filtered form would keep fewer than 2 columns, or a block whose lines cannot be
aligned with its parsed column names all stay whole. The rendered block is always a
byte-level subset of the original (original header, original column lines, original
order).

### Budget and the never-empty guarantee

`max_chars` (default 6000) is a hard budget: the weakest tables are dropped first
(the list is best-first, so the best table always survives), and a single table that
still exceeds the budget is kept whole with `over_budget: true` — a block that does
not fit is still a schema, and an empty schema is a guaranteed zero (see the round-2
empty-schema incident: the guard in `dev_tools/run_benchmark.py`, memory I-04).
If the linker cannot produce anything (empty schema, heavy `CREATE TABLE` format, a
question with no usable tokens, no table above `min_score`), the tool keeps today's
`prune_schema` result and records `fallback: prune_schema` with the reason.

## Honesty

* **Inputs are the schema, the question and the evidence — nothing else.** There is no
  parameter for gold SQL, a reference answer or a dev-question file; the test pins the
  signature and greps the module source for those words, and a behavioural test shows
  a question that matches nothing selects nothing even when the gold SQL of the
  benchmark question touches other tables.
* **Measurement vs selection.** Gold SQL appears only in the offline measurement that
  chose the defaults in this document, never in the selector.
* **Degradation is recorded, not hidden.** A missing sentence-transformer weights file
  becomes `embedder: hash` + `embedder_reason`, never a silent switch between arms.
* **No network, no API key, no extra LLM call**: one embed pass per (schema, embedder)
  per process for the optional boost; the default lexical path embeds nothing.

## Measured effect (offline, before any accuracy run)

Provenance: `data_cache/db_schemas.json` (the 8 dev databases), the 241 questions of
`data/bird_large.json`, `embedder=hash` (= `auto` on this machine), `min_score=1.0`,
`cl100k_base` token counts as an approximation of the real tokenizer.

| mode | k | gold tables fully kept | schema chars vs full | schema tokens vs full |
|---|---|---|---|---|
| `tables` | 4 | 209/241 = 86.7 % | 73 % | 74 % |
| `tables` | 6 | **224/241 = 92.9 %** | 82 % | 83 % |
| `tables` | 8 | 231/241 = 95.9 % | 88 % | 89 % |
| `columns` | 6 | **224/241 = 92.9 %** | **57 %** | **44 %** |

Per-database coverage at `tables k=6`: california_schools 23/24, card_games 31/31,
codebase_community 23/27, debit_card_specializing 26/26, financial 26/29,
student_club 26/33, superhero 35/36, toxicology 34/35. The weakest cell,
`student_club` (79 %), is a recall limit of the heuristic, not a bug: at `k=8` the
arm keeps 100 % of gold tables on five of the eight databases and 95.9 % overall.

These are *selector* numbers: they say what the model is allowed to see, not what it
answers. Execution accuracy is measured by `eval-run` against the real endpoint.

## Options

| config field | env | default | meaning |
|---|---|---|---|
| `schema_link` | `TEXT2SQL_SCHEMA_LINK` | `off` | `off` \| `tables` \| `columns` \| `highlight` \| `joins` |
| `schema_link_k` | `TEXT2SQL_SCHEMA_LINK_K` | `6` | tables to keep (join closure fills up to it) |
| `schema_link_min_score` | `TEXT2SQL_SCHEMA_LINK_MIN_SCORE` | `1.0` | minimum table score; none above it → fallback |
| `schema_link_max_chars` | `TEXT2SQL_SCHEMA_LINK_MAX_CHARS` | `6000` | hard character budget for the block |
| `schema_link_embedder` | `TEXT2SQL_SCHEMA_LINK_EMBEDDER` | `auto` | `auto` \| `sentence-transformer` \| `hash` \| `none` |

A typo (`table`, `word2vec`) raises at construction, like an unknown ambiguity mode:
a silently ignored arm would make a run report numbers for a configuration nobody
chose.

## Enabling an arm

The sealed harness builds the tool as `tool_cls(db_uri=..., llm_client=...)`, so the
arm is switched on from the environment (PowerShell, the runbook shell):

```powershell
$env:TEXT2SQL_SCHEMA_LINK = "columns"          # or "tables"
$env:TEXT2SQL_SCHEMA_LINK_K = "6"
$env:TEXT2SQL_SCHEMA_LINK_MIN_SCORE = "1.0"
$env:TEXT2SQL_SCHEMA_LINK_EMBEDDER = "hash"    # auto == hash here (no local ST weights)
$env:TEXT2SQL_PROCEDURE_LOG = "procedures.jsonl"

python -m dev_tools.run_benchmark --check      # plan + procedure_env in the plan
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only ...
```

Do not change these variables between two runs quoted as the same cell: `run.json`
records them under `procedure_env`, so the check is mechanical.

## What the artefact records

Per question (`Text2SQLGenerator.procedure_metadata()["schema_link"]["last"]` and one
JSONL line in `TEXT2SQL_PROCEDURE_LOG`): `mode`, `applied`, `fallback` (when it fired),
`tables` (+ `tables_selected`/`tables_total`), `columns_selected`/`columns_total`,
`schema_chars`, `schema_sha256`, the rendered `block` itself, `max_chars`,
`over_budget`, `reason`, top `scores`, `join_closure`, `embedder` and
`embedder_reason`, `used_evidence`.

Run-level: `run.json` gains a `schema_link` block aggregated from that log
(`schema_link_summary` in `dev_tools/run_benchmark.py`): `modes`, `questions`,
`questions_with_link`, `applied`, `fallbacks`, `over_budget`, mean/max tables,
mean columns, mean/max schema chars, `distinct_schema_sha256`, `embedders`. It is
absent when the arm never ran, so a default run's `run.json` stays quiet.

## Dry run (no database, no model, no network)

```powershell
python -m dev_tools.schema_link_dryrun                       # all five modes
python -m dev_tools.schema_link_dryrun --embedder none       # lexical-only arm
python -m dev_tools.schema_link_dryrun --modes off,tables    # a subset
```

The dry run builds the tool with `engine=None` and a deterministic stub LLM, runs one
question per mode against a schema dump, and asserts per mode:

* `off` — the system and user prompts are byte-identical to the `prune_schema`
  reference, and no link record is produced;
* `tables`/`columns` — `applied` is true, the SHA-256/table/column/char counts are
  recorded, and the system prompt contains exactly the logged block.

Measured on this machine (`student_club`, 1124 chars of schema):

```
-- mode=off:     block 1124/1124 chars (100%), all three checks ok
-- mode=tables:  block 588/1124 chars (52%), tables 4/8, columns 24/48
-- mode=columns: block 471/1124 chars (42%), tables 4/8, columns 19/48
OK: every requested mode behaved as documented, with no database.
```

Artefacts land in `runs/t25-schema-linking/` (`dry_run.json`, `procedures.jsonl`);
`--out` overrides the directory. Exit code `2` means a mode did not behave as
documented, with the failing check named.

## Variants — k, column mode, min_score, sample values (R3-5)

The grid: `k ∈ {6, 12, 20}` × `tables`/`columns` × `min_score ∈ {0.0, 1.0}` ×
`samples ∈ {0, 3}` = **24 cells**, plus the `off` baseline. `min_score=0.0` is the
"no threshold" arm (keep the top-`k` regardless of evidence); `min_score=1.0` is the
"lexical-only" arm (a table must share at least one question token). Everything is
measured offline, once, by `dev_tools/schema_link_sweep.py`:

```powershell
python -m dev_tools.schema_link_sweep --samples --emit-commands
# -> runs/t28-schema-link-sweep/{sweep.json,sweep.md,ex-commands.txt}
```

Provenance: `data/bird_large.json` (241 questions, 8 databases), the recorded light
schemas in `data_cache/db_schemas.json`, `embedder=hash` (a no-op distinction on this
machine: `auto` falls back to it), token counts `tiktoken:cl100k_base` (the R3-1
baseline table above estimates `chars/4`; compare **shares**, not absolute token
counts). Gold tables are parsed from the gold SQL **only to score the selector**; the
selector never sees them. Sample values were read from the live PostgreSQL read-only
(`SELECT * FROM "table" LIMIT 3` per table, 8 databases, no credentials printed) and
rendered as a `-- e.g. v1, v2, v3` suffix on each kept column line.

| cell | mode | k | min_score | samples | schema tokens (mean) | share of baseline | tables (mean) | gold-table recall | EX/241 |
|---|---|---|---|---|---|---|---|---|---|
| `off-baseline` | off | — | — | 0 | 404.5 | 100 % | 6.6 | n/a (no pruning fires) | — (baseline) |
| `k6-tables-min0-samples0` | tables | 6 | 0.0 | 0 | 361.6 | 89 % | 5.3 | 232/241 | — (blocked) |
| `k6-tables-min0-samples3` | tables | 6 | 0.0 | 3 | 749.9 | 185 % | 5.3 | 232/241 | — (blocked) |
| `k6-tables-min1-samples0` | tables | 6 | 1.0 | 0 | 337.3 | 83 % | 4.6 | 224/241 | — (blocked) |
| `k6-tables-min1-samples3` | tables | 6 | 1.0 | 3 | 663.4 | 164 % | 4.6 | 224/241 | — (blocked) |
| `k6-columns-min0-samples0` | columns | 6 | 0.0 | 0 | 202.0 | 50 % | 5.3 | 232/241 | — (blocked) |
| `k6-columns-min0-samples3` | columns | 6 | 0.0 | 3 | 467.2 | 116 % | 5.3 | 232/241 | — (blocked) |
| `k6-columns-min1-samples0` | columns | 6 | 1.0 | 0 | 177.7 | **44 %** | 4.6 | 224/241 | — (blocked) |
| `k6-columns-min1-samples3` | columns | 6 | 1.0 | 3 | 380.7 | 94 % | 4.6 | 224/241 | — (blocked) |
| `k12-tables-min0-samples0` | tables | 12 | 0.0 | 0 | 404.5 | 100 % | 6.6 | 240/241 | — (blocked) |
| `k12-tables-min0-samples3` | tables | 12 | 0.0 | 3 | 857.3 | 212 % | 6.6 | 240/241 | — (blocked) |
| `k12-tables-min1-samples0` | tables | 12 | 1.0 | 0 | 362.5 | 90 % | 5.3 | 231/241 | — (blocked) |
| `k12-tables-min1-samples3` | tables | 12 | 1.0 | 3 | 712.9 | 176 % | 5.3 | 231/241 | — (blocked) |
| `k12-columns-min0-samples0` | columns | 12 | 0.0 | 0 | 242.8 | **60 %** | 6.6 | **240/241** | — (blocked) |
| `k12-columns-min0-samples3` | columns | 12 | 0.0 | 3 | 571.2 | 141 % | 6.6 | 240/241 | — (blocked) |
| `k12-columns-min1-samples0` | columns | 12 | 1.0 | 0 | 200.9 | 50 % | 5.3 | 231/241 | — (blocked) |
| `k12-columns-min1-samples3` | columns | 12 | 1.0 | 3 | 426.8 | 106 % | 5.3 | 231/241 | — (blocked) |
| `k20-tables-min0-samples0` | tables | 20 | 0.0 | 0 | 404.5 | 100 % | 6.6 | 240/241 | — (blocked) |
| `k20-tables-min0-samples3` | tables | 20 | 0.0 | 3 | 857.3 | 212 % | 6.6 | 240/241 | — (blocked) |
| `k20-tables-min1-samples0` | tables | 20 | 1.0 | 0 | 362.5 | 90 % | 5.3 | 231/241 | — (blocked) |
| `k20-tables-min1-samples3` | tables | 20 | 1.0 | 3 | 712.9 | 176 % | 5.3 | 231/241 | — (blocked) |
| `k20-columns-min0-samples0` | columns | 20 | 0.0 | 0 | 242.8 | 60 % | 6.6 | 240/241 | — (blocked) |
| `k20-columns-min0-samples3` | columns | 20 | 0.0 | 3 | 571.2 | 141 % | 6.6 | 240/241 | — (blocked) |
| `k20-columns-min1-samples0` | columns | 20 | 1.0 | 0 | 200.9 | 50 % | 5.3 | 231/241 | — (blocked) |
| `k20-columns-min1-samples3` | columns | 20 | 1.0 | 3 | 426.8 | 106 % | 5.3 | 231/241 | — (blocked) |

`k=20` is identical to `k=12` in every row because the largest dev schema has **10
tables**: the grid saturates at `k=10`, and any `k ≥ 12` is "keep everything".

### The knee

Recall saturates between `k=6` and `k=12` (224/241 → 240/241 in `columns`, i.e. 92.9 %
→ 99.6 %) while `columns k=12 min_score=0.0` still sends **60 %** of the baseline block,
whereas the `tables` arm at the same recall sends **100 %** — so the knee is at
`k≈10–12`, and beyond it the only lever that cuts tokens without touching recall is the
column stage.

One recall detail worth keeping straight: the single 240/241 miss is not a missing
table — BIRD's gold SQL for `toxicology` question 282 references a derived-table alias
`subquery`, which no schema can contain. On real tables the `k≥12` arm keeps 100 %.

### Sample values: cheap to extract, expensive to send

Extraction is cheap and safe (52 `LIMIT 3` reads for all 8 dev databases), but the
render is not: appending three values per kept column adds **+100 % to +230 %** of the
default block (`457`…`857` tokens). Even the cheapest sample cell
(`k6-columns-min1-samples3`, 381 tokens = 94 % of baseline) gives up the entire
schema-link saving, and every `tables` sample cell exceeds the baseline. **Recommendation:
do not adopt sample values** for this arm; if a later arm wants them, cap to a handful
of columns and measure the EX effect separately.

### EX/241 is blocked, not zero

No model endpoint is up on this machine (the only listening socket is the PostgreSQL
tunnel on 5444), and one cell is a full harness run: **≈39 min per 241-question cell**
measured on this card (`runs/t2/e-scale/run.json`: 11:04:57 → 11:44:02). The 24-cell
grid is therefore ≈15.6 GPU-hours, and the honest knee subset is four cells (off,
`k6-tables-min1`, `k6-columns-min1`, `k12-columns-min0`) ≈ **2.6 h** on one server.
`python -m dev_tools.schema_link_sweep --emit-commands` writes the exact per-cell
`run_benchmark` command block (`runs/t28-schema-link-sweep/ex-commands.txt`), and the
A/B task (t26) already owns the first EX cell (`off` vs one linked arm). No EX number in
this table is estimated, extrapolated or invented.

### Recommended default to PROPOSE (not adopted)

* **Primary: `columns`, `k=12`, `min_score=0.0`** — 60 % of the schema block at
  240/241 gold-table recall (effectively 100 % of real tables). It dominates every
  `tables` cell on both axes.
* **If prompt size dominates: `columns`, `k=6`, `min_score=1.0`** — 44 % of the block
  at 92.9 % recall, the cheapest cell that still drops whole tables.
* **Do not adopt** `k=20` (identical to `k=12`), `samples>0` (no saving), or
  `tables`-mode pruning as the default (loses recall for a smaller token cut than the
  column stage achieves at the same recall).
* This is a **proposal**, not a default flip: the default stays `off` until an EX
  comparison exists, and the recommendation is about *which cell to measure next*, not
  about claiming accuracy.

## Non-destructive modes: `highlight` and `joins` (R3-6)

Why they exist: R3-1 measured that the current path already sends every table a query
needs (gold tables present in 240/241 and 876/876 cases; pruning never fires), and of
20 hand-classified OOD failures **zero** would have been fixed by adding a missing
table — 7/20 chose the wrong table, 4/20 read a column from the wrong side of a join,
7/20 were semantic. A `k`-cut can only lose information, so the remaining hypothesis is
**where the model looks**, not *what* it is allowed to see. These two modes keep the
schema intact and add guidance.

| mode | block | measured cost (241 questions, `tiktoken:cl100k_base`) |
|---|---|---|
| `off` (baseline) | full schema | 404.5 tokens |
| `highlight` | full schema, tables ordered by relevance, one `Relevant tables: …` line, per-table `-- highlight: score …; question columns: …` | **524.4 tokens (130 %)**, 4.2 annotations/question |
| `joins` | `highlight` + `Join paths (key columns; derived from the schema only):` with up to 12 `table.col = other.col` edges | **589.1 tokens (146 %)**, 6.7 edges mean, 268 chars of join block; 33/241 questions get zero edges |

Rules, each pinned by a test:

* **Nothing is removed.** Every original `TABLE` header and column line is copied
  verbatim; the marker is an added comment line. A test asserts every original line
  survives, and the dry run asserts it per mode (`63/63 original lines kept`).
  `columns_selected == columns_total` in both modes.
* **Join edges come only from the schema text.** Two tables are joined when they
  contain the same key-column name (`account_id`, `client_id`, `*_code`, `*key`);
  multi-word keys are listed before a bare `id`, the list is deduplicated and capped at
  `MAX_JOIN_EDGES = 12`. A test re-derives every edge's table and column from the
  schema string, and `test_joins_edges_come_only_from_the_schema_and_are_capped` caps a
  20-table fixture.
* **Honest degradation.** A database whose foreign keys are named differently
  (`student_club` is the dev example) yields no edges, and `joins` is then identical to
  `highlight`; that is reported (`join_edges: []`, `join_block_chars: 0`), not faked.
* **Same metadata surface** as the destructive modes: mode, block SHA-256, characters,
  tables/columns, `annotations`, `join_edges`, `join_block_chars`,
  `non_destructive: true`; `run.json.procedure_env` records the switch and
  `run.json.schema_link` the aggregate.
* `MAX_CHARS` is **not enforced** in these modes (truncating would make them
  destructive); the value is recorded only and `over_budget` is set when exceeded.

Enable an arm exactly like the destructive ones:

```powershell
$env:TEXT2SQL_SCHEMA_LINK          = "highlight"   # or "joins"
$env:TEXT2SQL_SCHEMA_LINK_K        = "6"
$env:TEXT2SQL_SCHEMA_LINK_MIN_SCORE= "1.0"
$env:TEXT2SQL_SCHEMA_LINK_EMBEDDER = "hash"
$env:TEXT2SQL_PROCEDURE_LOG        = "procedures.jsonl"
```

Dry run: `python -m dev_tools.schema_link_dryrun` now proves **all five modes** in one
pass (artefacts in `runs/t31-schema-linking/`), with the extra per-mode checks
`non_destructive_every_original_line_present` and `annotations_or_edges_present`.

What is still unmeasured: EX. `highlight` isolates "ordering/attention"; `joins` adds
the join-side hint aimed at the 4/20 wrong-column-side class. Whether either moves
OOD-120 is eval-run's paired measurement — the modes are plumbing plus a hypothesis,
not a result.

## Known limits

* The scorer is lexical first: paraphrases with no shared words (or a question that
  only names a value, e.g. "how many students are in 'Alpha'?") are reached only
  through the optional semantic boost, and its weights are absent on this machine.
* `k=6` keeps 92.9 % of gold tables; the missing 7 % are concentrated in
  `student_club` (79 %) and `codebase_community` (85 %). §Variants measures the whole
  `k` grid: recall saturates at `k≈10–12` (240/241 in `columns` mode at 60 % of the
  block), so `k=12`/`columns` is the recommendation to propose rather than `k=8`.
* Join closure is a name-based foreign-key heuristic: it needs a multi-word key column
  shared by name. A database that names foreign keys differently gets no closure.
* The column stage can only remove columns from tables it selected; if the table
  selection is wrong, the column stage cannot repair it.
* This document measures the selector, not the model. Whether a smaller prompt helps
  BIRD is an eval question with a paired comparison, not a claim here.


## Baseline before the selector (R3-1): what the current path sends, and which identifiers fail

Measured before changing anything, so the selector has a baseline to beat. Artifact:
`runs/schema_linking/schema-audit.json` (`runs/schema_linking/schema_audit.py`, CPU-only) —
a replay of the tool's real call site (`_schema_for` → `prune_schema(db_schema, question,
max_tables=12, min_schema_tables=20)`) over the recorded per-database light schemas of the
runs (`runs/t2/e3fair-bird-large/db_schemas.json`, `runs/ood/ood-scale7b-v2/db_schemas.json`).

| dataset | questions | databases | tables/db (min–median–max) | columns/db (min–med–max) | **pruning fired** | tables sent (min–med–max) | schema tokens (mean / med / max, est.) | gold tables present |
|---|---|---|---|---|---|---|---|---|
| `bird_large` | 241 | 8 | 3 – 7 – 10 | 11 – 52 – 115 | **0 / 241 (0 %)** | 3 – 8 – 10 | 423 / 351 / 891 | 240/241 = 99.6 % |
| OOD held-out | 876 | 10 | 2 – 4 – 17 | 10 – 34 – 89 | **0 / 876 (0 %)** | 2 – 12 – 17 | 396 / 423 / 715 | 876/876 = 100 % |
| OOD subsample | 120 | 10 | 2 – 4 – 17 | 10 – 34 – 89 | **0 / 120 (0 %)** | 2 – 9 – 17 | 396 / 378 / 715 | 120/120 = 100 % |

**The pruner is dead code on both benchmarks.** Its trigger is `min_schema_tables=20` and the
largest database in either benchmark has **17** tables (`movies_4`; `movie_3` 16, `superhero` 10,
`talkingdata` 12). Every question therefore gets the *full* light schema, and the token cost of the
schema block is the full-schema cost — ~314 tokens on average, 581–649 at the top end. The
per-database detail (tables, columns, tokens, gold-table recall) is in the artifact; the two
largest are `movie_3` 16 tables / 89 columns / 581 tokens and `movies_4` 17 / 53 / 383.

*Method notes.* Token counts are **exact `tiktoken:cl100k_base`** counts of the schema block the
model receives (`chars/4` is the recorded fallback); the table above averages over questions, so
`bird_large` is 423 tokens at 8.0 tables per question while the per-database average is 6.5 tables.
The selector sweep's `off-baseline` row above (404.5 tokens, 6.6 tables) reads a different schema
snapshot (`data_cache/db_schemas.json`) instead of the dump each run recorded and averages over
databases — the ~4 % gap is the snapshot, not a disagreement: both measurements agree that **no
pruning fires**. "Gold tables present" uses `sqlglot` to extract table names (a `FROM|JOIN
<ident>` regex counts `EXTRACT(YEAR FROM col)` as a table and over-reports failures on `disney`).
The one `bird_large` miss is the known false positive: BIRD's `toxicology` Q282 qualifies a derived-table
alias (`subquery`), which no schema can contain — on real tables the current path keeps 241/241.

## Runner for OOD scoring: `dev_tools/ood_eval.py`

One command, one report:

```bash
python -m dev_tools.ood_eval --predictions runs/ood/ood-scale7b-v2/query_results.json \
    --gold runs/ood/ood_heldout.json --out runs/ood/scores/ood-scale7b-v2.runner.json \
    --markdown runs/ood/scores/ood-scale7b-v2.table.md [--mode local|normalized|fixed]
```

It reuses the shipped scoring path (`score_runs.score_bird` → `fixed_eval.score_predictions`, the
`mode="local"` semantics the frozen 42.32 % baseline was measured with), adds a `per_database`
block, and prints the markdown table that goes into a report. `mode="normalized"` gives the
dialect-neutral robustness pass on the same file. `tests/test_ood_eval.py` runs the whole path
offline — a fake executor answers SQL from a dictionary — so the plumbing (per-database
aggregation, the markdown table, the CLI writing a report, a failing prediction counted as
`pred_error`) is pinned without a database.

Verified end to end on the shipped OOD predictions: the runner reproduces the sealed ledger
exactly — **620/876 = 70.78 %**, EX over gold-executable **620/811 = 76.45 %**, per-database rows
identical to `runs/ood/scores/ood-scale7b-v2.json` — while writing the same report through the
reused scoring path (`runs/ood/scores/ood-scale7b-v2.runner.json`).

## Which identifiers fail (classification of 20 OOD failures)

Sampled from `runs/t2/scores/ood-scale7b.json`: of 876 questions, **256 score zero, of which 65 are
`gold_error`** (the gold itself does not execute on the live server) and **191 are model failures**.
Twenty are classified, stratified by failure mode (execution error vs wrong result), artifact
`runs/schema_linking/failure-classification.json`:

| label | count | what it means |
|---|---|---|
| `wrong-table-chosen` | 7 | the statement names real tables, but not the ones the question needs (a missing join or a sibling table, e.g. `movie_crew` instead of `movie_cast`) |
| `wrong-column` | 4 | the column exists, but on another table (`t1.director` read from `movies_total_gross`, `city.country` read from `city`) — a join-key mistake |
| `semantically-different` | 7 | right tables, different rows (filter/join/aggregation semantics) |
| `other` | 2 | not schema failures: one runtime data error (`invalid input syntax for type real`), one statement that does not parse |

**Gold table present in the prompt: 20 / 20. Avoidable by including the right table: 0 / 20.**

**What the retrieval must fix:** not recall — every gold table is already in the prompt — but the
*ranking and join path*: it must put the right table and the foreign-key edge in front of the
model, because 11 of these 20 failures are the model picking the wrong table or reading a column
from the wrong side of a join it could have made.
