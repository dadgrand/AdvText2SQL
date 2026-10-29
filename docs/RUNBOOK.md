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
git branch --show-current                       # expected: feat/round2 (sealed feat/lora-track tip f4d3d4a)
git status --porcelain                          # expected: empty
python -m pytest tests -q                       # expected: 241 passed, ~63 s (no DB, no network)
```

Secrets live in `$REPO/.env` (gitignored): `BENCHMARK_DB_URL`, `DB_USER`, `DB_PASS`, `DB_HOST`,
`DB_PORT`, `DB_CLIENT_HOST`, `SSH_HOST`, `SSH_PORT`, `SSH_USER`, `SSH_PASS`, `LOCAL_FORWARD_PORT`.
Export them into the shell before the DB steps — bash: `set -a; . ./.env; set +a`;
PowerShell: `Get-Content .env | Where-Object {$_ -match '=' -and $_ -notmatch '^#'} | ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$k" $v.Trim('"') }`.

Expected `python -m pytest` output (the last line is the contract):

```
241 passed in 63.19s
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

Serve the **base** model (no adapter) — see §6 for the two servers; then check both plans before any
tokens are spent. The recorder reads `os.environ` only — it does **not** parse `.env` — so export the
credentials first exactly as §0 shows (`DB_USER`, `DB_PASS` and `BENCHMARK_DB_URL` come from `$REPO/.env`):

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
python -m pytest tests -q                        # 241 passed
git status --porcelain                           # empty (eval/, runs/, data_cache/ are ignored or outside the repo)
git log --oneline main..HEAD                     # the branch's commits
git diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py
                                                 # expected: no output
```

Record in the report which of S1–S4 ran, the endpoint model name, the run ids, and the commit sha each
number belongs to (`run.json` already stores all four).

---

## 10. Fallback path when the tunnel is down

Everything except the live numbers still works, in this order: `pytest tests -q` (241) →
`build_data.py --self-test` (684 rows) → `train_qlora.py --plan` (no GPU) → `local_bench` (§8.1) →
`serve_openai --fake` + `run_benchmark --check` (wiring only). Numbers produced this way are offline
proxies: label them, never paste them into the graded table.

---

## 11. Round 2 additions (draft — t9 seals)

Everything below is the round-2 delta on `feat/round2` (sealed round-1 tip `f4d3d4a`). The numbers are
measured on this workstation; the sections that are still missing are named at the end. Full tables live
in `docs/REPORT.md` §9–§12; this section is the operator's view.

### 11.1 `run.json` schema v2 — the recorder that makes a run quotable

`dev_tools/serve_openai.py` now exposes the serving provenance on `/health` (`base_model`, `served_name`,
adapter path + `adapter_model`/`adapter_config` SHA-256, `load_in_4bit`, effective `max_new_tokens`,
`requests_served`, `max_completion_tokens_observed`, `cuda_max_memory_allocated_bytes`), and
`dev_tools/run_benchmark.py` copies it into every `run.json`, together with:

| field | what it pins |
|---|---|
| `use_evidence` | whether `BenchmarkBIRD.use_evidence` was on (the round-1 mix-up) |
| `max_tokens` / `max_tokens_source` | the cap that actually binds (a disagreeing `--max-new-tokens` is refused) |
| `tool_commit_sha` / `tool_commit_dirty` / `tool_commit_dirty_paths` | the tool revision, and whether the worktree was dirty |
| `tool_source_sha256` | the tool module's own hash, independent of git |
| `model_id` / `served_name` / `adapter_id` / `adapter_sha256` / `adapter_config_sha256` / `load_in_4bit` | what was loaded |
| `cuda_max_memory_allocated_mib` | per-process peak VRAM (server restarted before the run) |
| `requests_served` / `max_completion_tokens_observed` | "no prediction hit the cap" is checkable from the artifact |
| `server_before` / `server_after` | the raw `/health` payloads |

Proof run: `runs/t2/recorder-proof-bird-small*` (`bird_small`, 22 questions) — quoted in §10.3 of the
report. If `/health` is unreachable the run still works, but the provenance fields are `null` and the run
is not quotable.

### 11.2 `--bird-extra` and the group split by database

