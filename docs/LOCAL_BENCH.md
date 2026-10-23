# Local BIRD substrate — SQLite copies, `db_schemas.json`, offline harness

What this directory does: it makes the BIRD half of the course project runnable
**without the teacher's PostgreSQL** — a real execution harness in the official
BIRD dialect (SQLite), the schema dump the QLoRA pipeline needs, and a verified
path from teacher predictions to a clean distillation set.

It is *not* a replacement for the course harness. Read §1 before quoting a
number.

```
data_cache/                     (outside the git repo, self-ignoring .gitignore)
  minidev.zip                   800,943,648 bytes — one-time download
  dev_databases/<db>/<db>.sqlite  8 BIRD dev databases, 865,845,248 bytes total
  dev_databases/manifest.json   sha256 + byte size per database
  db_schemas.json               8 light schemas built from the SQLite files
  db_schemas.manifest.json      per-db table/column counts + sha256 of each schema
  db_schemas.drift.json         SQLite-vs-PostgreSQL schema drift (why PG wins)
  artifacts/                    gold / score / distill artifacts of this run
  runs/<run-id>/                one directory per real-harness run (see §10)
```

---

## 1. Which number is which — three protocols, never interchangeable

| protocol | where it runs | how gold is treated | gold items that cannot score | what it is for |
|---|---|---|---|---|
| **course PostgreSQL harness** (`benchmarks/evaluate_bird.py`) | teacher's PostgreSQL 15.15 through the SSH tunnel | rewritten `sqlite → postgres`, then a regex adds `NULLIF` | **32/241** (measured live): 30 unparseable + ids 4, 35 (`UndefinedColumn: column t1.Charter School (Y / NULLIF(N,0)) does not exist`); id 572 executes but was silently mutated | the grade. This is the only number the teacher sees. |
| **PG, safe rewrite** (`dev_tools/fixed_eval.py --mode fixed`) | same server | AST-safe rewrite | 0 | honest model quality on PostgreSQL (**EX/241 + per-difficulty Wilson CIs + soft-F1**); see `docs/EVAL.md` |
| **official BIRD SQLite** (`dev_tools/local_bench.py`, **this file**) | local SQLite files, no server | executed **verbatim** | **0** execute errors; **1** timeout at the official 30 s budget (701) | fast iteration, offline, and the dialect every public BIRD number is measured in |

The local harness stamps every artifact with
`"protocol": "bird-sqlite-official"`, `"harness": "offline-sqlite (official BIRD
dialect) - NOT the course PostgreSQL harness"`, `"mode": "sqlite_ex_official"`
and a `not_comparable_with` field. A number from here is never an `EX/241` from
the teacher's harness.

### Why the two failure counts differ (30 vs 32 vs 33 vs 1)

* **30** — gold queries whose rewritten text does not *parse* as PostgreSQL
  (offline audit; `docs/EVAL.md`, `gold_damage_report`). This is a lower bound on
  the damage: it can only see syntax.
* **32** — gold queries that fail on the live server. The extra two are ids 4
  and 35: the regex also fires inside the quoted identifier
  `"Charter School (Y/N)"`, producing a *syntactically valid* column name that
  does not exist. Parse-based audits cannot see this; execution can.
* **33** — 32 + id 572 (`'… Weka and/or RapidMiner …'`: the regex mutates a
  string literal, the query executes, the answer is wrong without any error).
* **1** — the only SQLite failure, and it is not damage but cost: question 701
  (`codebase_community`) joins 40 325 users to 91 966 posts with no usable index
  and needs ~364 s here, so it exceeds the official 30 s budget.

Consequences worth keeping straight:

* the PG harness cannot score more than **241 − 32 = 209** questions, and an
  honest submission can match at most **208** of them (572 is mutated), i.e. a
  hard ceiling of **208/241 = 86.3 %** there;
* the SQLite protocol has **no such ceiling**: a perfect submission scores
  **240/241 = 99.59 %** at the 30 s budget (`ex_over_scoreable = 240/240 =
  100 %`) and **241/241** with the budget disabled;
* a score from SQLite and a score from PostgreSQL are *different measurements*,
  not the same measurement with noise. Report them side by side, labelled.

---

## 2. Getting the databases (one-time, never in tests)

