# Changelog — LoRA track on `feat/lora-track`

Baseline: `main` at `e812451` (the teacher's harness). Everything below is what this branch adds or
changes **on top of that commit**; nothing under `benchmarks/`, `data/` or the root entrypoints
(`bird_benchmark.py`, `ambrosia_benchmark.py`, `bird_evaluate_only.py`) differs from `main` — see
`docs/DECISIONS.md` ADR-001 and the scope check in `docs/VERIFICATION.md` §5.

Paths are relative to `research/advtext2sql/repo/`. One line per file: **path — reason**.

---

## Evaluation — offline, non-invasive scoring (`dev_tools/`)

- `dev_tools/fixed_eval.py` — the measurement layer: AST SQLite→PostgreSQL rewrite, gold-damage audit,
  EX/241 + EX/211 with Wilson CIs, per-difficulty and per-database breakdowns, soft-F1, and a paired
  McNemar/bootstrap block. Reason: the shipped evaluator destroys gold before execution
  (30/241 unparseable) and reports one bare number, which makes any adapter comparison meaningless.
  The password is percent-encoded for the DSN (it contains `/`), so the run does not depend on shell luck.
- `dev_tools/score_runs.py` — the report's scoring pass: `score bird`, `score ambrosia` (`AMBROSIA floor =
  170/240`) and `compare` (exact McNemar + seeded paired bootstrap against the frozen baseline
  `artifacts/pred_scores.json`, 42.32 % = 102/241). Reason: the shipped evaluator creates one engine per
  question with no timeout and freezes inside `evaluate_bird.py:82` over the tunnel, so measurement has to
  run through `fixed_eval`'s ledgers instead.
- `dev_tools/stub_llm.py` — deterministic offline stand-in for the OpenAI chat client. Reason: no test
  or dry run should need a key, a network or money to exercise the tool contract.
- `dev_tools/paths.py` — one place for the data-cache locations, all derived from the file location and
  overridable by env. Reason: the tools must work from any working directory and never write into
  `data/` or `benchmarks/`.
- `dev_tools/__init__.py` — package index for the above. Reason: the modules are imported as
  `dev_tools.*` from the repo root without an editable install.
- `docs/EVAL.md` — the design and the numbers behind the evaluator, including the live-server
  denominators (`EX/209`, honest maximum `208/241`). Reason: a measurement layer that nobody can
  re-derive is not evidence.

## Tool v2 — the harness-facing Text2SQL generator (`src/.../text2sql_tool/src/`)

- `src/adv_text2sql/mcp_servers/text2sql_tool/src/prompts.py` — English prompt contract (system, SQL,
  ambiguity `OK` / `AMBIGUOUS: <clause>`, two repair prompts). Reason: the questions are English while
  the baseline prompts were Russian, so the model translated and then answered the ambiguity question in
  a third form that the harness scores as "ambiguous" (guaranteed 0).
- `src/adv_text2sql/mcp_servers/text2sql_tool/src/sql_utils.py` — fence/comment stripping, backtick→ANSI
  quotes, first-statement extraction, AST-level `NULLIF(denominator, 0)`, PostgreSQL parse + SELECT-only
  guard. Reason: predictions are executed verbatim, so a markdown fence, a backtick or a non-SELECT
  statement is a silent zero.
- `src/adv_text2sql/mcp_servers/text2sql_tool/src/schema_pruning.py` — deterministic lexical table
  selection with a recall guard (>20 tables only, full-schema fallback). Reason: token control on large
  schemas without embeddings, network or a second model.
- `src/adv_text2sql/mcp_servers/text2sql_tool/src/text2sql_implementation.py` — `Text2SQLGenerator` v2:
  never-raising `query()`, ambiguity modes (`strict` / `birdsafe` / `off`), pruning, sqlglot gate, one
  repair, optional execution feedback, resilient LLM I/O; **plus the F3 repair fix** — a repair only
  follows a generation that returned SQL, and a blank reply consumes a retry instead of a repair. Reason:
  the baseline's `query()` can raise (the harness has no `try/except` around it) and spent 9 LLM calls
  where the budget allowed 6.
- `tests/test_tool.py` — 26 offline tests: harness contract, never-raise, ambiguity table, call budget
  (`2`, `3`, and `2*(R+1)` attempts), sqlglot gate, execution feedback, pruning recall on real BIRD
  questions, English-only prompts, and an autouse guard that fails if any test creates a database engine.
  Reason: the F3 defect and the "shape test dialled a real DSN" defect were both found by verification;
  the guard makes both unregressable.

## Training — data build, distillation, QLoRA (`training/`)

- `training/build_data.py` — BIRD + Ambrosia SFT build with `--self-test`, schema-basis provenance,
  teacher distillation, feature-prompt mode, and hard failure on sqlglot drift, missing schemas or
  dev-set leakage. Reason: the repo's own rewrite destroys 14 training targets and mutates one more;
  training on them would teach the regex's damage.
- `training/teacher_filter.py` — keeps a teacher answer only if it parses as PostgreSQL **and** its
  execution result matches gold through an injected executor. Reason: imitating unverified SQL adds
  variance, not capability.
- `training/train_qlora.py` — QLoRA SFT planner/runner with zero heavy imports at module load, a `--plan`
  dry run that writes provenance, a TRL compatibility decision, completion-only loss and a smoke-data
  guard. Reason: the run must be reviewable (and testable) without a GPU, torch or 12 GiB of weights.
- `training/README.md` — runbook for the data build, both distillation modes and the training planner.
- `training/RUN_NOTES.md` — the measured first runs (1.5B smoke, 7B 125-step run) plus the environment
  traps that cost real time (transformers<5, trl 0.18.2, accelerate>=1.10, HF Xet, symlinks).
- `training/requirements-train.txt`, `training/constraints-train.txt` — the frozen training environment
  (85 pins) and the small constraint file that forced the resolution.
- `training/fixtures/teacher_predictions.json`, `training/fixtures/teacher_results.json` — offline
  fixtures for the distillation filter, so its tests never touch a database.
- `training/.gitignore` — keeps `out*/`, `runs/` and venvs out of the patch. Reason: adapters and
  checkpoints are artifacts; the patch must carry code, not gigabytes.

## Offline substrate — schemas, SQLite harness, local server, run recorder (`dev_tools/`)

- `dev_tools/dump_schemas.py` — writes `db_schemas.json` (`{db_id: schema text}`) from live PostgreSQL
  or from the SQLite copies, byte-identical to the tool's own renderer and validated with
  `--verify-against-tool`. Reason: training and serving must see the same schema string, and the PG
  migration folded 218 identifiers to lower case.
- `dev_tools/local_bench.py` — `bird-sqlite-official` protocol: gold execution, official SQLite EX and
  the distillation filter on the 8 SQLite copies. Reason: gives a fast, DB-free comparison loop; explicitly
  labelled as *not* the course harness so its numbers cannot be confused with EX/241.
- `dev_tools/run_benchmark.py` — runs the real course harness once into
  `data_cache/runs/<run-id>/` (`query_results.json`, `db_schemas.json`, `run.json`) and refuses to
  overwrite a run. Reason: the harness writes its outputs into the CWD, so two runs silently overwrite
  each other (R1-D22); `--check` also makes the endpoint/plan reviewable before spending tokens.
- `dev_tools/serve_openai.py` — local OpenAI-compatible server (transformers + optional PEFT adapter,
  `--fake` for contract tests). Reason: vLLM is Linux-only and this workstation has no Docker, so the
  harness needs a reachable endpoint that speaks the same three env vars.
- `dev_tools/requirements-serve.txt` — pinned dependencies of that server.
- `dev_tools/fastmcp_shim/sitecustomize.py` — PYTHONPATH shim that stubs `fastmcp` when the MCP runtime
  is absent. Reason: `mcp_servers/__init__.py` imports `fastmcp` for a serving dependency the benchmark
  never uses, which otherwise blocks the harness import on a machine without it.
- `docs/LOCAL_BENCH.md` — what the SQLite substrate buys, what it does not, and the command list.

## Tests

- `tests/conftest.py` — puts `src`, `dev_tools` and the repo root on `sys.path` and provides `FakeDB`, an
  in-process PostgreSQL stand-in. Reason: the whole suite must run without a database or network.
- `tests/test_eval.py` — 36 tests pinning the damage (30/241, 2/22, 3 silent), the statistics hand
  checks (Wilson, McNemar, bootstrap), the three modes and the CLI.
- `tests/test_local_bench.py` — 22 tests: renderer parity with the tool, DSN round-trip, deterministic
  gold artifacts, official set-equality semantics, distillation drop reasons.
- `tests/test_serve.py` — HTTP contract tests with a fake backend and a tiny in-memory model (no GPU, no
  download).
- `tests/test_data.py` — 19 tests for the training pipeline, including a meta-path blocker proving the
  planner imports no heavy module.
- `tests/test_tool.py` — see the tool section above.

## Docs

- `docs/EVAL.md`, `docs/LOCAL_BENCH.md` — see the sections above.
- `docs/VERIFICATION.md` — the independent verification report (V1, V2, V2.7): commands with exit codes,
  the reconciled damage table, the denominators, the scope audit and the findings.
- `docs/RUNBOOK.md` — the DB-day procedure (this commit).
- `docs/DECISIONS.md` — the architecture decisions with context, decision and consequence (this commit).
- `CHANGELOG.md` — this file.

## Repo hygiene

- `.gitignore` — `runs/` (intentional extension beyond the upstream file, captain-approved: a LoRA run
  launched from the repo root with a relative `--out` writes adapters/checkpoints into `runs/`, which must
  not enter the patch) and `lora-track.patch` (the generated patch bundle itself).

---

## Not verified offline / requires the course server

- **The adapter's execution accuracy is not in this patch.** The EX/241, EX/211, EX/209 and Ambrosia
  accuracy of `runs/e3-r16/adapter` are produced by the measurement run **T2**; its artifact is
  `docs/REPORT.md` and its numbers land in a follow-up commit. This patch therefore does **not** claim a
  leaderboard-comparable number, and no EX figure here should be quoted as one.
- **The live-server facts were measured once and are not reproducible without the tunnel**: gold
  executability 209/241 (32 failures: 30 unparseable + ids 4 and 35 with `UndefinedColumn`) and the
  572 literal mutation are recorded in `docs/EVAL.md` (§"Denominators on the LIVE server") and
  `research/advtext2sql/captain-verified-numbers.md` §E, not re-derived by any test.
- **The official Ambrosia score needs a database**: `build()` reads the schema, so Ambrosia cannot be
  scored offline at all; the 70.83 % constant floor is arithmetic on `data/ambrosia_large.json`, not a run.
- **The S1–S4 harness runs** (small/large × BIRD/Ambrosia) need the live PostgreSQL and a model
  endpoint; `docs/RUNBOOK.md` documents them, none has been executed for this patch.
- **vLLM was never run here** (Linux/WSL2 only, no Docker on this machine). The shipped serving path is
  `dev_tools/serve_openai.py`, tested against a fake backend and a tiny in-memory model; a real 7B +
  adapter load is exercised only through the T1 training run's `PeftModel` check.
- **`training/out-real/`** (the 684-row dataset actually used for training) is a local artifact, ignored
  by `training/.gitignore`; `training/build_data.py` regenerates it from `db_schemas.json` in about a
  minute, and the runbook says how.

## Results (pending T2)

The adapter exists — `runs/e3-r16/adapter`, best validation loss **0.1757** at step 50, **29 m 18 s**
wall clock, **11.0 GiB** peak allocated on a 16 GB card — and its execution-accuracy numbers are produced
by the T2 run, whose artifact path is `docs/REPORT.md`; they land in a follow-up commit. T2's measurements
go through `dev_tools/score_runs.py` (`score --kind bird`, `score --kind ambrosia`, `compare` against the
frozen 42.32 % baseline) and its raw run directories stay under `runs/t2/`, which is ignored by
`.gitignore` — the report carries the numbers, not the gigabytes.