Scale-up data (`training/build_data.py`, measured in `training/RUN_NOTES.md` §5):

```powershell
python training\build_data.py --bird-extra ..\data_cache\bird_train `
  --schemas ..\data_cache\bird_train\db_schemas_scale.json --exclude-missing-schema-dbs `
  --group-split-keep-train california_schools --group-split-keep-train financial `
  --group-split-keep-train toxicology --group-split-keep-train card_games `
  --group-split-keep-train codebase_community --group-split-keep-train superhero `
  --group-split-keep-train student_club --group-split-keep-train debit_card_specializing `
  --no-tokenizer --out training\out-scale
```

Result: 6,958 raw pairs → **5,612 usable over 71 databases**; group split **4,736 train / 61 dbs** and
**876 valid / 10 dbs**, db_id overlap 0; combined training file 5,130 rows; sequence estimate p99 1,784.
Rules worth knowing: the extra corpus goes through the *same* cleaning code as the base file; items carry
file-derived ids; non-data files in the download directory are skipped by shape and named; and a database
whose schema is missing is either refused or excluded explicitly (`--exclude-missing-schema-dbs`) — never
silently synthesised.

### 11.3 `matrix.py` — assembling the feature × model table (CPU only)

```bash
python -m dev_tools.matrix                        # table + paired block vs adapter_ev
python -m dev_tools.matrix --json runs/t2/scores/matrix.json
python -m dev_tools.matrix --reference base_ev    # pair against another arm
python -m dev_tools.matrix --arms my_arms.json    # caller-supplied arms
```

It never starts a server, never touches the DB and never runs a blocked arm: rows without a ledger print
as `blocked` with the exact command that would produce them. Four measured arms + the frozen reference row
are in `docs/REPORT.md` §12.1; six blocked arms (four feature arms waiting for a team feature file + GPU
window, two API arms waiting for the teacher endpoint) in §12.2.

### 11.4 Ambrosia natural-class-balance arm — ready, ~40 min GPU

`training/AMBROSIA_NATURAL_RECIPE.md`. The dataset is built (`training/out-natural/`, ignored): 480 rows =
146 ambiguous + 334 natural negatives, train/valid 432/48, combined 722. The training command is in the
recipe; **cost is ~40 min GPU**, not the 15 min originally planned: 138 steps × 14.06 s/step = 32.3 min
plus 434 s (7.2 min) for the 240-question Ambrosia evaluation. Do not start it while another task owns the
card, and run t7's scale measurements first (captain's ordering). Evaluation: `TEXT2SQL_AMBIGUITY_MODE=strict`,
then compare against the 70.83 % never-ambiguous floor — never quote an Ambrosia number without that floor.

### 11.5 Windows/WDDM paging trap on a 16 GB card

Measured on this workstation, and it cost a whole night-scale attempt: above roughly 15.5 GiB of dedicated
VRAM, Windows/WDDM spills the working set into shared system memory instead of failing with OOM. With the
scale-up run at 15,870 of 16,376 MiB, `Get-Counter '\GPU Adapter Memory(*)\Shared Usage'` reported
**4,285 MB of shared GPU memory**, and the step time walked from **33 s to 93 s** (ETA inflated to 24 h).
Training at the VRAM edge here is not merely slow — it is **3× slow**, and it looks like a data or model
problem until the counter is read.

Detect:

```powershell
Get-Counter '\GPU Adapter Memory(*)\Shared Usage'   # must be near zero during training
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv
```

Fix, in order:

