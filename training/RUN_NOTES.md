# RUN_NOTES — first real QLoRA adapters (T1)

All numbers below are **measured on this machine**, not estimated. Artifacts are untracked by design
(no commits in this task): `runs/smoke-1.5b/` and `runs/e3-r16/`, logs next to them.

Machine: Windows, NVIDIA RTX 4070 Ti SUPER 16 GB (reported 15.99 GiB), driver 610.88,
Python 3.12.10, `torch 2.5.1+cu121`.

---

## 0. Environment (the traps are the interesting part)

```powershell
py -3.12 -m venv training\.venv-train
# this workstation's Windows proxy is SOCKS (socks=127.0.0.1:10808): a fresh venv cannot reach
# PyPI until it can speak SOCKS, and it cannot install PySocks without network, so bootstrap it:
python -m pip install --target training\.venv-train\Lib\site-packages pysocks
training\.venv-train\Scripts\python -m pip install -r training\requirements-train.txt
```

`training/requirements-train.txt` is a full `pip freeze` (85 pins) plus the cu121 `--extra-index-url`;
`training/constraints-train.txt` is the small constraint file that forced the resolution below
(`transformers<5`, `torch==2.5.1`) while it was being found.
The three pins that are not optional, learned by failing:

| pin | why |
|---|---|
| `transformers==4.57.6` (`<5`) | transformers 5.x hard-requires `torch>=2.6`; on the cu121 build it prints `Disabling PyTorch because PyTorch >= 2.6 is required but found 2.5.1+cu121` and then dies with `NameError: name 'nn' is not defined` inside peft |
| `trl==0.18.2` | trl 0.16.1 has **no** `completion_only_loss`; 0.21+ renamed `SFTConfig` fields. 0.18.2 is inside `>=0.16,<0.21` and exposes it |
| `accelerate>=1.10` | transformers 4.57 calls `accelerator.unwrap_model(..., keep_torch_compile=False)`; accelerate 1.2 rejects the kwarg |

Two download traps on this machine (both hit, both worked around without touching system settings):

1. **Xet storage** — `cas-server.xethub.hf.co` is unreachable through the proxy:
   `ConnectionError: ...error sending request for url (https://cas-server.xethub.hf.co/...)`.
   Fix: `HF_HUB_DISABLE_XET=1`, which routes downloads through the normal CDN.
2. **Symlinks** — `huggingface_hub` cannot create symlinks on this account (`WinError 1314`), so the
   hub-cache layout fails even after the blobs download. Fix: `snapshot_download(..., local_dir=...)`
   which copies instead of symlinking. The models therefore live outside `HF_HOME`:

   ```
   D:\AI\models\qwen2.5-coder-1.5b-instruct   2.96 GB
   D:\AI\models\qwen2.5-coder-7b-instruct    14.20 GB (4 safetensors shards)
   ```
   The captain's report also notes the equivalent proxy fact for `httpx`: `OSError: Unknown scheme for
   proxy URL URL('socks4://127.0.0.1:10808')`, fixed with `NO_PROXY=*`/`no_proxy=*` (or
   `pip install "httpx[socks]"`). It did not block this run: `--model` points at a local directory and
   the run itself uses `HF_HUB_OFFLINE=1`.

All training commands were run with `training\.venv-train\Scripts\python.exe`. The repo's main
interpreter is untouched (it still has no `trl`/`peft`, so it can still run `--plan` and the offline
test suite, which is exactly what `tests/test_data.py` relies on).

### WDDM paging on 16 GB — the trap that cost the round-2 run three launches

Above roughly **15.5 GiB of dedicated VRAM, Windows (WDDM) spills GPU memory into system RAM** and the
training step collapses: on this card, with a 7B QLoRA at `--max-seq-len 3072`, dedicated usage sat at
**15,870 of 16,376 MiB**, `Get-Counter '\GPU Adapter Memory(*)\Shared Usage'` reported **4,285 MB
shared**, and the step time walked **33 s → 93 s** with the ETA inflating to 24 h. It is not merely
slow: at `--max-seq-len 2560` (15,860 MiB dedicated, 1,398 MB shared) the run **stalled outright** at
step 6 — 100 % GPU utilisation, 77 W power draw, 0.03 s of CPU in 25 s and no TensorBoard events for
12 minutes — which looks like a hang, not like slowness.

Detect it with:

```powershell
Get-Counter '\GPU Adapter Memory(*)\Shared Usage'   # MB; must be near zero during training
nvidia-smi --query-gpu=memory.used,power.draw --format=csv,noheader   # low power at 100 % util = paging
```

Fix it by keeping the peak below the threshold, not by waiting:

* set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (free, reversible, less fragmentation);
* size `--max-seq-len` to the **tokenizer-measured** max, not to the recipe's default. Round 1 fitted
  in 11.0 GiB because its run rows were ≤1,015 tokens; the round-2 scale-up has rows up to 2,460
  (`training/out-scale/token_lengths.json`, real Qwen tokenizer: p50 587 / p95 1,294 / p99 2,414 /
  max 2,460), and 151 rows above 2,048 were **dropped and counted**
  (`training/out-scale-t2048/filter_report.json`), never truncated.
* the result at `--max-seq-len 2048`: **14,957 MiB dedicated, 148 MB shared, 232 W, 18.5 s/step** —
  from 85 s/step to 18.5 s/step by reducing the ceiling 1.5×. Training at the edge of VRAM is not
  merely slow here; it is 4.6× slow or dead.


---

## 1. Pre-flight (`--plan`, no GPU allocation)

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py --plan --data training/out-real --out runs/e3-r16
# (the bare `python training/train_qlora.py --plan ...` also works and exits 0; with the repo's main
#  interpreter, which has no trl, it reports decision=blocked instead of ok — by design)
```