The canonical URL in the BIRD README (`https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip`)
is **not reachable from this machine**: DNS resolves (8.141.181.247), but TCP/TLS
never completes (`curl -sI` → exit 28 after 40 s; ranged GET → exit 35 "SSL
connect error" after 82 s). The OSS **accelerate** endpoint serves the same
object and does work here:

```bash
cd research/advtext2sql/data_cache
curl -L -C - --retry 5 --retry-all-errors \
  -o minidev.zip https://bird-bench.oss-accelerate.aliyuncs.com/minidev.zip
# 800,943,648 bytes, HTTP 206, ~14 MB/s (56 s on this machine)
```

The archive layout differs from the README (`minidev/MINIDEV/…`, not
`minidev/mini_dev_data/…`):

```
minidev/MINIDEV/dev_databases/<db>/<db>.sqlite
minidev/MINIDEV/dev_databases/<db>/database_description/*.csv
minidev/MINIDEV/mini_dev_sqlite.json          # the official question/gold file
minidev/MINIDEV/mini_dev_sqlite_gold.sql
minidev/MINIDEV/dev_tables.json
```

Extract only the 8 databases this course uses (the package also ships
`formula_1`, `european_football_2`, `thrombosis_prediction`, which are not part
of the benchmark here):

```bash
python - <<'PY'
import os, zipfile
DBS = ["california_schools","financial","toxicology","card_games",
       "codebase_community","superhero","student_club","debit_card_specializing"]
root = r"research/advtext2sql/data_cache"
z = zipfile.ZipFile(os.path.join(root, "minidev.zip"))
for db in DBS:
    member = f"minidev/MINIDEV/dev_databases/{db}/{db}.sqlite"
    out = os.path.join(root, "dev_databases", db)
    os.makedirs(out, exist_ok=True)
    with z.open(member) as src, open(os.path.join(out, f"{db}.sqlite"), "wb") as dst:
        dst.write(src.read())
PY
```

Everything downloaded or extracted lives under `research/advtext2sql/data_cache`,
which contains its own `.gitignore` (`*` plus `!.gitignore`) — it is outside the
git repository and is never committed. **Tests never download**: they skip
themselves when the files are absent (`tests/test_local_bench.py`).

---

## 3. Schemas: `db_schemas.json`

The training prompt must contain the exact string the tool prints at inference
(`Text2SQLGenerator._get_db_schema_light`):

```
TABLE <table>
  - <column> (<type>)
<blank line between tables>
```

`dev_tools/dump_schemas.py` re-implements that renderer step by step and proves
it byte-identical against the real class, for the live server *and* for SQLite.

```bash
cd research/advtext2sql/repo

# 1) live PostgreSQL (preferred: this is what the course harness serves)
python -m dev_tools.dump_schemas --bird-only --out ../data_cache/db_schemas_pg.json
python -m dev_tools.dump_schemas --bird-only --out ../data_cache/db_schemas_pg.json \
    --verify-against-tool california_schools      # same string as the tool prints

# 2) offline fallback from the SQLite copies (works with the tunnel down)
python -m dev_tools.dump_schemas --sqlite-dir ../data_cache/dev_databases --bird-only \
    --out ../data_cache/db_schemas.json --verify-against-tool california_schools

# 3) validate any dump (a teammate's artifact, a stale file) without any database
python -m dev_tools.dump_schemas --validate ../artifacts/db_schemas_bird.json

# 4) show how two dumps differ (casing, types, missing databases)
python -m dev_tools.dump_schemas --sqlite-dir ../data_cache/dev_databases --bird-only \
    --out ../data_cache/db_schemas.json --compare-with ../artifacts/db_schemas_bird.json
```

Credentials come from `repo/.env` (`DB_CLIENT_HOST`, `DB_PORT`, `DB_USER`,
`DB_PASS`; values are quoted there and are stripped). **The password contains
`/`**, so it must be percent-encoded before it goes into a URL — `postgres_dsn()`
does that (`quote(..., safe="")`) and a test asserts the round-trip through
`sqlalchemy.engine.make_url`. There is no synth-from-gold path: a `db_id` that
cannot be reflected is *reported*, never invented (`--skip-missing` is required
to write a partial dump, and the manifest lists what is missing).

### Which schema should training use? PostgreSQL

Measured drift between the two dumps for the same 8 databases
(`data_cache/db_schemas.drift.json`):

| difference | count | example |
|---|---|---|
| databases | 0 | both cover all 8 |
| identifier **casing** | 218 (2 tables + 216 columns) | SQLite `satscores.AvgScrMath` → PG `satscores.avgscrmath`; SQLite table `postHistory` → PG `posthistory` |
| type text | 59 | SQLite `INTEGER` → PG `BIGINT` |
| columns added or lost | 0 | — |

The migration into PostgreSQL created **unquoted** identifiers, so PostgreSQL
folded them to lower case in 4 of the 8 databases (`california_schools`,
`card_games`, `codebase_community`, `debit_card_specializing`). The tool prints
what the server reports, so at serving time the prompt says `avgscrmath` while
the BIRD gold SQL (and any model fine-tuned on the SQLite schema) says
`AvgScrMath`. That is exactly the silent training/serving drift the renderer
test exists to prevent:

> **Train on the PostgreSQL dump** (`artifacts/db_schemas_bird.json`, or
> regenerate `data_cache/db_schemas_pg.json` while the tunnel is up). Use the
> SQLite dump only to keep working when the tunnel is down, and regenerate
> before a run that will be graded.

```bash
# the captain's live dump, validated here, is the recommended training input
python -m dev_tools.dump_schemas --validate ../artifacts/db_schemas_bird.json
python ../training/build_data.py \
    --schemas ../artifacts/db_schemas_bird.json \
    --train data/train_queries.json --ambrosia data/ambrosia_train.json \
    --out ../data_cache/training_out
```

---

## 4. Gold execution: 241 questions, one timeout, zero errors

```bash
cd research/advtext2sql/repo
python -m dev_tools.local_bench gold --answers data/bird_large.json --timeout 30 \
    --out ../data_cache/artifacts/gold_local_results.json
```

Measured (budget = the official 30 s):

| set | n | ok | error | timeout | wall clock |
|---|---|---|---|---|---|
| `bird_small.json` | 22 | 22 | 0 | 0 | ~0.5 s |
| `bird_large.json` | 241 | 240 | 0 | **1** (`701`) | ~37 s |
| `bird_large.json`, `--timeout 0` | 241 | 241 | 0 | 0 | ≈6 min (701 alone ≈ 364 s) |

The artifact is deterministic (no timestamps, no durations), so two runs are
byte-identical — the test asserts it. `per_question` carries
`status ∈ {ok, error, timeout, ambiguous, missing_db}`, the row count and the
error text; `error_ids` / `timeout_ids` are the ids to look at.

---

## 5. Scoring an adapter offline (official SQLite EX)

```bash
python -m dev_tools.local_bench score \
    --predictions query_results.json \
    --answers data/bird_large.json \
    --out ../data_cache/artifacts/local_score.json
```

* `EX = set(predicted_rows) == set(gold_rows)` — official BIRD semantics:
  order-insensitive **and** duplicate-blind, no numeric tolerance, no
  column-name check.
* A prediction that errors or times out scores 0; the error ids are listed
  (`pred_error_ids`, `pred_timeout_ids`).
* Questions whose **gold** is unusable are excluded from `ex_over_scoreable` (the
  SQLite analogue of `EX/211`) but stay in `ex`.
* Output fields: `ex`, `ex_percent`, `n_correct`, `n_scored`,
  `n_gold_unusable`, `ex_over_scoreable`, `by_difficulty`, `per_question`.

Reference points from this run (gold used as predictions, i.e. a perfect
submission):

| answers | EX (official SQLite) | note |
|---|---|---|
| `bird_small.json` | 22/22 = 100 % | `ex_over_scoreable = 1.0` |
| `bird_large.json` @ 30 s | **240/241 = 99.59 %** | 701 is a gold timeout, so 240/240 of the scoreable ones |

---

## 6. Distillation: teacher predictions → clean SFT source

```bash
python -m dev_tools.local_bench distill \
    --predictions ../data_cache/artifacts/teacher_demo_predictions.json \
    --answers data/train_queries.json \
    --out ../data_cache/artifacts/distill_sft.json --format jsonl
```

Keeps a teacher answer only if it **executes** on the real database *and* its
rows equal the gold rows. Everything else is counted, not silently dropped:

`missing_prediction`, `gold_ambiguous`, `gold_unusable`, `pred_error`,
`pred_timeout`, `mismatch`, `missing_db`.

Each kept record is
`{question_id, db_id, difficulty, question, evidence, sql, rows}` — the teacher
SQL is verified to reproduce the gold answer on the real data, which is what
makes it safe to imitate. The report (counts + `kept_ids` + per-difficulty
breakdown) is written next to the records as `<out>_report.json`.

Demonstrated on the real 241 questions with a deliberately damaged teacher file
(200 correct answers, 11 mismatches, 19 non-executable, 10 omitted, 1 gold
timeout): **kept 200/241** with exactly those drop counts
(`data_cache/artifacts/distill_sft.json`, `…_report.json`).

---

## 7. What this buys, and what it does not

**Buys**

* an execution harness with **no server, no tunnel, no network** — the official
  BIRD dialect, so adapter comparisons are meaningful and fast;
* a measured, reproducible statement about BIRD gold quality on SQLite
  (0 execute errors, 1 timeout) that is independent of the PostgreSQL rewrite;
* `db_schemas.json` for all 8 databases in the exact tool format, which
  **unblocks the QLoRA data build** without waiting for the course server;
* a tool-parity test, so training and serving cannot drift silently;
* a distillation filter that only keeps answers verified against the real data.

**Does not buy**

* the course's numbers: `EX/241`/`EX/211` on PostgreSQL include the regex damage
  (32 dead + 1 mutated) and the PostgreSQL planner's behaviour. A SQLite EX is
  **not** the grade and must not be reported as one.
* the teacher's exact rubric — the grading script, its seeds, `MAX_RETRIES` and
  the small/large file choice are still unknown (see `r1-repo-mechanics.md`);
* PostgreSQL-only syntax fidelity: gold is SQLite dialect, so a model that writes
  PostgreSQL-isms can look better here than it will in the harness;
* the 30 s budget's true cost on the server: 701 takes ~364 s on SQLite; on the
  live server it failed in the captain's smoke run (id 701 was among the 32
  failures) — both harnesses effectively lose it;