1. keep the VRAM peak below the paging threshold: for this dataset `--max-seq-len 2560` (the t6 tokenizer
   check measured max 2,460 / p99 2,414 tokens, so every row still fits and nothing is truncated; the
   build's chars/3.6 estimate — p99 1,784 / max 1,848, `training/RUN_NOTES.md` §5 — agrees), and
2. set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` in the environment before the restart.
3. If shared usage is still in the gigabytes at 2,560, drop to `--max-seq-len 2048` **with an explicit row
   filter** that drops and counts longer rows — do not truncate targets silently.

After the resize the retry settled around ~44–45 s/it by step 6 (`runs/r2-scale/train.log`; the first
steps are 33–38 s/it as the allocator warms up), versus the 33 → 93 s/it drift before it.

### 11.6 Still missing after this draft (do not read as results)

- **t7 — scale-up adapter measurement:** adapter (`runs/r2-scale/adapter` when t6 finishes) on
  `bird_large`, evidence on and off, paired against round 1's `runs/e3-r16` and the 2×2 cells. Placeholder
  in `research/advtext2sql/ROUND2_RESULTS.ru.md`.
- **t17 — procedure arms + the real CFG comparison (cut-off 14:00):** the retrieval few-shot bank and
  self-consistency are implemented, tested and **OFF by default** (`docs/PROCEDURES.md`), but not measured;
  the grammar arm itself is measured on the A100 (see §11.7).
- **t18 — OOD check** on 10 databases the model never saw; **t19 — 14B scale arm on the A100**;
  **t21 — Qwen3.5-9B arm** at the same data and recipe. None measured yet.
- **Optional natural-balance Ambrosia arm (§11.4):** 40 min GPU, only after t7, low expected value.

### 11.7 Constrained decoding (R2-15) — an optional arm, off by default

Measured on a rented A100 80 GB (vLLM 0.32.0 + xgrammar, AWQ 4-bit `Qwen2.5-Coder-7B-Instruct`), same tool
prompts, **tool repair disabled in both arms**, only the proxy's grammar field differs:

| arm | decoding | EX/241 | EX/209 | unexecutable | fences in raw completions |
|---|---|---|---|---|---|
| N | greedy, unconstrained | **39/241 = 16.18 %** | 39/209 = 18.66 % | **62** | **241/241** |
| G | xgrammar, SELECT-only GBNF | **44/241 = 18.26 %** | 44/209 = 21.05 % | **57** | **0/241** |

Paired (same 241 ids): **b = 7, c = 2, exact p = 0.1797, +2.07 pp, 95 % CI [−0.41, +4.56]** — inconclusive
at n = 241. The deliverable is the **contract guarantee** (fenced output 241/241 → 0/241, grammar accepts
241/241 completions), not the EX delta: **55 of arm N's 62** unexecutable predictions are
`UndefinedColumn`/`UndefinedTable`/`UndefinedFunction` (schema grounding, not syntax; only 2 are syntax
errors), so a grammar cannot name the right column. Cost: ×1.43 wall clock / ×1.92 per token. Ship as an
optional arm, **off by default**.

Run:

```bash
# vLLM on :8000, then the proxy on :8100 (grammar on; use --no-grammar for arm N)
python -m dev_tools.guided_proxy --port 8100 --upstream http://127.0.0.1:8000 \
    --served-name qwen2.5-coder-7b-awq-guided --base-model Qwen/Qwen2.5-Coder-7B-Instruct-AWQ \
    --max-new-tokens 512 --quantization awq --load-in-4bit --log data_cache/runs/<run-id>/llm_requests.jsonl
export LLM_BASE_URL=http://127.0.0.1:8100/v1 LLM_API_KEY=EMPTY LLM_MODEL_NAME=qwen2.5-coder-7b-awq-guided
python -m dev_tools.run_benchmark --predict-only --disable-sql-repair \
    --run-id r2-15-grammar-awq --query-file data/bird_large.json --answer-file data/bird_large.json \
    --db-url localhost:5444 --runs-dir runs/t2
# contract facts (independent of EX): grammar acceptance + the raw-text table
python -m dev_tools.check_grammar --proxy-log runs/t2/r2-15-grammar-awq/llm_requests.jsonl
python -m dev_tools.decoder_report --runs-dir runs/t2 --run-id r2-15-grammar-awq
```

`run.json` records `sql_repair: "disabled"`, `tool_wrapper: dev_tools.run_benchmark.no_repair_tool`,
`guided_decoding` (backend, grammar file/SHA-256, guided/unguided request counts), `device_memory` and
`latency`. Rules for quoting it: print **both** EX/241 and EX/209; never fold this number into the LoRA
cells (different serving stack — vLLM/AWQ vs transformers 4-bit); and never call +2.07 pp an improvement —
"inconclusive at n = 241, with the contract guarantee as the actual deliverable".

### 11.8 Procedure arms and thinking mode (R2-14, R2-19) — present, off by default

Both are sources plus docs, not results: retrieval few-shot (`src/.../retrieval.py`) and self-consistency
(`src/.../self_consistency.py`) are wired but **disabled by default**; `docs/PROCEDURES.md` has the
commands and the dry-run verdict. Thinking-mode handling is the explicit `--no-thinking` / `--thinking`
switch in `dev_tools/serve_openai.py` (`enable_thinking` recorded in `run.json`); it is a serving shim, not
a measured arm yet.

---

## What the branch measured

The first real measurement of the LoRA branch through this runbook's own path (T2) is in `docs/REPORT.md`.
Headline, so a reader of this runbook knows what the procedure produced before running it again:

* **BIRD `bird_large` (241):** base model **EX/241 = 21.16 %** (51/241, 95 % CI 16.5–26.8),
  **EX/209 = 24.40 %**; base + `runs/e3-r16/adapter` **EX/241 = 25.31 %** (61/241, CI 20.2–31.2),
  **EX/209 = 29.19 %**. Honest maximum 208/241 = 86.3 % (30 unparseable + ids 4, 35 `UndefinedColumn` +
  id 572 mutated literal). Wall clock 912 s and 1 250 s on a 4-bit 7B server.
* **The LoRA effect is +4.15 pp and inconclusive:** paired b = 26, c = 16, exact McNemar p = 0.164,
  95 % CI [−0.83, +9.54] pp. The pre-registered rule (b − c ≥ 6, p < 0.05, CI excludes 0) is not met, so
  §8's table must be reported as "inconclusive at n = 241", not as an improvement. The adapter does move
  invalid-SQL predictions 40 → 36 and challenging-tier EX 6.6 % → 21.3 %.
* **Against the team's frozen 42.32 % baseline** both runs are far below (adapter b = 20, c = 61,
  p = 5.7e-06, Δ −17.01 pp; base b = 10, c = 61, p = 4.6e-10, Δ −21.16 pp). That gap is
  configuration-dominated (tool revision, 4-bit serving, `use_evidence=False`, decoding), not an adapter
  regression — §8's paired block is the right instrument for it, and the frozen file is id-level only.
* **Ambrosia (`ambrosia_large`, strict):** base 143/240 = 59.58 % detection — already below the
  70.83 % never-ambiguous floor; the adapter 70/240 = 29.17 % because it answered "ambiguous" for all
  240 items (paired b = 53, c = 126, p = 4.9e-08, Δ −30.42 pp). Reported as ambiguity detection against
  constant baselines, never as accuracy.
* **Training:** `runs/e3-r16/adapter`, best validation loss 0.1757 at step 50, 29 m 18 s, 11.0 GiB peak of
  15.99 GiB (r=16, alpha=32, all-linear, 684 rows).

An evidence-enabled baseline (E1-fair) settles most of the gap to the frozen 42.32 % file: the same base
model with the stock entrypoint's `use_evidence=True` — the flag our own wrapper omitted — scores
**84/241 = 34.85 %** (EX/209 40.19 %), **+13.69 pp** over E1 (b = 39, c = 6, exact McNemar p = 5.4e-07,
10k bootstrap CI [+8.71, +19.09]), i.e. evidence explains **65 %** of the 21.16 pp gap. The rest does not
close: E1-fair is still **−7.47 pp** behind the frozen file (b = 21, c = 39, p = 0.0273, CI
[−13.69, −1.24]) and cannot be attributed from the artifacts (the frozen file has no SQL or config).
Decoding is ruled out (no prediction hits the 512-token cap). The clean adapter comparison is unchanged
(+4.15 pp, p = 0.164, inconclusive); the missing cell is **E3-fair** (adapter **with** evidence, ~15 min),
so no adapter claim should quote the frozen baseline until it exists (`docs/REPORT.md` §7.4). Practical
consequence for a re-run: set `use_evidence` explicitly and record `max_tokens`, `use_evidence`, the tool
commit and `torch.cuda.max_memory_allocated` in `run.json` — the wrapper records none of them today, and a
1 Hz `nvidia-smi` sample is how the ~8.2 GiB 4-bit serving footprint was measured.