Observed (training venv): `[sqlglot] installed=30.23.0 pin=30.23.0 match=True`,
`[trl] installed=0.18.2 required=>=0.16,<0.21 decision=ok`,
`SFTConfig: length_field='max_length' eval_field='eval_strategy' completion_only_loss=yes`,
`[data] ... schema_basis=file rows=train=684 valid=52`,
`[gpu] not probed: --plan never imports torch`.
Run with the main interpreter instead, the same command prints `decision=blocked` (no trl there) and
still exits 0 — the planner is deliberately usable without the training stack.

`runs/e3-r16/provenance.json` after the real run records: `model.id = Qwen/Qwen2.5-Coder-7B-Instruct`
(+ the local snapshot path), `data.data_hash = 9d0f8e80…9e01`, per-file hashes
(`sft_all_train.jsonl` 684 rows `d67a926e…`, `sft_bird_valid.jsonl` 52 rows `499224ce…`),
`seeds {data:42, data_seed:42, train:42}`, `pins.sqlglot 30.23.0 match=true`,
`pins.trl 0.18.2 decision=ok`, and the git commit `220991b5…` of `feat/lora-track` (dirty, as expected:
the training artifacts are untracked). A later `--plan` into the same directory writes
`provenance.plan.json` and does not clobber the training record.

---

## 2. Smoke run — Qwen2.5-Coder-1.5B (1 epoch)

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py `
  --model "D:\AI\models\qwen2.5-coder-1.5b-instruct" `
  --model-id Qwen/Qwen2.5-Coder-1.5B-Instruct `
  --data training/out-real --out runs/smoke-1.5b `
  --epochs 1 --max-seq-len 2048 --per-device-batch 4 --grad-accum 4 `
  --eval-steps 20 --logging-steps 5 --report-to none --sample-index 0
```

| measured | value |
|---|---|
| wall clock | **295.6 s** (4m56s), 43/43 steps, 5.7–6.9 s/step |
| peak VRAM `max_memory_allocated` | **10.67 GiB** (reserved 29.95 GiB — WDDM lets the allocator reserve beyond the device; allocation is the honest number) |
| train loss | 0.5268 → 0.3002 (mean), last step 0.19 |
| eval loss (BIRD valid) | 0.2698 at epoch end |
| adapter | `runs/smoke-1.5b/adapter` — 73.9 MB, loads with `PeftModel.from_pretrained` (verified) |
| exit | 0 |

Sample (greedy, row 3 / `question_id` 64):
`SELECT COUNT(T1.cdscode) FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.cdscode = T2.CDSCode WHERE T1.MailingCity = 'Hickman' AND T2.CharterNumber = '00D4'`
— PostgreSQL syntax, no backticks, no `IIF`/`IFNULL` (the dialect contract took), identifiers
hallucinated (expected at 1 epoch / 1.5B).

---

## 3. Real run — Qwen2.5-Coder-7B-Instruct (3 epochs)

```powershell
training\.venv-train\Scripts\python.exe training\train_qlora.py `
  --model "D:\AI\models\qwen2.5-coder-7b-instruct" --model-id Qwen/Qwen2.5-Coder-7B-Instruct `
  --data training/out-real --out runs/e3-r16 `
  --epochs 3 --max-seq-len 3072 --per-device-batch 1 --grad-accum 16 --lr 1.5e-4 `
  --r 16 --alpha 32 --target-modules all-linear --seed 42 `
  --eval-steps 25 --logging-steps 5 --report-to tensorboard --sample-index 0
