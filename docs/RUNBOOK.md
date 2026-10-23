# RUNBOOK — DB day for the LoRA track

Everything here is copy-pasteable from `research/advtext2sql/repo` (call it `$REPO`) unless a step says
otherwise. The procedure turns a fresh checkout of `feat/lora-track` plus `.env` credentials into the
four graded numbers (S1–S4) and the paired test that says whether the adapter helped.

Two hard rules, from `docs/DECISIONS.md`:

* the teacher's harness is never edited — measurement code lives in `dev_tools/` (ADR-001);
* every reported BIRD number is quoted as `EX/241` **and** `EX/211` (ADR-005), and the Ambrosia number is
  always shown next to the 70.83 % constant floor.

Wall-clock estimates are from the measured runs on this workstation (RTX 4070 Ti SUPER 16 GB, Windows,
Python 3.12), not from a budget.

---

## 0. Prerequisites (5 min, once per session)

```bash
cd research/advtext2sql/repo
git branch --show-current                       # expected: feat/lora-track
git status --porcelain                          # expected: empty
python -m pytest tests -q                       # expected: 138 passed, ~35 s (no DB, no network)
```

Secrets live in `$REPO/.env` (gitignored): `BENCHMARK_DB_URL`, `DB_USER`, `DB_PASS`, `DB_HOST`,
`DB_PORT`, `DB_CLIENT_HOST`, `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SSH_PASS`, `LOCAL_FORWARD_PORT`.
Export them into the shell before the DB steps — bash: `set -a; . ./.env; set +a`;
PowerShell: `Get-Content .env | Where-Object {$_ -match '=' -and $_ -notmatch '^#'} | ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$k" $v.Trim('"') }`.

Expected `python -m pytest` output (the last line is the contract):

```
138 passed in 34.62s
```

**Fallback:** a non-empty `git status` means someone left work uncommitted — do not start a run until it
is explained; the patch (`lora-track.patch`) is regenerated from a clean tree.

---

## 1. Tunnel + DB ping (2 min)

The benchmark PostgreSQL listens on `10.11.1.6:5444`, reachable only through the MIPT jump host. Either
open the forward by hand:

```bash
ssh -N -L 5444:10.11.1.6:5444 "$SSH_USER@lnsigo.mipt.ru" -p 2278
```

…or use the team's keepalive wrapper (paramiko, auto-reconnect), from `$REPO`:

```bash
python ../ssh_tunnel.py          # scripts live one level above the repo, next to data_cache/
```

Verify the forward, then the server (same working directory, `$REPO`):

```bash
python - <<'PY'
import socket
s = socket.create_connection(("127.0.0.1", 5444), timeout=5); s.close(); print("forward: open")
PY
python ../db_probe.py            # inventories databases
```

Expected: `forward: open`, then a list containing the 8 BIRD dev databases (`california_schools`,
`card_games`, `codebase_community`, `debit_card_specializing`, `financial`, `superhero`, `student_club`,
`toxicology`) and the Ambrosia families `attachment_*`, `scope_*`, `vague_*`; server PostgreSQL 15.15.

**Fallback:** `Test-NetConnection 127.0.0.1 -Port 5444` false ⇒ the tunnel died (it died mid-session
once: "server closed the connection unexpectedly" while the port still accepted TCP). Restart the
wrapper, re-check. If the jump host itself is unreachable, jump to §10 — every offline number stays
producible, only S1–S4 and the live denominators are blocked.

---

## 2. Schema dump for **all** training databases (2 min)

The prompt a model is trained on must contain the exact string the tool prints at inference, so the dump
comes from the live server, through the tool's own renderer:

```bash
python -m dev_tools.dump_schemas \
    --out ../data_cache/db_schemas_pg.json \
    --verify-against-tool california_schools
```

Without `--bird-only` the dumper takes every `db_id` referenced by `data/*.json` — the 8 BIRD databases
**and** the Ambrosia training/dev databases — which is what the Ambrosia head needs.

Expected: a dump plus `<out>.manifest.json` listing every `db_id`, its source (`postgres`), table/column
counts, and `verify-against-tool: byte-identical for california_schools`; exit 0. A `db_id` that cannot
be reflected is **reported, never invented**; `--skip-missing` is required to write a partial dump and the
manifest then names what is missing.

**Fallback A — tunnel down:** build from the SQLite copies instead (works, but the SQLite schema differs
from the server in identifier casing, see ADR-006):