* anything Ambrosia: those databases have no SQLite copies here.

---

## 8. Tests

```bash
cd research/advtext2sql/repo
python -m pytest tests/test_local_bench.py -q     # 22 tests, offline, ~7 s
python -m compileall -q dev_tools
```

The suite pins: the renderer equality with the tool, the 8-database schema
coverage against real `sqlite_master` metadata, the light-format validator,
percent-encoded DSN round-trip, `.env` parsing, the deterministic gold artifact,
the 701 timeout, official set-equality semantics (order/duplicates), the
gold-vs-prediction failure split, distillation drop reasons, the CLI of all three
commands, the protocol naming, and the run wrapper's refusal to spend tokens or
overwrite a run. Tests that need the extracted databases skip themselves when
`data_cache/dev_databases` is absent.

---

## 9. Status of the live tunnel (context for these numbers)

The tunnel was up at 13:0x on 2026-10-23 (PostgreSQL 15.15, 900 databases, all 8
BIRD dev databases present, `readonly_user`). It died mid-session ("server closed
the connection unexpectedly" on every retry while the local port still
accepted TCP), so the PostgreSQL schema dump used for the drift table was the
captain's `artifacts/db_schemas_bird.json`, validated here with
`--validate` and compared with `--compare-with`. Everything in this document
except the drift table is reproducible **without** the tunnel.