```

Resolved config (from `provenance.json`): `effective_batch=16`, `planned_steps=129`,
`max_seq_len=3072`, `completion_only_loss=true`, `4bit-nf4 double-quant, compute bf16`,
`lr_scheduler=cosine`, `warmup_ratio=0.03`, `early_stopping_patience=3`,
`metric_for_best_model=eval_loss`, `load_best_model_at_end=true`, `packing=false`,
`attn_implementation=sdpa`.

| measured | value |
|---|---|
| wall clock | **1758.1 s = 29m18s** (125 steps at ~11.3 s/step + 5 evals ≈ 21 s each) |
| peak VRAM `max_memory_allocated` | **11.01 GiB** of 15.99 GiB |
| peak VRAM `max_memory_reserved` | **12.15 GiB** — no OOM, no paging, no fallback needed |
| steps | 125 of 129 planned — **early stopping fired** (see below) |
| train loss (every 5 steps) | 0.5268 → 0.4314 → 0.2803 → 0.2170 → 0.1914 → … → **0.0405** |
| LR | 1.5e-4 cosine, 3 % warmup, 0.00015 → decayed per step (logged with each loss entry) |
| LoRA | `trainable=40,370,176` = 0.919 % of 4,393,342,464 (all-linear, r=16, alpha=32, dropout 0.05) |
| adapter | `runs/e3-r16/adapter` — 161.5 MB `adapter_model.safetensors` + tokenizer; loads with `PeftModel` (r=16, alpha=32, 7 target modules) and reproduces the same greedy output |
| exit | 0 |

### Validation loss and early stopping (the honest part)

| eval step | epoch | eval_loss |
|---|---|---|
| 25 | 0.58 | 0.2110 |
| **50** | **1.16** | **0.1757 ← best, this is the adapter that was saved** |
| 75 | 1.75 | 0.1846 |
| 100 | 2.33 | 0.1801 |
| 125 | 2.91 | 0.1845 |

`EarlyStoppingCallback(patience=3)` therefore stopped at step 125 (~2.91 epochs of 3) and
`load_best_model_at_end=True` saved the **step-50** weights, not the last ones. Both facts are
deliberate: eval_loss is the only offline model-selection signal we have (BIRD EX needs the DB
tunnel), and carrying 0.18–0.21-level noise into "3 full epochs" would be a claim the numbers do not
support. `checkpoint-50` and `checkpoint-125` are on disk; `save_total_limit=2` kept both because the
best one is protected.

### Sample generation (greedy, the PostgreSQL-vs-SQLite sanity check)

Row 3 of `sft_all_train.jsonl`, `question_id` 64 (`california_schools`):

```
gold    : SELECT COUNT(*) FROM schools WHERE CharterNum = '00D4' AND MailCity = 'Hickman'
adapter : SELECT COUNT(CDSCode) FROM schools WHERE MailingCity = 'Hickman' AND CharterNum = '00D4'
```

Eyeball verdict: **dialect contract learned, schema linkage not learned.** The output is
single-statement PostgreSQL with single-quoted literals, no backticks, no `IFNULL`/`IIF`, no trailing
semicolon, no fences — exactly the format the evaluator executes verbatim. It also invents identifiers
(`MailingCity`, `CDSCode`) that do not exist in the prompt's schema, i.e. it will fail at execution on
this question. That is consistent with the design's own framing: 684 rows over 8 BIRD schemas buys
behaviour/format alignment, not capability. The measurable questions (does this beat the prompt-only
baseline on Ambrosia/BIRD) need the DB tunnel and belong to the evaluation task.

Saved in `runs/e3-r16/sample_generation.json` with `sqlite_markers: []` and `looks_postgres: true`.

### No-OOM note

The 3072/1/16 configuration fit with 3 GiB of headroom (`peak allocated 11.01` vs `15.99`), so the
documented fallback (max_seq_len 2048, batch 1, grad-accum 32) was **not** used. `checkpoint-125`
still exists if a longer/looser run is ever wanted.

---

## 4. What is reusable next time

```powershell
# 1. environment (once)
python -m pip install --target training\.venv-train\Lib\site-packages pysocks
training\.venv-train\Scripts\python -m pip install -r training\requirements-train.txt

# 2. pre-flight (no GPU)
training\.venv-train\Scripts\python training\train_qlora.py --plan --data training/out-real --out runs/e3-r16