```bash
python -m dev_tools.dump_schemas --sqlite-dir ../data_cache/dev_databases \
    --out ../data_cache/db_schemas.json --verify-against-tool california_schools
```

**Fallback B — use the captain's validated dump:** `python -m dev_tools.dump_schemas --validate
../artifacts/db_schemas_bird.json` (offline; prints coverage and format errors). Copy it to
`$REPO/training/db_schemas.json`. Do **not** train on a dump that fails `--validate`.

Cross-check against the tool the harness will actually use:

```bash
python -m dev_tools.dump_schemas --sqlite-dir ../data_cache/dev_databases --bird-only --out /tmp/x.json \
    --compare-with ../artifacts/db_schemas_bird.json       # expected: 218 casing + 59 type differences
```

---

## 3. Frozen baseline on `bird_large` / `ambrosia_large` (E0, ~1 h, first time only)

E0 is the *floor*: base 7B with the repository's own (Russian) prompts. Our branch replaces
`prompts.py`, so run E0 from a throw-away worktree of `main` — the branch is never modified.

Two things must be true inside that worktree, and only the first is free: the **harness** comes from `main`
(that is what the worktree is for), and the **recorder** is ours (`dev_tools/` exists only on this branch).
So copy our measurement code in; it derives its own `REPO_ROOT` from its location, which makes it drive the
worktree's `benchmarks/` and the worktree's `src/` tool — i.e. main's baseline, not ours:

```bash
git worktree add ../advtext2sql-main main
cp -r dev_tools ../advtext2sql-main/       # our recorder, not the harness
cp .env ../advtext2sql-main/.env          # gitignored secrets; the worktree does not carry them
cd ../advtext2sql-main
```

Serve the **base** model (no adapter) — see §6 for the two servers; then check both plans before spending
tokens (the recorder exports `DB_USER`/`DB_PASS` and `BENCHMARK_DB_URL` from the environment or `.env`):

```bash
python -m dev_tools.run_benchmark --check \
    --run-id e0-bird-large --query-file data/bird_large.json --answer-file data/bird_large.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"

python -m dev_tools.run_benchmark --check --benchmark ambrosia \
    --run-id e0-ambrosia-large --query-file data/ambrosia_large.json --answer-file data/ambrosia_large.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"
```

Expected: the `run plan:` block, with `git_commit: e812451…` (proof the worktree is main, not the branch)
and `--check: nothing was started, nothing was written`. Note that `--check` does not echo the benchmark
name — the plan does carry it, so verify the Ambrosia wiring explicitly with:

```bash
python -c "from pathlib import Path; from dev_tools.run_benchmark import plan_run; \
p = plan_run(run_id='check', query_file=Path('data/ambrosia_large.json'), \
answer_file=Path('data/ambrosia_large.json'), db_url='localhost:5444', \
tool_class='adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation.Text2SQLGenerator', \
llm_base_url='http://127.0.0.1:8000/v1', llm_model='m', runs_dir=Path('../data_cache/runs'), \
benchmark='ambrosia'); print('benchmark:', p['benchmark'])"
# expected: benchmark: ambrosia   (it is also stored as run.json's "benchmark" key by a real run)
```

Then the two real runs (both spend tokens — the endpoint must be approved first):

```bash
python -m dev_tools.run_benchmark --allow-llm-spend \
    --run-id e0-bird-large --query-file data/bird_large.json --answer-file data/bird_large.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"

python -m dev_tools.run_benchmark --allow-llm-spend --benchmark ambrosia \
    --run-id e0-ambrosia-large --query-file data/ambrosia_large.json --answer-file data/ambrosia_large.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"
