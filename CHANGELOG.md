# Changelog — LoRA track (`feat/lora-track` sealed; round 2 on `feat/round2`)

Baseline: `main` at `e812451` (the teacher's harness). Everything below is what this branch adds or
changes **on top of that commit**; nothing under `benchmarks/`, `data/` or the root entrypoints
(`bird_benchmark.py`, `ambrosia_benchmark.py`, `bird_evaluate_only.py`) differs from `main` — see
`docs/DECISIONS.md` ADR-001 and the scope check in `docs/VERIFICATION.md` §5.

Paths are relative to `research/advtext2sql/repo/`. One line per file: **path — reason**.

## Applying this branch

`lora-track.patch` (in this directory, ignored by `.gitignore` because it regenerates from history) is a
`git format-patch main --stdout` bundle of every commit above `main` (`e812451`). Two ways to apply it
on a fresh clone of `main`:

```bash
git apply lora-track.patch            # applies the working tree; verified with --check on a fresh clone
git am --keep-cr lora-track.patch     # replays every commit in the bundle; --keep-cr is required
```

`--keep-cr` is not cosmetic: the teacher's blob for `src/.../text2sql_implementation.py` stores CRLF, and
plain `git am` strips CRs from the patch before applying, so it fails on that one file while `git apply`
succeeds. After `git am --keep-cr` the tree is clean and `python -m pytest tests -q` gives
`130 passed, 8 skipped` (the skips need the out-of-repo `data_cache/` SQLite copies); in this working
repository, which has them, the same command gives `138 passed`.

History note: the bundle contains one historical commit that is **not** in our conventional style —
`wip(eval): offline evaluation layer (I1) - safety snapshot before shutdown` (`220991b`). It was taken
before an unscheduled shutdown to avoid losing work, and it is preserved as-is in the history on purpose:
rewriting published-by-us history buys nothing here, while a dishonest re-author would make the record
harder to audit. Every commit added after it follows `feat:` / `fix:` / `test:` / `docs:` / `chore:`.

---

## Round 2 — `feat/round2` (draft, 2026-10-29; t9 commits)

Round 2 continues from the sealed round-1 tip `f4d3d4a`; `feat/lora-track` is frozen and this section
describes what `feat/round2` adds on top of it. **Nothing in this section is committed yet** — it is the
pre-integration draft written by t14 so that the final seal (t9) is a review-and-commit job. Paths are
relative to this repo; files under `research/advtext2sql/` (outside the repo) are named explicitly and do
not enter the patch. The round-1 `lora-track.patch` stays the sealed round-1 bundle; t9 regenerates the
round-2 bundle against `f4d3d4a`.

### Evaluation — the feature × model matrix assembler (`dev_tools/matrix.py`)

- `dev_tools/matrix.py` — CPU-only assembler for the round-2 feature × model matrix: reads per-question
  `fixed_eval` ledgers, prints EX/241, EX/209, per-difficulty and soft-F1 for every arm, plus a paired
  McNemar/10k-bootstrap block against a reference arm; arms without an artifact are printed as
  `blocked`/`not measured` with the command that would produce them, never executed. It cross-checks the
  four 2×2 cells against the independent `2x2-analysis.json` and reports a `DISCREPANCY` finding instead
  of smoothing it over. Reason: round 2 must answer "does the adapter stack with the features?" with one
  table, not with a hand-built spreadsheet.
- `tests/test_matrix.py` — pins the assembler: no socket is opened during a CLI run, a blocked arm never
  starts anything, and a planted mismatch surfaces as a finding. Reason: an assembler that can silently
  "fix" numbers is worse than no assembler.
- `runs/t2/scores/matrix.json` — the assembled artifact (ignored by `runs/`, quoted in `docs/REPORT.md`
  §12). Reason: the table must be reproducible from the ledgers, not retyped.

### Recorder and runner — `run.json` schema v2 (`dev_tools/`)

- `dev_tools/serve_openai.py` — `/health` now reports the provenance a run needs: `base_model`,
  `served_name`, adapter path plus `adapter_model`/`adapter_config` SHA-256, `load_in_4bit`, the effective
  `max_new_tokens`, `requests_served`, `max_completion_tokens_observed` and
  `cuda_max_memory_allocated_bytes`. Reason: the serving process is the only source of truth for what was
  actually loaded and how much VRAM it used; round 1's `run.json` had none of it.
- `dev_tools/run_benchmark.py` — `run.json` schema v2: records `use_evidence`, `max_tokens` +
  `max_tokens_source` (and refuses a `--max-new-tokens` that disagrees with the server), the git commit
  **and dirty-path list**, the tool module's SHA-256, model/adapter ids and hashes, the whole `/health`
  payload before/after the run, request counts and the largest completion; plus `--feature-prompt FILE`,
  which validates the block, wraps the tool so the question becomes `<question>\nFeature block:\n<text>`
  (nothing under `src/` or `benchmarks/` changes), records `feature_prompt {file, chars, sha256_16}`, and
  refuses a missing/empty/oversized file or an Ambrosia + feature combination. Reason: the round-1 E1 vs
  E1-fair mix-up was possible because the two runs were indistinguishable on disk; this makes the
  configuration un-misquotable, and gives the blocked feature arms a runnable path.
- `tests/test_serve.py` — pins the `/health` provenance keys and the request/completion counters.
- `tests/test_local_bench.py` — pins the plan-level recorder (`use_evidence`, effective cap, refusal of a
  disagreeing cap), the feature-block validation/refusal paths and the wrapper behaviour.
- `runs/t2/recorder-proof-bird-small*` — the short re-run that shows the new fields on disk (ignored
  artifact; quoted in `docs/REPORT.md` §10.3).

### Schema guard — an empty schema fetch must never be reported as a run (`dev_tools/run_benchmark.py`)

- `dev_tools/run_benchmark.py` — the last silent-failure hole in the measurement path. `benchmarks/base.py`
  writes `{db_id: tool.db_schema}` and the tool renders `""` when it cannot read a schema (wrong credentials,
  tunnel down); the harness then runs every question against empty prompts and produces a plausible ledger.
  The wrapper now (a) probes **one** database through the tool's own DSN before spending
  (`probe_database_schema` + `preflight_schema_problem`) and refuses on a failed probe, (b) reads the produced
  `db_schemas.json` after the run (`read_schema_health`, minimum 20 non-space characters per block) and
  refuses — exit 2 — when any value is empty or truncated, naming the first databases and pointing at the
  `.env` quoting/CRLF trap, and (c) writes the evidence into `run.json` even when refusing:
  `schemas_bytes_total`, `schemas_db_count`, `schemas_chars_total`, `schemas_empty_count`,
  `schemas_empty_databases`, `schemas_problem`, `schema_probe`, `schemas_allowed_empty`, plus `status:
  refused` and the message. `--allow-empty-schemas` keeps a deliberate empty-schema experiment possible and
  is recorded, so it can never be mistaken for a working fetch. Reason: two round-2 runs finished this way
  (`runs/t2/r2-scale7b-ev`; `runs/ood/ood-base7b-292` scored 5.14 % where the same model with a real schema
  scored 71.67 %) and the second nearly became a headline claim.
- `tests/test_schema_guard.py` — 26 offline tests: healthy / one-empty / truncated / empty-dict / missing /
  unreadable / wrong-shape schema files, the refusal wording, the escape hatch recorded, `first_db_id`, the
  probe (success, redacted failure, skipped), the pre-flight refusal, and both run entrypoints
  (`run_predict_only`, `run_harness`) end to end with a stub harness. Two tests use the real incident dumps
  (`runs/ood/ood-base7b-292` 209 B all-empty, `ood-base7b-120` 9,872 B healthy) and skip on a clean checkout;
  an inline ten-database reproduction always runs.

### Thinking mode — Qwen3-class arms (`dev_tools/serve_openai.py`)

- `dev_tools/serve_openai.py` — explicit reasoning switch plus post-filter, both reported by `/health`.
  `--no-thinking` (and its symmetric `--thinking`) passes `enable_thinking=False` into
  `tokenizer.apply_chat_template` **only when the template advertises the hook** (detected from the template
  source, because `apply_chat_template` silently accepts any Jinja variable); a Qwen2.5-style template is a
  graceful no-op and a tokenizer that rejects the keyword (TypeError) is reported as unsupported instead of
  claiming a mode. `strip_thinking()` removes residual reasoning before the answer leaves the server:
  complete `<think>…</think>` blocks, an unterminated block (the shape produced when the generation cap is
  burned inside reasoning — the part after a blank line is kept, otherwise the tail is dropped), stray tags,
  and a `Thinking:` lead-in; a completion with no block is returned byte-identical. `/health` gains
  `thinking_mode` (`default`/`off`/`on`/`off-unsupported`/`on-unsupported`/`not-applicable`),
  `thinking_template_supported`, `thinking_flag_passed` and `thinking_stripped_count`. Reason: a thinking
  model served without this either spends the cap on "Let me think…" or hands the tool prose, so the arm
  would measure a plumbing bug instead of the model.
- `dev_tools/run_benchmark.py` — `_server_metrics` copies the four thinking keys into `run.json`, so a
  "thinking" arm cannot be quoted as a normal one (or vice versa).
- `tests/test_serve.py` — 16 new offline tests (no model, no GPU, no network): the flag reaches a
  Qwen3-style stub template and is absent from a Qwen2.5-style one, a rejecting tokenizer degrades to
  `off-unsupported`, `Thinking:…`/block/unterminated-block stripping, the untouched clean answer, the
  FakeBackend counter over HTTP, the mutually exclusive CLI flags, the startup line, the closed
  `thinking_mode` vocabulary, and `run.json` provenance.

### Training — scale-up data and the natural Ambrosia arm (`training/`)

- `training/build_data.py` — `--bird-extra DIR` scale-up mode: the official BIRD train split is cleaned by
  the **same** code path as the base corpus (no rule drift), extra ids are file-derived so they cannot
  collide with dev ids, and the split becomes a **group split by database** (`--group-split-keep-train`,
  `--exclude-missing-schema-dbs`) so a db_id is entirely in train or entirely in valid. Reason: training on
  342 rows over 8 databases measures memorisation; the group split is the only split that measures schema
  linkage.
- `tests/test_data.py` — six scale-mode tests: a three-database fixture that reproduces every drop cause
  the real build hits, shape detection of non-data files in a download directory, byte-determinism, the
  schema-missing refusal/exclusion paths, a directory with no question files, and `--drop-evidence`.
- `training/RUN_NOTES.md` §5 — the measured scale build: 6,958 raw pairs → **5,612 usable over 71
  databases**, group split **4,736 train rows / 61 dbs** and **876 valid / 10 dbs**, combined training file
  5,130 rows, sequence p99 1,784 (max_seq_len 3,072 keeps every row).
- `training/AMBROSIA_NATURAL_RECIPE.md` — the natural-class-balance Ambrosia arm, **ready but blocked on
  GPU**: dataset built (480 rows = 146 ambiguous + 334 natural negatives, 432/48 split, 722 combined),
  planner dry-run recorded, measured cost **~40 min GPU** (32 min training at 14.06 s/step + 8 min eval).
- `training/build_data.py` — `--teacher-endpoint` / `--teacher-key-env` distillation plan: records the
  endpoint and key presence and writes `distill_plan.json` **without opening a socket** (`network_calls: 0`,
  enforced by a sockets-poisoned test); the actual distillation path stays `--teacher` + `--teacher-results`
  (teacher SQL kept only if it parses as PostgreSQL and reproduces the gold rows). Reason: the teacher run is
  blocked, and the one command must be ready without pretending it has run.
- `training/distill.md` — the blocked distillation procedure, the exact command, the self-distillation
  fallback and the verification checklist; `docs/REPORT.md` §15 records the arm as blocked.
- `training/RUN_NOTES.md` §7 — the executed **self-distillation** fallback (R2-10 Branch B): 357/357 train
  predictions, 171 execution-identical row sets, filter keep=180 of 342 attempted, dataset 558 combined
  rows, 60/105 training steps in 810.7 s, peak 10.94 GiB; everything labelled `self-distilled`, no teacher
  was contacted. `training/out-selfdistill/` is an artifact (`training/.gitignore`).
- `training/.gitignore` — `out-scale*/` (every scale-build directory, including `out-scale-t2048/`),
  `out-natural/`, `out-selfdistill/` and `out-real/` are artifacts; the recipes/build commands that produce
  them are the sources.

### Tool — procedure arms, both OFF by default (`src/.../text2sql_tool/src/`)

- `src/.../text2sql_tool/src/retrieval.py` — the retrieval few-shot arm: a train-only kNN bank over
  `data/train_queries.json`, scoped per `db_id`, with two independent leakage layers (source allowlist that
  refuses `bird_small`/`bird_large`/`mini_dev` by name, plus a content guard on dev ids **and** exact
  question strings), gold SQL transpiled `sqlite → postgres` and re-parsed before it can become an example,
  an offline sentence-transformer or a dependency-free hashing embedder, and a recorded reason whenever the
  arm cannot activate. Reason: the +4 pp adapter result was inconclusive, so the non-adapter question needs
  an arm that is honest about its bank.
- `src/.../text2sql_tool/src/self_consistency.py` — the sampling arm's pure decision layer: `n` seeded
  candidates, majority over canonical result sets (row order ignored, value types preserved), fallback to a
  majority over normalised SQL text when the database is unreachable, ties to the earliest sample, `n` capped
  at 9 and `temperature=0` refused. No I/O, which is what makes the rules testable offline.
- `src/.../text2sql_tool/src/text2sql_implementation.py` — config wiring: 15 new `Text2SQLConfig` fields plus
  `TEXT2SQL_*` environment overrides (`resolve_procedure_config`), a lazy retriever, `n`-sample generation
  with `extra_create_args` (`temperature`, `seed`) and a per-round time budget, one shared execution helper
  for the vote and the feedback dry-run, `procedure_metadata()` and an optional JSONL procedure log. Both
  arms default to off, so the sealed baseline is unchanged. Also `_create`: a client that rejects
  `extra_create_args` is tolerated and the fallback is logged instead of pretending to have sampled.
- `src/.../text2sql_tool/src/prompts.py` — `FEW_SHOT_PROMPT_TEMPLATE` (English, explicitly says the examples
  are different questions and must not be copied).
- `dev_tools/procedures_dryrun.py` — offline dry run: both arms on, 357-row bank, hashing embedder, stub LLM,
  an injected `ConnectionRefusedError` engine; asserts the block was injected and the vote resolved, writes
  `dry_run.json` / `prompts.txt` / `procedures.jsonl`, exits 2 when an arm did not activate.
- `dev_tools/run_benchmark.py` — `run.json` gains `procedure_env` (every `TEXT2SQL_*` variable of the
  process), because the sealed harness builds the tool without arguments and the environment is the only
  thing that says which arm produced an artefact.
- `tests/test_procedures.py` — 33 offline tests: the two leakage layers against real dev rows, a dev file
  renamed to `train_queries.json`, transpilation, embedder/bank determinism, degradation when the weights are
  absent, block injection + metadata + log without the DSN, the vote rules, both fallbacks, reproducibility,
  the `n`/budget caps, the greedy path staying untouched when both arms are off, the env/constructor
  configuration paths, and `run.json.procedure_env`.
- `docs/PROCEDURES.md` — the options, the measured per-arm prompt cost (`k=1`: 110 cl100k tokens mean;
  `k=3`: 325 mean / 538 max over 240 of 241 dev questions), the latency formula, the metadata schema, how to
  enable an arm on the sealed path, and the dry-run transcript.

### Constrained decoding — the A100 grammar arm (`dev_tools/`, `docs/DECODER.md`)

- `dev_tools/grammars/postgres_select.gbnf` — a SELECT-only PostgreSQL grammar in GBNF (xgrammar
  dialect): `WITH [RECURSIVE]`, set operations, all join flavours, subqueries in `FROM`/`WHERE`/`IN`/
  `EXISTS`, `CASE`, both `CAST` forms, `EXTRACT`, `SUBSTRING(x FROM a FOR b)`, window functions,
  `DISTINCT`/`GROUP BY`/`HAVING`, `ORDER BY … NULLS FIRST|LAST`, `LIMIT`/`OFFSET`, double-quoted
  identifiers. Reason: round 1's "грамматический декодер" was the tool's sqlglot gate plus one repair
  call — post-hoc filtering, not decoding. Measured acceptance with the serving tokenizer: **241/241**
  of the `bird_large` gold after a lossless sqlite→postgres rewrite, 215/241 of the raw sqlite gold,
  211/241 of the gold as the shipped evaluator damages it.
- `dev_tools/guided_proxy.py` — OpenAI-compatible proxy in front of vLLM that injects
  `structured_outputs.grammar` **only** into SQL-generation requests (classified by the tool's own
  system prompt), leaves the ambiguity gate unconstrained, imposes the 512-token cap the harness never
  sends, and serves `GET /health` with the provenance `run_benchmark.py` records plus a
  `guided_decoding` block (grammar file/SHA-256/request counts) and an `nvidia-smi` memory sampler.
  It performs **no** text transformation, so a measured delta is attributable to decoding. Reason: the
  AutoGen client cannot send guided-decoding fields, and vLLM has no `/health` for the recorder.
- `dev_tools/check_grammar.py` — compiles the grammar with the serving tokenizer (xgrammar) and reports
  gold acceptance; `--proxy-log` replays a run's completions and proves they are inside the grammar.
  Reason: a "guided" arm whose output is outside the grammar is not a guided arm.
- `dev_tools/decoder_report.py` — assembles per-arm parse validity (sqlglot), decode-side facts from the
  proxy log (fences, prose prefix, semicolons, cap hits, tokens, latency) next to the `fixed_eval` EX
  numbers; keeps "unparseable" and "unexecutable" apart on purpose.
- `dev_tools/run_benchmark.py` — `--disable-sql-repair`: wraps the tool so the arm spends no second
  generation repairing its own SQL (neither the syntax repair nor the execution repair), records
  `sql_repair` + `tool_wrapper` in `run.json`, and passes the proxy's `guided_decoding` /
  `device_memory` / `latency` blocks through `_server_metrics`. Refused for `--benchmark ambrosia` (no
  SQL to repair). Reason: the grammar delta has to be measured against a surrogate with the *same*
  repair setting, otherwise repair and decoding are confounded.
- `tests/test_guided.py` (new) — pins request classification (SQL guided, ambiguity never), the
  no-rewriting invariant of the injection, `RunState` accounting, the repair-free wrapper (no second
  generation, execution repair skipped), the `plan_run` refusal for Ambrosia, the grammar's structural
  invariants (unique rules, no undefined references, no fences/semicolons/backticks/lower-case
  keywords), and the decoder report's parse-validity/decode-fact counters.
- `docs/DECODER.md` (new) — what was rejected and why (gate+repair surrogate, `guided_choice`,
  `guided_json` envelope, `guided_regex` vs recursion, post-hoc cleanup, llama.cpp fallback, bf16,
  bitsandbytes), the exact serving/proxy configuration, the three environment obstacles (missing `gcc`
  for inductor, missing `nvcc` for FlashInfer's sampler, the ignored `--enforce-eager`), grammar
  coverage and the threats to validity.
- `runs/t2/r2-15-{norep,grammar}-awq/` — the two arms (ignored artifact: `query_results.json`,
  `run.json`, `llm_requests.jsonl`); `runs/t2/scores/r2-15-*.json` — their `fixed_eval` ledgers.
  Reason: `docs/REPORT.md` §13 must be reproducible from files, not from a transcript.

### Docs

- `docs/REPORT.md` §14 — the scale-up A100 cell; §15 — blocked distillation; §16 — the completed 7B
  scale × evidence 2×2 and the mechanism; §17 — the 14B headline; §18 — the procedure/decoder arms
  (retrieval measured, self-consistency not measured); §19 — the OOD check on 10 unseen databases with the
  attribution left open. §9–§13 remain as before.
- `docs/DECODER.md` — why every other constrained-decoding option was rejected, the grammar itself, and the
  measured decoder facts; `docs/PROCEDURES.md` — the two procedure arms and their dry-run verdict.
- `docs/RUNBOOK.md` §11 — round-2 operations: recorder v2, `--bird-extra`/group split, `matrix.py`, the
  Ambrosia natural recipe, the Windows/WDDM paging trap (measured 4,285 MB shared GPU memory, step
  time 33 → 93 s; fix: seq 2,560 + `expandable_segments:True`), the A100 grammar arm and the
  off-by-default procedure/thinking switches.
- `research/advtext2sql/AMBROSIA_CONTRACT_PROPOSAL.ru.md` — the one-page proposal to the course teacher
  (list-of-readings contract; blocked on the teacher, no acceptance assumed); `research/advtext2sql/
  ROUND2_RESULTS.ru.md` — the owner-facing round-2 results (14B headline, 7B mechanism, OOD, procedures,
  blocked arms); `research/advtext2sql/INTEGRATION_INVENTORY.md` — the seal checklist and file inventory.
- `research/advtext2sql/audit_round2.py` + `AUDIT_ROUND2.json` — the independent pre-integration audit:
  **232/232 checks reproduce, 0 findings**, including the decoder, both scale-up arms, the 14B, the OOD set
  and the procedure arms; it caught the stale `cells.base_strict` block in
  `runs/t2/scores/ambrosia-calibration.json` before the seal (root cause fixed, artifact regenerated,
  audit re-run clean).

### Tests

- `tests/test_matrix.py`, `tests/test_guided.py`, `tests/test_procedures.py`,
  `tests/test_schema_guard.py` (new) and `tests/test_data.py`, `tests/test_serve.py`,
  `tests/test_local_bench.py` (extended): the matrix assembler, the grammar proxy/grammar replay, the
  procedure arms, the empty-schema guard (refuse a run whose schema dump has an empty value unless
  `--allow-empty-schemas`), the recorder/`/health` provenance, the feature-block wiring and the
  scale-up/group-split build. Suite: **267 passed** (offline, ~69 s; `docs/RUNBOOK.md` §0).

### Results — the round-2 headline (14B) and the 7B mechanism

**14B headline (R2-18).** A local 14B open model with LoRA, trained on the same **5,612-row / 71-database**
corpus and measured on a rented A100 80 GB with the same protocol: **EX/241 = 130/241 = 53.94 %** (Wilson
47.64–60.12), **EX/209 = 130/209 = 62.20 %**, soft-F1 0.5817, per difficulty 63/91 · 43/89 · 24/61, and
**only 5 unexecutable predictions** (vs 19 for the 7B scale arm, 40 for round-1 base). Paired: vs round-1
adapter+evidence **b=51, c=14, +15.35 pp, p=4.475e-06, CI [+9.13,+21.58]**; vs the frozen team file
**b=43, c=15, +11.62 pp, p=0.000307, CI [+5.39,+17.43] — the pre-registered rule is met**. The comparison is
against a **frozen predictions file** (id-level, no SQL/prompt/config), not a live run of the team's
pipeline; n=241 means CI ≈±6 pp; ceiling 208/241; the 14B needed a rented A100 80 GB (~3.5 h, effective
batch 16) and nothing here proves the recipe fits 16 GB.

**7B scale is the mechanism (R2-5/R2-6).** Both arms on the 4070: **103/241 = 42.74 %** with evidence
(EX/209 49.28 %, soft-F1 0.4935, 19 unexec) and **53/241 = 21.99 %** without (25.36 %, 0.2993, 28 unexec).
Against round-1 base+evidence: **b=30, c=11, +7.88 pp, p=0.0043, CI [+2.90,+13.28]** (rule met); against the
frozen file: **b=32, c=31, Δ+0.41 pp, p=1.0** (the 7B tie stands and is not retracted by the 14B). The
**scale × evidence interaction is +11.20 pp** (CI [+4.98,+17.84], sign test 45/19, p=0.0016): scale helps
only with evidence. Cross-hardware repeat: local 103 vs A100 102, b=1, c=0. The discarded empty-schema A100
attempt is kept as `runs/t2/scores/r2-scale7b-ev.FAILED-empty-schemas.json` (0/241, never a number).
**Post-seal addendum (2026-10-29).** OOD attribution measured and negative: base 86/120 = 71.67 % vs adapter 83/120 = 69.17 % on the same 120-question every-7th subsample (b=8, c=11, Δ −2.50 pp, p=0.648) — the adapter adds nothing on unseen schemas; its 292-row cell is 220/292; the first base-292 attempt (empty schema dump, 15/292 = 5.14 %) is discarded and never quoted; empty-schema guard added to `dev_tools/run_benchmark.py` with tests.\n\n**Secondary arms (measured, not headline).** OOD on 10 held-out databases: 620/876 = 70.78 %
(EX/811 76.45 %, 65 dead gold; attribution to the adapter open — base arm on a 300-question subsample).
Retrieval few-shot k=3 on the 7B scale adapter: 117/241 = 48.55 %, +6.22 pp over the same adapter without
retrieval (b=27, c=12, p=0.0237 — rule met); grammar: 44/241, p=0.1797, contract violations 241/241 → 0/241;
self-consistency not measured. **Audit:** 233/233 checks, 0 findings, including both scale arms, the 14B,
the OOD set and the procedure arms.

---

## Evaluation — offline, non-invasive scoring (`dev_tools/`)

- `dev_tools/fixed_eval.py` — the measurement layer: AST SQLite→PostgreSQL rewrite, gold-damage audit,
  EX/241 + EX/211 with Wilson CIs, per-difficulty and per-database breakdowns, soft-F1, and a paired
  McNemar/bootstrap block. Reason: the shipped evaluator destroys gold before execution
  (30/241 unparseable) and reports one bare number, which makes any adapter comparison meaningless.
  The password is percent-encoded for the DSN (it contains `/`), so the run does not depend on shell luck,
  and a dropped tunnel (`OperationalError`) is retried once instead of being recorded as a gold-query
  failure — measured: question 714 was mis-scored that way while another run held the tunnel's channels.
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
  each other (R1-D22); `--check` also makes the endpoint/plan reviewable before any tokens are spent.
  `--benchmark {bird,ambrosia}` drives `BenchmarkAmbrosia` for S1/S4 and records the choice in the plan.
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
  not enter the patch), `eval/` (score reports and frozen baselines written by `docs/RUNBOOK.md` §3/§8, so
  a DB day leaves `git status` clean) and `lora-track.patch` (the generated patch bundle itself).

---

## Not verified offline / requires the course server

- **The measured EX is the branch's own protocol, not a leaderboard number.** The runs behind
  `## Results` used 4-bit serving, tool v2 (English prompts, sqlglot gate, one repair), greedy decoding and
  `use_evidence=False`; the team's frozen 42.32 % baseline came from a different tool revision, a May DB
  snapshot we cannot re-create and its own decoding choices. The two are not interchangeable, which is why
  the delta against the frozen file is reported as a configuration difference, not as model quality.
- **The live-server facts were measured once and are not reproducible without the tunnel**: gold
  executability 209/241 (32 failures: 30 unparseable + ids 4 and 35 with `UndefinedColumn`) and the
  572 literal mutation are recorded in `docs/EVAL.md` (§"Denominators on the LIVE server") and
  `research/advtext2sql/captain-verified-numbers.md` §E, not re-derived by any test.
- **The official Ambrosia score needs a database**: `build()` reads the schema, so Ambrosia cannot be
  scored offline at all; the 70.83 % constant floor is arithmetic on `data/ambrosia_large.json`, not a run.
  Ambrosia is reported only as ambiguity detection next to that floor (see `## Results`).
- **The S1–S4 harness runs** (small/large × BIRD/Ambrosia) need the live PostgreSQL and a model
  endpoint; `docs/RUNBOOK.md` documents them. The measurement run executed the large-file variants
  (`bird_large`, `ambrosia_large`) plus one unchanged-entrypoint smoke on `bird_small`.
- **vLLM was never run here** (Linux/WSL2 only, no Docker on this machine). The shipped serving path is
  `dev_tools/serve_openai.py`, tested against a fake backend and a tiny in-memory model; a real 7B +
  adapter load is exercised only through the T1 training run's `PeftModel` check.
- **`training/out-real/`** (the 684-row dataset actually used for training) is a local artifact, ignored
  by `training/.gitignore`; `training/build_data.py` regenerates it from `db_schemas.json` in about a
  minute, and the runbook says how.

## Results

Measured with the real harness on the live PostgreSQL through the tunnel: `bird_large` (241) and
`ambrosia_large` (240), `mode="local"` (the shipped evaluator's semantics), greedy decoding, 4-bit serving,
tool v2 with its default `birdsafe` ambiguity gate. Full tables, per-run commands, token counts and the
honest-limitations section live in `docs/REPORT.md`; raw ledgers stay in `runs/t2/` (ignored) because the
report is the record.

**BIRD, `bird_large` (n = 241; EX/209 = over the gold that executes on the server; honest maximum
208/241 = 86.3 % because 30 gold queries are unparseable, ids 4 and 35 die with `UndefinedColumn`, and
id 572 executes against a mutated literal):**

| run | EX/241 | EX/209 | simple / moderate / challenging | soft-F1 | wall clock |
|---|---|---|---|---|---|
| E1 base `Qwen2.5-Coder-7B-Instruct` | **51/241 = 21.16 %** (95 % CI 16.5–26.8) | 51/209 = 24.40 % | 37.4 % / 14.6 % / 6.6 % | 0.2974 | 912 s |
| E3 base + `runs/e3-r16/adapter` | **61/241 = 25.31 %** (95 % CI 20.2–31.2) | 61/209 = 29.19 % | 40.7 % / 12.4 % / 21.3 % | 0.3351 | 1 250 s |
| team's frozen baseline (id-level only) | 102/241 = 42.32 % | — | n/a | — | — |

**Paired (exact McNemar + 10 000 paired-bootstrap resamples, seed 0):**

| comparison | b | c | exact p | ΔEX | 95 % CI | verdict |
|---|---|---|---|---|---|---|
| adapter vs base model (the LoRA effect) | 26 | 16 | 0.164 | **+4.15 pp** | [−0.83, +9.54] pp | **inconclusive at n = 241** |
| adapter vs frozen 42.32 % baseline | 20 | 61 | 5.7e-06 | −17.01 pp | [−23.65, −9.96] pp | worse, configuration-dominated |
| base model vs frozen (context) | 10 | 61 | 4.6e-10 | −21.16 pp | [−27.39, −14.97] pp | worse, configuration-dominated |

The pre-registered rule (claim "better" only if `b − c ≥ 6`, `p < 0.05` and the CI excludes 0) is **not**
met, so the adapter is not claimed to improve BIRD: it fixes 26 questions and breaks 16, which is +10 net
and +4.15 pp but compatible with chance. What the adapter does move is the shape — invalid-SQL predictions
40 → 36, challenging 6.6 % → 21.3 % — i.e. it learned the dialect contract more than schema linkage.

**Ambrosia, `ambrosia_large` (n = 240, `TEXT2SQL_AMBIGUITY_MODE=strict`), reported as ambiguity detection
next to the constant baselines, never as benchmark accuracy:** base model 143/240 = 59.58 % (below the
170/240 = 70.83 % never-ambiguous floor; predicted ambiguous 61, precision 0.279, recall 0.243); adapter
70/240 = 29.17 % because it answered "ambiguous" for **all 240** items (precision 0.292, recall 1.000),
i.e. it collapsed to the always-ambiguous constant. Paired adapter vs base: b = 53, c = 126, exact
p = 4.9e-08, Δ −30.42 pp — significantly worse, and the honest recommendation is a different training mix
or a decision threshold, not more epochs.

Adapter provenance: `runs/e3-r16/adapter`, best validation loss **0.1757** at step 50, **29 m 18 s**,
**11.0 GiB** peak allocated of 15.99 GiB (LoRA r=16, alpha=32, all-linear, 684 rows).

**Was the gap to the frozen 42.32 % configuration or weights? Mostly configuration — and the remainder
stands.** Re-running the same base model with the stock entrypoint's `use_evidence=True` (the flag our own
wrapper omitted) gives **84/241 = 34.85 %** (EX/209 40.19 %, soft-F1 0.4677, 833 s), a paired
**+13.69 pp** over E1 (b = 39, c = 6, exact McNemar p = 5.4e-07, 10k bootstrap CI [+8.71, +19.09]) —
i.e. evidence explains **65 % of the 21.16 pp gap**, the largest single effect measured on this branch.
It does **not** close it: against the frozen file the evidence-enabled base model is still
**−7.47 pp** (b = 21, c = 39, p = 0.0273, CI [−13.69, −1.24]), and that residual cannot be attributed
with the artifacts we have (the frozen file is per-question scores only — no SQL, no prompt, no commit).
Decoding length is ruled out (no prediction reaches the 512-token cap, all 241 parse as PostgreSQL). The
clean adapter comparison is unchanged (E3 vs E1, +4.15 pp, p = 0.164, inconclusive); E3 vs E1-fair
(−9.54 pp, p = 0.0022) is not an adapter measurement because it flips the evidence flag as well. The
missing cell of the 2×2 design is E3-fair (adapter **with** evidence, one ~15-minute run) and no adapter
claim should quote the frozen baseline until it exists (`docs/REPORT.md` §7.4).

Serving VRAM on this card: sampled at 1 Hz, the 4-bit server runs at ~**8.2 GiB** (peak 8,218 MiB of
16,376 MiB, `runs/t2/vram_samples.json`; spot samples during E1/E3 stayed in 7,684–8,009 MiB), versus
16,016 MiB for the bf16 load that Windows paged. `run.json` does not carry a per-run
`torch.cuda.max_memory_allocated` (the server owns the process), so the per-run peak is a sampled figure —
recording it in `run.json` is a follow-up (§7.4).