# 3. the run (≈30 min on this card)
training\.venv-train\Scripts\python training\train_qlora.py --model <snapshot> --model-id Qwen/Qwen2.5-Coder-7B-Instruct --data training/out-real --out runs/e3-r16 --epochs 3 --max-seq-len 3072 --per-device-batch 1 --grad-accum 16 --lr 1.5e-4 --r 16 --alpha 32 --target-modules all-linear --seed 42 --eval-steps 25
```

Logs: `runs/smoke-1.5b.log`, `runs/e3-r16.log` (loss/lr/step per logging interval, eval blocks,
`[measured]` line, `[sample]` block). Structured copies: `provenance.json`, `train_summary.json`,
`sample_generation.json` in each run directory. TensorBoard event file under `runs/e3-r16/runs/`.

Do not run a second job on this GPU while the 7B trains: the run sits at 11.0 GiB allocated and a
second model would OOM it.

---

## 5. Scale-up dataset (round 2, R2-4) — `training/out-scale`

Round 1 trained on 342 BIRD rows over 8 databases. `--bird-extra` adds the official BIRD train split
and splits **by database**, so the adapter sees schemas instead of memorising one.

```powershell
# 1. raw data (once, ~2.6 MB; the machine's SOCKS proxy breaks huggingface_hub without NO_PROXY)
$env:NO_PROXY='*'
python -c "from huggingface_hub import hf_hub_download; hf_hub_download('birdsql/bird23-train-filtered','data/train-00000-of-00001.jsonl',repo_type='dataset')"
# -> copy to research/advtext2sql/data_cache/bird_train/bird23_train_filtered.jsonl
#    sha256 2ee64aa593e1adc59be7d5b9ce2395372a8ee27fdd94a3fb4d2fe09f3cd34bc8, 2,654,294 bytes, 6601 rows

# 2. schemas for every db_id, from the LIVE PostgreSQL through the tunnel (~6 min for 77 dbs)
python -m dev_tools.dump_schemas <--db-id for each of the 77 ids> --skip-missing `
    --out ../data_cache/bird_train/db_schemas_scale.json

# 3. the build (~15 s, CPU only)
python training\build_data.py --bird-extra ..\data_cache\bird_train `
  --schemas ..\data_cache\bird_train\db_schemas_scale.json --exclude-missing-schema-dbs `
  --group-split-keep-train california_schools --group-split-keep-train financial `
  --group-split-keep-train toxicology --group-split-keep-train card_games `
  --group-split-keep-train codebase_community --group-split-keep-train superhero `
  --group-split-keep-train student_club --group-split-keep-train debit_card_specializing `
  --no-tokenizer --out training\out-scale
```

Measured (seed 42, `training/out-scale/count_table.txt`): 6,958 raw pairs (357 repo + 6,601 extra) ->
362 unparseable + 85 mutated-but-parseable + 2 duplicate questions + 77 duplicate normalised SQL ->
6,416 parseable -> 820 rows excluded (6 BIRD train databases exist live but are **empty shells**) ->
**5,612 usable over 71 databases**; group split **4,736 train rows / 61 dbs** and **876 valid rows /
10 dbs**, db_id overlap 0. Combined training file 5,130 rows (4,736 BIRD + 394 Ambrosia), sequence
estimate p50/p95/p99/max = 568/1173/1784/1848 (chars/3.6).

For R2-5: the smoke guard passes (`schema_basis=file-partial`, not a stub basis), and
`training/.venv-train/Scripts/python.exe training/train_qlora.py --plan --data training/out-scale`
resolves r=16, alpha=32, max_seq_len=3072, effective_batch=16, TRL ok. `max_seq_len=3072` leaves room
for the p99 1,784; do not drop to 1,024 for this dataset without re-measuring the truncation rate.
`--drop-evidence` builds the no-evidence arm of the adapter x evidence matrix from the same corpus.

---

## 6. The scale run (R2-5): three local attempts, then the A100 — what was actually measured

### 6.1 Local 16 GB attempts (all preserved, none delivered)

| # | start | config | outcome (measured) |
|---|---|---|---|
| 1 | 04:04 | `out-scale`, seq 3072, 3 ep | WDDM paging: 15,870/16,376 MiB dedicated, **4,285 MB shared**, step 33 → 93 s; stopped at step 17 (`runs/r2-scale/train-seq3072-e3-paging.log`) |
| 2 | 04:27 | `out-scale`, seq 2560, 2 ep | still 15,860 MiB dedicated / **1,398 MB shared**; hard stall at step 6 — 100 % GPU util, **77 W**, 0.03 s CPU in 25 s, no TensorBoard events for 12 min (`train-seq2560-e2-stalled.log`) |
| 3 | 04:44 | `out-scale-t2048`, seq 2048, 2 ep | **healthy**: 14,957 MiB dedicated, **148 MB shared**, 232 W, **18.6 s/step**; reached **step 495/624** in 2 h 54 m, `checkpoint-300` + `checkpoint-450` written, then **terminated externally at 07:40:15 (exit −1, no traceback)** — paging was fixed by then, so this was a kill, not a compute failure |