```

Freeze the predictions so the paired test never moves again. From inside the worktree, `../data_cache` and
`../repo` are the right relative paths (`data_cache` is shared by both trees, `repo` is the branch):

```bash
mkdir -p ../repo/eval
cp ../data_cache/runs/e0-bird-large/query_results.json      ../repo/eval/baseline_bird_large.json
cp ../data_cache/runs/e0-ambrosia-large/query_results.json  ../repo/eval/baseline_ambrosia_large.json
cd ../repo && git worktree remove --force ../advtext2sql-main
```

`--force` is required: the copied `dev_tools/` is untracked in the worktree, and a plain
`git worktree remove` refuses to delete a tree with untracked files. `eval/` is gitignored (see
CHANGELOG "Repo hygiene"), so the frozen baselines never dirty the branch.

**Expected:** two `query_results.json` files plus the two frozen copies; the captain's live measurement of
the shipped baseline tool on `bird_large` is **42.32 % EX/241** (`docs/VERIFICATION.md` §7) — within a
couple of points of that is normal, because generation is not deterministic across endpoints.

**Fallback:** if E0 cannot be run today, use the frozen team artifact, score it once (§8, `--mode local`),
and treat it as the baseline — but label it as *frozen artifact*, not a re-run. At n = 22 a paired test
needs ≥ 6 discordant wins to be believable; at n = 241 a 2-point difference is noise.

---

## 4. Data build — 684 rows (1 min offline, or 15 min with a tokenizer)

```bash
python training/build_data.py \
    --train data/train_queries.json --ambrosia data/ambrosia_train.json \
    --schemas ../artifacts/db_schemas_bird.json \
    --out training/out-real
```

(`--schemas ../data_cache/db_schemas_pg.json` is the freshly dumped file from §2; the captain's validated
`artifacts/db_schemas_bird.json` is the same content and is what the measured build used. Use
`--no-tokenizer` when no tokenizer is reachable — the header line then says so and the percentiles are a
characters/3.6 estimate.)

Expected `COUNT TABLE`, printed verbatim by the build (byte-identical in the T1 dataset, which recorded
`schema_basis=file`, `token_basis=chars/3.6 estimate (tokenizer disabled)` in `stats.json`):

```
=================== COUNT TABLE ===================
schema_basis=file  seed=42  valid_frac=0.15  token_basis=chars/3.6 estimate (tokenizer disabled)
dropped=14  mutated=1  usable=342  train/valid=290/52
ambrosia built=438 (394/44)  combined=684
---------------------- detail ---------------------
sqlglot                      30.23.0
BIRD raw                     357
  with backticks             16
  with sqlite-only fns       25
  unparseable after rewrite  14
  mutated (semantics changed)   1   -> parseable 343, usable 342
  usable                     342
  duplicate questions        0
  duplicate normalized SQL   0
  train/valid                290/52
Ambrosia rows                480 (ambiguous 146, ok 334)
  negatives kept             292
  built                      438 (skipped 0)
  train/valid                394/44
combined training rows       684
sequence length chars/3.6 estimate (tokenizer disabled)
  p50/p95/p99/max            275/956/993/1014
leakage                      clean (bird qids 342 vs dev 241, ambrosia dbs 103 vs dev, overlaps 0)
===================================================
```

Artifacts: `sft_all_train.jsonl` (684), `sft_bird_train.jsonl`, `sft_bird_valid.jsonl` (52),
`sft_ambrosia_train.jsonl`, `sft_ambrosia_valid.jsonl`, `dropped.json` (15 = 14 unparseable + 1 mutated),
`stats.json`.

A one-second offline regression that needs no schemas at all:

```bash
python training/build_data.py --self-test --no-tokenizer     # expected: same counts, synth schemas
```

**Fallback:** missing schemas ⇒ the build **refuses** and lists the missing `db_id`s; fix §2. For a code
smoke only, `--allow-missing-schemas` (labelled `file+stub`) or `--self-test` (`synth-from-gold`); both
are refused by `train_qlora.py` unless `--allow-smoke-data` — do not train on them.

---

## 5. QLoRA training (~30 min GPU)

Environment once (`training/requirements-train.txt`, 85 pins; `RUN_NOTES.md` §0 has the full trap list):

```powershell
py -3.12 -m venv training\.venv-train
python -m pip install --target training\.venv-train\Lib\site-packages pysocks   # SOCKS proxy bootstrap
training\.venv-train\Scripts\python -m pip install -r training\requirements-train.txt
```

Pre-flight, no GPU, no torch import:

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py --plan --data training/out-real --out runs/e3-r16
```

Expected (training venv): `[sqlglot] installed=30.23.0 pin=30.23.0 match=True`,
`[trl] installed=0.18.2 required=>=0.16,<0.21 decision=ok`, `[data] … rows=train=684 valid=52`,
`[gpu] not probed: --plan never imports torch`, `[plan] TRL decision=ok`. With the repo's own
interpreter (no `trl`) the same command prints `decision=blocked` and still exits 0 — that is the
offline-safe path the tests use.

