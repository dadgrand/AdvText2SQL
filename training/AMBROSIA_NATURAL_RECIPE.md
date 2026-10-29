# Ambrosia natural-class-balance arm — READY, BLOCKED ON GPU

**Status: ready, blocked.** The dataset is built and verified (`training/out-natural/`, CPU-only,
2026-10-29). The training run is **not started**: the single RTX 4070 Ti SUPER is occupied by the
night-scale run (t6, ETA ~14:30) and, by the captain's ordering, the scale-adapter measurement
(t7) goes first. Do not start this arm before t7's four runs are done.

**Run estimate (derived from measured hardware numbers, not guessed):** ~32 min training +
~8 min Ambrosia evaluation ≈ **40 min of GPU**, not the 15 minutes in the original plan — see
"Cost" below for the arithmetic.

---

## 1. What this arm is for, and why the prior is weak

Round 1's adapter answered `ambiguous` for all 240 Ambrosia items
(`runs/t2/scores/e3-ambrosia-large.json`: 70/240 = 29.17 %, exactly the always-ambiguous
constant) after training on a 2:1 negative sample. The natural-balance hypothesis says the
down-sampling caused that collapse. The measured class mix says the opposite direction:

| build | ambiguous rows | unambiguous rows | ambiguous share |
|---|---|---|---|
| round-1 `training/out-real` (`--ambrosia-neg-ratio 2.0`) | 146 | 292 kept (of 334 natural) | **33.3 %** |
| this arm `training/out-natural` (`--ambrosia-neg-ratio 0`) | 146 | **334** | **30.4 %** |

So the natural build has *more* negatives (69.6 % vs 66.7 %) and the positive share moves by
2.9 pp. A collapse to "always ambiguous" cannot be explained by too few negatives, and a 2.9 pp
mix change is unlikely to fix it. **This arm is still worth its 40 minutes** — it is a controlled
single-variable change with a measurable outcome, and it is the last cheap test of the head
before the contract question is decided — but its expected value is low and the decision below
does not depend on it. (The round-1 collapse is much more consistent with the head learning
"Ambrosia-style prompt → ambiguous" from the schema stub / domain shortcut than with class
balance; that is a hypothesis, not a measurement.)

## 2. Data build (already executed, CPU-only, ~15 s)

```powershell
python training\build_data.py --schemas ..\data_cache\db_schemas.json --no-tokenizer `
    --ambrosia-neg-ratio 0 --out training/out-natural
```

`--ambrosia-neg-ratio 0` means "keep every natural negative" (`build_data.py:1051`: the
sub-sample branch only runs when the ratio is > 0). Measured output, from
`training/out-natural/stats.json`:

| quantity | value |
|---|---|
| Ambrosia rows | 480 (146 ambiguous + 334 unambiguous, natural 30.42 % ambiguous) |
| built / skipped | 480 / 0 |
| train / valid | 432 / 48 (`valid_frac` 0.15, seed 42) |
| BIRD train / valid | 290 / 52 (unchanged from round 1) |
| **combined training rows** | **722** |
| `schema_basis` | `file` (real harness schemas, not synth — same as `out-real`) |
| prompt length, chars/3.6 estimate | p50 266 · p95 965 · p99 1002 · max 1024 (≤ `--max-seq-len 3072`) |
| leakage | clean: Ambrosia train dbs 104 vs dev, id/question/db overlaps 0 |
| file hashes (`sha256_16`) | `sft_all_train` 722 `343a313354a6a16b` · `sft_ambrosia_train` 432 `2547473c86ae7096` · `sft_ambrosia_valid` 48 `749b64e6576bffdc` · `sft_bird_train` 290 `54c1e41a88b25609` · `sft_bird_valid` 52 `67025f5d8664d018` |

The build is deterministic (seed 42) and `training/.gitignore` now ignores `out-natural/`, so the
dataset is an artifact, not a source change.

## 3. Training — the blocked step

Identical recipe to `runs/e3-r16` (r=16, alpha=32, all-linear, 3 epochs, lr 1.5e-4,
max-seq-len 3072, per-device-batch 1 × grad-accum 16, seed 42) so that **only the Ambrosia class
mix changes**:

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py `
  --model D:\AI\models\qwen2.5-coder-7b-instruct --model-id Qwen/Qwen2.5-Coder-7B-Instruct `
  --data training/out-natural --out runs/ambrosia-natural `
  --epochs 3 --max-seq-len 3072 --per-device-batch 1 --grad-accum 16 --lr 1.5e-4 `
  --r 16 --alpha 32 --target-modules all-linear --seed 42 --eval-steps 25
```