The 2048 ceiling required dropping, never truncating, the rows above it: **151 of 5,130 training rows
(2.9 %, all SQL, 2,048-2,460 tokens; nothing above 1,588 remained)** left with per-file and per-db
counts in `training/out-scale-t2048/filter_report.json`, and the derived directory keeps the build's
`stats.json` (with a `token_filter` block), `count_table.txt` and `group_split.json`, so it is still
self-describing.

### 6.2 The delivered run (A100 80 GB), measured

Same corpus, same recipe, one machine change — `training/out-scale-t2048`, data_hash
`eb1ace92d6f03a9795d44fdab1bd6e2cae80744574641d17be246049fbf75e9b` (identical to the local attempt 3),
4,979 train / 876 valid rows, `schema_basis=file-partial`. Recipe: r=16, alpha=32, `all-linear`,
4-bit NF4 double-quant + bf16 compute, gradient checkpointing, **effective batch 16**, lr 1.5e-4
cosine, warmup 0.03, seq 2048, seed 42, EarlyStopping patience 3, eval/save every 150 steps.

* **750 of 936 planned steps**, EarlyStopping at epoch **2.40** (best eval loss at step 300, then
  three worse evaluations);
* **wall clock 6,753.3 s = 112 m 33 s**; **peak VRAM 35.12 GiB allocated / 74.21 GiB reserved** on an
  A100 80 GB PCIe (the 4-bit weights are only ~4.4 GiB; the rest is the batch-8 micro-batch at 2,048
  tokens — this recipe cannot be reproduced on 16 GB with batch 8);
* train loss **0.3756 → 0.0357**;
* **eval loss per checkpoint**: 150 → 0.131293 · **300 → 0.126443 (best)** · 450 → 0.143776 ·
  600 → 0.141140 · 750 → 0.169168;
* LoRA trainable **40,370,176 of 4,393,342,464 (0.919 %)** — identical to the sealed round-1 adapter,
  so the comparison to round 1 stays honest on the point that matters;
* greedy sample (row 0, `extra:bird23_train_filtered:5510`): generated == gold exactly,
  `SELECT COUNT(name) FROM filed_for_bankrupcy`, `looks_postgres: true`, no SQLite markers.

Artifacts: `runs/a100-scale/` (`provenance.json`, `train_summary.json`, `sample_generation.json`,
`adapter/adapter_config.json`, `adapter/README.md`) and the weights
**`runs/a100-scale-adapter/adapter_model.safetensors`** (161,533,192 bytes,
sha256 `71abcde86a45a351…`) with its `adapter_config.json`. Load them with
`PeftModel.from_pretrained(base, "runs/a100-scale-adapter")` — the `runs/a100-scale/adapter/` directory
holds the config and README only.

### 6.3 Deviations, named

1. **Three epochs, not the two the resize decision set** — the local run died at 495/624 and the run
   was restarted from scratch on the rented A100, where three epochs fit (112 min) and EarlyStopping
   ended it at 2.40 anyway.
2. **A different machine** (A100 80 GB, `peak_vram 35.12 GiB`), so nothing here says this recipe fits
   in 16 GB at batch 8; local batch 1 × grad-accum 16 is what fits there.
3. **Micro-batch shape**: `per_device_batch 8 × grad_accum 2` instead of `1 × 16`. The effective batch
   is 16 in both, which is the parameter the recipe fixes; the micro-batch shape is what the hardware
   forced.
4. **`report_to: none`** on the A100 run (no TensorBoard events shipped), and **`git.commit: null`** —
   the remote checkout carried no git metadata, so the provenance has no branch commit. The data
   hashes and pins (`sqlglot 30.23.0`, `trl 0.18.2`) are recorded and match.
5. **No A100 `train.log` in this workspace** — the measured series above comes from
   `runs/a100-scale/provenance.json` / `train_summary.json`. And `runs/r2-scale/provenance.json` is the
   *plan-mode* provenance of the first local attempt (`mode: "plan"`, 3 epochs, seq 3072, no `measured`
   block): do not read hyper-parameters from it.



---

## 7. The self-distillation arm (R2-10, branch B) — what was run, and what it may not claim

**Branch A never existed.** No teacher prediction file for `data/train_queries.json` is anywhere in
the tree: the only teacher files are `training/fixtures/teacher_predictions.json` (5 labelled synthetic
rows) and `data_cache/artifacts/teacher_demo_predictions.json` (the round-1 *damaged dev* demo). So
this is the free fallback, and everything below is **self-distilled** — never "distilled from the team
pipeline". Its honest baseline is the round-1 adapter it came from (`runs/e3-r16/adapter`,
sha256_16 `ea50f537cdf8e5a1`), not a teacher.