The run (7B, measured **1758.1 s = 29 m 18 s**, 125 of 129 steps, peak **11.01 GiB** allocated of
15.99 GiB):

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py `
  --model "D:\AI\models\qwen2.5-coder-7b-instruct" --model-id Qwen/Qwen2.5-Coder-7B-Instruct `
  --data training/out-real --out runs/e3-r16 `
  --epochs 3 --max-seq-len 3072 --per-device-batch 1 --grad-accum 16 --lr 1.5e-4 `
  --r 16 --alpha 32 --target-modules all-linear --seed 42 `
  --eval-steps 25 --logging-steps 5 --report-to tensorboard --sample-index 0
```

Expected: `trainable=40,370,176 (0.919 %)`, train loss `0.5268 → 0.0405`, eval losses
`0.2110 / 0.1757 / 0.1846 / 0.1801 / 0.1845`, early stopping at step 125, `load_best_model_at_end` keeps
the **step-50** weights (`eval_loss 0.1757`). Artifacts: `runs/e3-r16/adapter` (161.5 MB),
`provenance.json`, `train_summary.json`, `sample_generation.json`, `runs/e3-r16.log`.

**Fallbacks:** OOM ⇒ `--max-seq-len 2048 --per-device-batch 1 --grad-accum 32`; `SFTConfig` TypeError ⇒
the TRL pin is wrong (`trl==0.18.2`); `NameError: name 'nn' is not defined` in peft ⇒ `transformers<5`
(5.x hard-requires torch ≥ 2.6); `accelerator.unwrap_model(..., keep_torch_compile=False)` TypeError ⇒
`accelerate>=1.10`; bitsandbytes/Windows failures ⇒ run the same command under WSL2. Downloads:
`HF_HUB_DISABLE_XET=1` (the Xet CDN is unreachable through the SOCKS proxy) and
`snapshot_download(..., local_dir=...)` (symlinks fail with `WinError 1314`); `NO_PROXY=*` for httpx.
Do not train a second job on the same GPU: the run sits at 11.0 GiB.

---

## 6. Serving the model for the harness (5 min)

The harness only needs an OpenAI-compatible endpoint selected by three variables:

```bash
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_API_KEY=EMPTY
export LLM_MODEL_NAME=sql-lora            # E1: the base model's served name

# Ambrosia runs (S1, S4) must opt into the strict ambiguity gate, otherwise the tool never answers
# "ambiguous" (birdsafe default) and the run measures the 70.83 % floor instead of the gate (ADR-003).
export TEXT2SQL_AMBIGUITY_MODE=strict     # BIRD runs (S2, S3) leave it unset (birdsafe default)
```

PowerShell equivalents:

```powershell
$env:LLM_BASE_URL="http://127.0.0.1:8000/v1"; $env:LLM_API_KEY="EMPTY"; $env:LLM_MODEL_NAME="sql-lora"
$env:TEXT2SQL_AMBIGUITY_MODE="strict"     # S1/S4 only; Remove-Item Env:TEXT2SQL_AMBIGUITY_MODE for BIRD
```

The tool reads the variable in `Text2SQLGenerator.__init__` (a `Text2SQLConfig(ambiguous_mode=...)`
argument, if ever passed, wins over it), so it must be set in the shell that launches the recorder.

**Preferred — vLLM (Linux/WSL2 only):**

```bash
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --served-model-name qwen25coder7b --enable-lora \
  --lora-modules sql-lora=/mnt/d/Work/TalentHub/research/advtext2sql/repo/runs/e3-r16/adapter \
  --max-lora-rank 32 --max-model-len 4096 --dtype bfloat16 --gpu-memory-utilization 0.85 --port 8000
curl -s localhost:8000/v1/models | python -m json.tool     # expected: qwen25coder7b and sql-lora
```

**This workstation — `dev_tools/serve_openai.py`** (no Docker, no WSL2 needed; same contract, no
batching/streaming):

```bash
python -m pip install -r dev_tools/requirements-serve.txt         # once
python -m dev_tools.serve_openai --model "D:\AI\models\qwen2.5-coder-7b-instruct" \
    --adapter runs/e3-r16/adapter --served-name sql-lora --port 8000 --load-in-4bit
curl -s http://127.0.0.1:8000/v1/models      # expected: {"object":"list","data":[{"id":"sql-lora",…}]}
curl -s http://127.0.0.1:8000/health         # expected: 200
```

Expected: one served name; `LLM_MODEL_NAME` must equal it exactly (the harness sends it in every
request; `--relax-model-name` accepts any). Without `--adapter` the same command serves E1's base model.