A dry run of the planner was executed (no GPU, no model load) and is recorded in
`runs/ambrosia-natural/provenance.json`:

```
steps_per_epoch 46 · total_steps 138 · train_rows 722 · valid_rows 52
data_hash b162f5c79bd808e5… · schema_basis=file
[sqlglot] 30.23.0 match=True · [trl] 0.18.2 decision=ok · SFTConfig completion_only_loss=yes
```

**Cost derivation.** The sealed round-1 run measured `runs/e3-r16/train_summary.json`:
`steps_run 125`, `wall_clock_s 1758.1` → **14.06 s/step** (including 5 evaluation passes).
138 steps × 14.06 s ≈ **1 940 s ≈ 32 min** (early stopping may cut it; e3 stopped at 125 of
129 planned steps). The "~15 minutes" in the plan is optimistic by ~2×.

## 4. Evaluation — what would count as a fix

```powershell
# serve the new adapter (4-bit, same server as every other run)
training\.venv-train\Scripts\python.exe -m dev_tools.serve_openai `
  --model D:\AI\models\qwen2.5-coder-7b-instruct --adapter runs/ambrosia-natural/adapter `
  --served-name qwen2.5-coder-7b+amb-natural --port 8000 --max-new-tokens 512 --load-in-4bit

# strict mode is mandatory: birdsafe never answers "ambiguous", which on Ambrosia IS the floor
. runs/t2/run_env.ps1
$env:LLM_MODEL_NAME = "qwen2.5-coder-7b+amb-natural"   # run_env.ps1 sets the base served name
$env:TEXT2SQL_AMBIGUITY_MODE = "strict"
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only --benchmark ambrosia `
    --run-id amb-natural-ambrosia-large --query-file data/ambrosia_large.json `
    --answer-file data/ambrosia_large.json --db-url localhost:5444 --runs-dir runs/t2

python -m dev_tools.score_runs score --kind ambrosia `
    --predictions runs/t2/amb-natural-ambrosia-large/query_results.json `
    --answers data/ambrosia_large.json --out runs/t2/scores/amb-natural-ambrosia-large.json
```

Cost: the round-1 adapter needed 434 s (7.2 min) for the same 240 questions, so ~8 min.

**Acceptance for the arm:** strict-mode detection accuracy **> 170/240 = 70.83 %** (i.e. ≥ 171
correct) AND precision on its ambiguous calls **> 50 %** — the second is what makes the first
possible (accuracy = (170 + TP − FP)/240, so beating the floor is exactly TP > FP). The round-1
adapter scored 70/240 = 29.17 % with precision 29.17 %; the base model scored 143/240 = 59.58 %
with precision 27.87 %. See `docs/REPORT.md` §11 for the full operating-point table and
`runs/t2/scores/ambrosia-calibration.json` for the numbers.

## 5. Residual risk of running it

* **Expected value is low** (§1): the class mix moves 2.9 pp in the safe direction. A negative
  result therefore does not prove the head is unfixable, only that class balance was not the
  cause — which is still worth knowing before the contract decision.
* **It cannot beat the floor by itself.** The floor is a *decision-rule* comparison, not a
  training-mix comparison: even a better classifier must clear 50 % precision, and the artifacts
  contain no way to calibrate a threshold (§11). If the arm returns 60 % accuracy again, nothing
  about Ambrosia is settled except that this hypothesis is dead.
* **GPU contention:** the 40 minutes must not overlap t7 or the t6 tail; 11.0 GiB peak measured
  for the round-1 7B QLoRA run leaves no room for a second job.
* **Prompt surface is unchanged** from round 1 (`schema_basis=file`, same chat template), so a
  difference in behaviour is attributable to the data only.