### 7.1 Generation (GPU, ~31 min served)

* generator: the round-1 adapter served 4-bit with `--use-evidence` (the stock `BenchmarkBIRD`
  setting), `/health` recorded `adapter_sha256 ea50f537cdf8e5a1…`, 512-token cap;
* questions: the **357 train questions** in `data/train_queries.json`, never the dev files (0 id
  overlap with the 241 dev questions, checked before spending GPU);
* result: `data_cache/runs/r2-selfdistill-preds/query_results.json`, 357 predictions, 0 empty,
  0 with backticks, 0 with SQLite functions, 600 server requests, `max_completion_tokens` hit the 512
  cap at least once (some generations are truncated by the cap and are counted as drops below).

### 7.2 Execution verification (CPU + tunnel, `runs/r2-selfdistill/exec_summary.json`)

Gold = the canonical PostgreSQL target `build_data.py` compares against; teacher = the adapter's raw
prediction. All 357 questions executed on the live database through the tunnel:

| counter | value |
|---|---|
| questions | 357 |
| gold execution errors | 15 |
| prediction execution errors | 31 |
| both executed | 311 |
| identical row sets | 171 |

### 7.3 The COUNT TABLE of the distilled build (`training/out-selfdistill`)

```
[teacher] mode=exec-verified attempted=342 kept=180 dropped=162 not_attempted=0
          keep-exec-match     180
          exec-mismatch       131
          exec-error           31
[teacher] strict mode: dropped 126 train rows without a verified teacher target
[teacher] applied to 164 BIRD train rows (valid rows keep gold targets)
sft_bird_train.jsonl         164      sft_bird_valid.jsonl   52
sft_ambrosia_train.jsonl     394      sft_ambrosia_valid.jsonl 44
sft_all_train.jsonl          558      schema_basis=file   leakage=clean
```

`kept 180` vs `identical_row_sets 171` differ by 9 rows: the filter compares row sets with its own
tolerance (order/floats) and decides the replay side by normalised SQL equality, so a prediction that
normalises to the canonical gold is compared against the gold record. 16 of the 180 kept questions
fell into the *valid* split (valid keeps gold targets), which is why 164 reach the training file.

**Leakage audit.** `check_leakage` is clean per benchmark (`bird.id_overlap []`,
`ambrosia.db_id_overlap []`, 0 question-text overlap). A naive *global* id check shows 58 ids shared
between the training file and the BIRD dev files — that is the documented BIRD/Ambrosia id-space
collision, not leakage: those 58 rows are Ambrosia `task=ambiguity` rows with unrelated questions, and
the round-1 artifact has exactly the same pattern (58 ambiguity ids + 84 SQL ids colliding with the
*Ambrosia* dev ids). Ids are only unique inside a benchmark, which is why the guard is per benchmark.

### 7.4 Training (round-1 recipe, GPU 13.5 min)

r=16, alpha=32, `all-linear`, 4-bit NF4, bf16 compute, gradient checkpointing, effective batch 16,
lr 1.5e-4 cosine, seq 3072, seed 42, 3 epochs, EarlyStopping patience 3, eval every 10 steps
(105 planned steps total for 558 rows).

| measured | value |
|---|---|
| steps | **60 / 105**, EarlyStopping at epoch 1.72 |
| wall clock | **810.7 s = 13 m 30.7 s** |
| peak VRAM | **10.94 GiB allocated / 12.19 GiB reserved** (RTX 4070 Ti SUPER 15.99 GiB) |
| train loss | 0.6892 → 0.0541 |
| eval loss | 10→0.301742 · 20→0.251027 · **30→0.238608 (best)** · 40→0.250141 · 50→0.275472 · 60→0.285863 |
| best checkpoint | `runs/r2-selfdistill/checkpoint-30` |
| adapter | `runs/r2-selfdistill/adapter/adapter_model.safetensors`, 161,533,192 B, sha256_16 `1e1419f650144e1b` |
| LoRA trainable | 40,370,176 (0.919 %) — identical capacity to round 1 |
| data hash | `690082394235f47b…` over `training/out-selfdistill` (558/52 rows) |
| git | `feat/round2` @ `49a4b6dc…`, dirty (uncommitted work) |

Greedy sample (train row 0, qid 1333): `SELECT T1.city, T1.state FROM zip_code AS T1 INNER JOIN
member AS T2 ON CAST(T2.zip AS BIGINT) = T1.zip_code WHERE T2.position = 'President'` — PostgreSQL
form, no SQLite markers, semantically the same join as the gold (reversed table order plus an explicit
cast).