**Fallbacks (in order):** vLLM → `serve_openai` → in-process generation inside `query()` with
`transformers`+`peft` (allowed by the task contract, slower per item). `--fake` serves a deterministic
canned answer for wiring tests only, never for a number.

---

## 7. Run the real harness and record it (E1 then E3; 20–60 min per run)

Always plan first — it validates inputs, prints the endpoint, tool class and commit, and spends nothing:

```bash
python -m dev_tools.run_benchmark --check \
    --run-id e3-bird-small --query-file data/bird_small.json --answer-file data/bird_small.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"
```

Expected:

```
run plan:
  run_id: e3-bird-small
  run_dir: …/data_cache/runs/e3-bird-small
  query_file: data/bird_small.json
  …
  git_commit: <sha>
--check: nothing was started, nothing was written
```

Then the real run (spends tokens; the endpoint must be approved first):

```bash
python -m dev_tools.run_benchmark --allow-llm-spend \
    --run-id e3-bird-small --query-file data/bird_small.json --answer-file data/bird_small.json \
    --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"
```

Expected: `run e3-bird-small finished: overall_accuracy=<x> total=22`, then
`artifacts in …/data_cache/runs/e3-bird-small: query_results.json, db_schemas.json, run.json`.
`db_schemas.json` is what the tool actually saw — diff it against §2's dump to catch a schema regression.

Order of runs, cheapest first (**S1–S4**). Every row is copy-pasteable; the Ambrosia rows differ in two
ways — `--benchmark ambrosia` (the harness class) and `TEXT2SQL_AMBIGUITY_MODE=strict` (ADR-003):

| id | command | n | what it measures |
|---|---|---|---|
| S1 | `TEXT2SQL_AMBIGUITY_MODE=strict python -m dev_tools.run_benchmark --allow-llm-spend --benchmark ambrosia --run-id s1-ambrosia-small --query-file data/ambrosia_small.json --answer-file data/ambrosia_small.json --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"` | 24 | the ambiguity contract, scored as detection (no SQL executed) |
| S2 | `python -m dev_tools.run_benchmark --allow-llm-spend --run-id s2-bird-small --query-file data/bird_small.json --answer-file data/bird_small.json --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"` | 22 | fast BIRD signal, `EX/22` (`EX/20` scoreable) |
| S3 | `python -m dev_tools.run_benchmark --allow-llm-spend --run-id s3-bird-large --query-file data/bird_large.json --answer-file data/bird_large.json --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"` | 241 | headline `EX/241` (`EX/211` offline-parse basis, live `EX/209`) |
| S4 | `TEXT2SQL_AMBIGUITY_MODE=strict python -m dev_tools.run_benchmark --allow-llm-spend --benchmark ambrosia --run-id s4-ambrosia-large --query-file data/ambrosia_large.json --answer-file data/ambrosia_large.json --db-url "$BENCHMARK_DB_URL" --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL_NAME"` | 240 | Ambrosia detection accuracy vs the **70.83 %** floor |

PowerShell: prefix the same commands with `$env:TEXT2SQL_AMBIGUITY_MODE="strict";` and write the long
flags on one line. Each run must use its own `--run-id`, or `run_benchmark` refuses to overwrite it.

**Fallback:** if `run_benchmark` refuses (`DB_USER/DB_PASS not set`, non-empty run dir, missing
`$LLM_API_KEY`), fix the cause — do not run the entrypoint directly from `$REPO`: it writes
`query_results.json` into the CWD and two runs silently overwrite each other (R1-D22). If you must use the
entrypoint, run it from a fresh directory and copy `query_results.json` out immediately.

---

## 8. Score and test the difference (5 min)

```bash
export DB_USER=... DB_PASS=...            # the evaluator executes on the same server
python -m dev_tools.fixed_eval \
  --predictions ../data_cache/runs/e3-bird-large/query_results.json \
  --answers data/bird_large.json \
  --out eval/report_e3_bird_large.json \
  --mode fixed \
  --baseline eval/baseline_bird_large.json
```

Expected keys in the JSON: `ex_all` (EX/241 + Wilson 95 % CI), `ex_gold_executable`, `n_gold_unexecutable`,
`gold_unexecutable_ids` / `gold_unexecutable_reasons`, `n_pred_unexecutable`, `n_missing_predictions`,
`per_difficulty`, `per_database`, `soft_f1_mean`, `soft_f1_mean_all`, and
`paired {n_paired, b, c, discordant, mcnemar_exact_p, delta_ex, bootstrap95, n_boot, seed}`.

