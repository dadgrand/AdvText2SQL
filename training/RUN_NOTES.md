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