### 7.5 What this arm can and cannot claim

* **Can**: it is a real adapter trained under the same recipe on teacher-verified targets produced by
  our own best model — a controlled test of whether "train only on what you already answer correctly"
  changes behaviour.
* **Cannot**: it is not distillation from the team's stronger pipeline, and it cannot add capability
  the round-1 adapter lacked — every target in it is an answer that adapter already produced. Any
  later EX comparison must be labelled `self-distilled` and compared against `runs/e3-r16/adapter`.
* Handoff: the 241-question measurement belongs to the eval owner (`runs/r2-selfdistill/adapter` +
  `provenance.json`); no number in this section is an EX result.

---

## 8. The 14B capacity arm (R2-18) — one model change, everything else identical

Status note first, because it matters for provenance: **the run was already complete when the task
reached me** (`runs/a100-14b/provenance.json` is timestamped 2026-10-29T07:48:13Z, the task was
dispatched afterwards). I did not launch it and did not touch `runs/a100-scale` (its provenance is
still 04:11:18Z). What this section records is the **verification** of that run and the copy of its
artifacts into this workspace (`runs/a100-14b/`, weights in `runs/a100-14b-adapter/`, sha256
`5833a1cce065f186…`, 275,341,720 B — the remote sha256 is byte-identical).

Purpose: the project's question is how much of the team API pipeline's advantage a local model can
recover. The 7B+LoRA+evidence arm reached 38.59 % (indistinguishable from their 42.32 %). The 14B arm
holds the data, the recipe and the seed fixed and changes **only the model** (and, for memory, the
micro-batch), which isolates capacity from data and recipe.

### 8.1 Configuration (identical to the 7B scale run except model and micro-batch)

`Qwen/Qwen2.5-Coder-14B-Instruct` from `/root/models/qwen2.5-coder-14b`, 4-bit NF4 double-quant,
bf16 compute, gradient checkpointing, r=16, alpha=32, `all-linear`, lr 1.5e-4 cosine, seq 2048,
**micro-batch 4 × grad-accum 4 = effective batch 16**, 3 epochs, EarlyStopping patience 3, eval/save
every 150 steps, seed 42. Data: `training/out-scale-t2048`, data_hash `eb1ace92d6f03a9795d44f…`
(4,979 train / 876 valid) — the same corpus as the 7B arm.

### 8.2 Measured (from `runs/a100-14b/provenance.json` / `train_summary.json`)

| quantity | 7B (`runs/a100-scale`) | **14B (`runs/a100-14b`)** | ratio |
|---|---|---|---|
| steps | 750 / 936 (ES at epoch 2.404) | **750 / 936 (ES at epoch 2.405)** | — |
| wall clock | 6,753.3 s (112 m 33 s) | **12,841.8 s (214 m 02 s)** | 1.90× |
| peak VRAM allocated | 35.124 GiB | **30.991 GiB** | 0.88× (smaller micro-batch) |
| peak VRAM reserved | 74.211 GiB | **60.623 GiB** | — |
| train loss first → last | 0.3756 → 0.0357 | **0.4637 → 0.0332** | — |
| eval loss @150 / @300 | 0.131293 / **0.126443 (best)** | 0.114024 / **0.112492 (best)** | −11.0 % at the best checkpoint |
| eval loss @450 / @600 / @750 | 0.143776 / 0.141140 / 0.169168 | 0.122852 / 0.125280 / 0.159049 | — |
| best checkpoint | `checkpoint-300` | **`checkpoint-300`** | — |
| LoRA trainable | 40,370,176 (0.919 %) | **68,812,800 (0.836 %)** | — |
| adapter | 161,533,192 B, sha256_16 `71abcde8…` | **275,341,720 B, sha256 `5833a1cce065f186…`** | — |

Both runs stop at the same place (epoch 2.40, three consecutive worse evaluations after the step-300
minimum), which is a useful sanity signal: the schedule, not the model size, decided when to stop.

### 8.3 Greedy sample (row 0, `extra:bird23_train_filtered:5510`)

```
gold   : SELECT COUNT(name) FROM filed_for_bankrupcy
14B    : SELECT COUNT(name) FROM filed_for_bankrupcy      (identical; looks_postgres true, no markers)
```

### 8.4 What this does and does not establish

* **Does**: on identical data and recipe, the 14B reaches a lower validation loss (0.112492 vs
  0.126443) and trains at 1.90× the wall clock, with **lower** peak VRAM than the 7B because the
  micro-batch is 4 instead of 8. The one parameter changed for memory (micro-batch 8→4, with
  grad-accum 2→4 keeping the effective batch at 16) is the only deviation, and it is recorded here.