Two denominators, do not mix them up: `n_gold_unexecutable` counts gold that fails **to parse or to
execute**. On a live server it is **32** (30 unparseable + ids 4 and 35, which parse but die with
`UndefinedColumn` because the shipped rewrite renamed the identifier), so `ex_gold_executable` divides by
**209**, not 211. `EX/211` is the offline parse-only basis (`241 − 30`); `EX/209` is the live-executable
basis; the honest maximum is 208/241 because id 572 executes against a mutated literal (ADR-005).

The report to quote (fill in the T2 numbers when they exist):

```
BIRD  : EX/241 = a.a % (95 % CI …)   EX/211 = b.b %   EX/209 = c.c %   honest max 208/241 = 86.3 %
Ambrosia: accuracy = d.d % (floor 70.83 %)   gold_ambiguous = 70/240
Paired vs E0: b = …, c = …, exact McNemar p = …, ΔEX 95 % CI [..]
```

Paired interpretation: `b` = items the adapter fixed, `c` = items it broke; at n = 22 you need ≥ 6
discordant wins before saying anything, at n = 241 a 2-point `ΔEX` is inside the noise band. Report
`--mode local` only for comparability with the shipped evaluator (it reproduces the 30 dead gold queries);
`--mode fixed` is the honest headline; `--mode normalized` removes the dialect penalty.

**Fallback:** predictions that fail to execute are listed in `n_pred_unexecutable`; an empty
`n_missing_predictions` inside a report is a hard requirement — a missing prediction is scored 0 and
silently flattens the comparison.

### 8.0 The report's own scoring pass (`dev_tools/score_runs.py`)

`score_runs.py` is what the T2 measurement run uses: it consumes `fixed_eval` ledgers and adds the paired
comparison against the team's frozen baseline (`../artifacts/pred_scores.json`, **42.32 % = 102/241**,
per-question scores only, no SQL).

```bash
python -m dev_tools.score_runs score --kind bird \
    --predictions runs/t2/e3-bird-large/query_results.json --answers data/bird_large.json \
    --out runs/t2/scores/e3-bird-large.json --db-url localhost:5444

python -m dev_tools.score_runs score --kind ambrosia \
    --predictions runs/t2/e3-ambrosia-large/query_results.json --answers data/ambrosia_large.json \
    --out runs/t2/scores/e3-ambrosia-large.json

python -m dev_tools.score_runs compare \
    --current runs/t2/scores/e3-bird-large.json --reference runs/t2/scores/e1-bird-large.json \
    --frozen ../artifacts/pred_scores.json --out runs/t2/scores/paired-bird-e3-vs-e1.json
```

Expected: one JSON per recipe; the Ambrosia file reports the detection counts next to the constant floor,
never as "benchmark accuracy". `--db-url localhost:5444` works while the tunnel is up; credentials come
from `repo/.env` (`DB_CLIENT_HOST`/`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`).

### 8.1 Offline sanity at any time (no DB)

```bash
python -m dev_tools.fixed_eval --answers data/bird_large.json --out /tmp/gold_damage.json --gold-damage
# expected tail: "gold queries silently mutated … : 3  (ids: 4, 35, 572)"
python -m dev_tools.local_bench gold --answers data/bird_large.json --timeout 30 \
    --out ../data_cache/artifacts/gold_local_results.json     # expected: 240 ok, 1 timeout (701)
```

`local_bench` measures the **official SQLite protocol**, which is not the course grade — quote it as
"proxy, out of harness" or not at all (ADR-005).

---

## 9. Close-out checklist

```bash
python -m pytest tests -q                        # 138 passed
git status --porcelain                           # empty (eval/, runs/, data_cache/ are ignored or outside the repo)
git log --oneline main..HEAD                     # the branch's commits
git diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py
                                                 # expected: no output
```

Record in the report which of S1–S4 ran, the endpoint model name, the run ids, and the commit sha each
number belongs to (`run.json` already stores all four).

---

## 10. Fallback path when the tunnel is down

Everything except the live numbers still works, in this order: `pytest tests -q` (138) →
`build_data.py --self-test` (684 rows) → `train_qlora.py --plan` (no GPU) → `local_bench` (§8.1) →
`serve_openai --fake` + `run_benchmark --check` (wiring only). Numbers produced this way are offline
proxies: label them, never paste them into the graded table.