---

## 10. Running the real harness and recording it (`dev_tools/run_benchmark.py`)

The harness writes `query_results.json` and `db_schemas.json` into the current
directory, so two runs overwrite each other (R1-D22). This wrapper gives every
run its own directory under `data_cache/runs/<run-id>/` and refuses to reuse a
non-empty one.

```bash
cd research/advtext2sql/repo

# 1) plan only: validates inputs, prints the endpoint/tool/commit, writes nothing
python -m dev_tools.run_benchmark --check \
    --run-id 2026-10-23-baseline \
    --query-file data/bird_small.json --answer-file data/bird_small.json \
    --db-url "$BENCHMARK_DB_URL" \
    --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"

# 2) the real run (spends tokens; the captain must approve the endpoint first)
python -m dev_tools.run_benchmark --allow-llm-spend \
    --run-id 2026-10-23-baseline \
    --query-file data/bird_small.json --answer-file data/bird_small.json \
    --db-url "$BENCHMARK_DB_URL" \
    --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"
```

Records per run: `query_results.json` (predictions), `db_schemas.json` (the
schemas the tool actually saw — compare it with `dump_schemas.py` output),
`run.json` (argv, tool class, model, endpoint, file hashes, git commit,
timestamps, accuracy summary). Needs `DB_USER`/`DB_PASS` in the environment and
a reachable PostgreSQL. The API key is read from `$LLM_API_KEY`
(`--llm-api-key-env` to change it) and is **never** accepted as a command-line
argument. `run_benchmark` imports `benchmarks.*` and the tool class only when
the run actually starts, so `--check` works without `fastmcp`, `autogen` or a
provider key.