* **Does not**: eval loss is per-token cross-entropy on the same 876 held-out rows; it is not EX and
  it does not answer "does it beat the 7B on the benchmark". That answer needs the 241-question
  measurement on `bird_large` plus the OOD held-out set, which belongs to the eval owner. No EX
  number is quoted in this section.
* `git.commit` is `null` for this run (the A100 checkout carries no git metadata), the same gap the
  scale run has; the dataset hash and the pins are recorded, so the run is still identifiable.

---

## 9. The Qwen3.5-9B generation arm (R2-20) — **prepared and smoke-tested, not run**

**No Qwen3.5 number is claimed.** The arm was prepared end to end, smoke-tested twice on the A100, and
then closed by the captain's NO-GO on the measured arithmetic: at the achievable speed it cannot land
before the deadline, and an unmeasured adapter is worth less than an honest gap.

**Why it never started (both on the box, 2026-10-29):** `/root/chain_q35.sh` fired at 07:48:13Z and died
immediately — the `/root/venv` transformers does not know model type `qwen3_5`
(`ValueError: ... Transformers does not recognize this architecture`). The retry `/root/chain_q35b.sh`
then sat **stuck for 6.5 h in `do_wait`** and could never start: its break condition required
`runs/a100-14b/train.log`, a file the 14B run never wrote (its artifacts are `provenance.json`,
`train_summary.json`, `sample_generation.json`, checkpoints).

**What was proven before the NO-GO:** the isolated `/root/venv-q35` (transformers 5.21.0) **does** build
the architecture (`AutoModelForCausalLM.from_config` → `Qwen3_5ForCausalLM`), and the custom
completion-only trainer `/root/q35_train.py` runs: smoke of 8 rows gave `trainable=43,278,336 (0.782 %)`,
**VRAM peak 26,836 MiB**, one eval and a saved adapter. The tokenizer question was settled by
measurement rather than assumption — with the **Qwen3.5** tokenizer the 4,979 training rows are
p50 614 / p95 1,308 / p99 1,612 / **max 1,751**, so **0 rows truncate at 2,048, 2,560 or 3,072**
(`/root/q35_token_lengths.json`); `--max-seq-len 2560` was therefore a free choice, and it is recorded
as measured, not assumed.

**The measured blocker — speed.** One training step at micro-batch 4 × seq 2,560 took **28-33 s**, because
Qwen3.5's linear-attention / delta-rule layers fall back to reference PyTorch: `causal_conv1d` is absent
and no fused `flash-linear-attention` kernels are compiled. At 311 steps per epoch that is
**2.4-2.9 h per epoch** — 3 epochs ≈ 7.2-8.6 h, and even 1 epoch (~2.6 h) would only have *started* the
evaluation chain, past the deadline. Installing `flash-linear-attention` 0.6.0 (`fla-core`) made it
**7× worse** — 208 s per step with Triton 3.2.0 below the recommended 3.3.0 — so the install was reverted
and `/root/venv-q35` is back to the documented baseline. A future Qwen3 arm needs `causal-conv1d` plus a
Triton ≥ 3.3 stack before it can be costed seriously.

**Hand-off note for the docs:** `--no-thinking` (thinking-mode stripping, R2-19) exists in the
`/root/work/repo-t17` checkout of `dev_tools/serve_openai.py` and **not** in `/root/work/repo`. That
checkout is a scratch tree owned by the evaluation work, *not* a maintained serving path (correction from
its owner, 2026-10-29): a resumed Qwen3 arm must **sync `dev_tools/` from the branch HEAD** that carries
the flag and serve from that, instead of depending on somebody's scratch copy — otherwise the number is
meaningless and the provenance does not say which tree produced it.

**Chain note:** do not gate any future chained run on a file the previous run may never write.
`/root/chain_q35b.sh` waited on `runs/a100-14b/train.log`, and the 14B run writes `provenance.json`,
`train_summary.json`, `sample_generation.json` and checkpoints — never a `train.log` — so the chain sat
6 h 28 m in `do_wait` and the arm never started. Gate on the process (`pgrep -f <trainer>`) or on the
run's `provenance.json`, and give the wait a deadline.

**Artifacts left in place:** `/root/q35_run.sh` (correct launcher: venv-q35, seq 2560, micro 4 × accum 4,
`EPOCHS=` overridable) and `/root/q35_post.py` (the `provenance.json` + greedy `sample_generation.json`
that `q35_train.py` does not write); local copies in `runs/a100-q35/`. Nothing under `runs/a100-q35/` on
the box was produced by a real training run.
