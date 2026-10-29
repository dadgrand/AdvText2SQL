# Distillation path — one command when the teacher endpoint appears (R2-7)

Status: **implemented, dry-run tested, blocked on access** — nothing in this file was produced by
running a teacher. The repo never calls the endpoint: it verifies the answers the team's pipeline
produces. If the endpoint or the predictions never arrive, the fallback at the end of this file is the
honest substitute.

---

## 1. What already exists (no new code needed to use a teacher)

| piece | what it does |
|---|---|
| `training/build_data.py --teacher <file>` | replaces the BIRD **training** target with the teacher SQL, wherever the teacher's answer survives the filter |
| `training/build_data.py --teacher-results <file>` | injects the recorded execution results, so "kept" means *executed and equal to gold*, not merely "parses" |
| `training/teacher_filter.py` | the rule: keep iff the SQL parses/canonicalises as PostgreSQL **and** executes with a row set equal to the gold query's. Every rejection is counted by reason: `unparseable-postgres`, `canonicalise-failed`, `exec-error`, `exec-mismatch`, `gold-exec-error`, `empty`, `keep-exec-match`, `keep-parse-only` |
| `--teacher-mode strict` / `fallback` | `strict` = keep only teacher-verified rows (this is the distillation dataset); `fallback` = gold target where the teacher failed (keeps the corpus size, dilutes the distillation) |
| `--teacher-require-exec` | refuse to keep anything when no executor was supplied — the "verified or nothing" switch |
| `--teacher-endpoint URL` | **new**: records the endpoint, proves `$TEACHER_API_KEY` is set, writes `distill_plan.json`, and never opens a socket (`network_calls: 0` in the report). Without `--teacher` it stops with exit code **3** and writes no dataset |

The executor is deliberately injected (`execute(question_id, sql) -> rows`), so this module has no
database code and no HTTP code at all. A distillation run therefore cannot silently become a
"teacher simulation": there is nothing in the tree that could produce teacher answers.

## 2. The exact procedure

### Step 0 — access (see §4 for the precise ask)

```powershell
$env:TEACHER_API_KEY = '<key>'          # never written to a file or a commit
$env:TEACHER_URL     = 'https://<host>/v1'
```

### Step 1 — run the team's best pipeline over the 357 **training** questions

* input: `data/train_queries.json` — **357 questions, 8 databases**. This is the file the sealed
  round-1 build was made from.
* **never** `data/bird_large.json` / `bird_small.json` (the evaluation set). `build_data.py`
  re-asserts this on every build: `check_leakage` aborts if a dev question, a normalised dev question
  or an Ambrosia dev `db_id` reaches the training rows, and a leaking build leaves no artifact.
* model + features: **GPT-5.4-mini + `FEAT_2+3+4+8+12+17+18+19+27+29+30+32+33+35`** (the team's best
  pipeline, as named by R2-7).
* capture two files:
  1. **predictions** — `{question_id: sql}`; a JSON object, a JSON list of records
     (`question_id` + one of `prediction`/`predicted_sql`/`sql`/`SQL`/`query`) or JSONL are all
     accepted by `teacher_filter.load_predictions`;
  2. **results** — `{question_id: {"gold": <rows> | {"error": ...}, "teacher": <same>}}`, the shape of
     `training/fixtures/teacher_results.json`. Without this file the filter can only parse-check, and
     the report will say `mode: parse-only` (a reviewable smoke mode, never a silent downgrade).

### Step 2 — read the plan (no network, no dataset)

```powershell
python training/build_data.py --teacher-endpoint $env:TEACHER_URL --out training/out-distill
# writes training/out-distill/distill_plan.json and exits 3 while --teacher is missing
```

Check in `distill_plan.json`: `n_questions == 357`, `key_present: true`, `teacher_recipe` matches the
pipeline you are about to run, `ready: true` once predictions exist.

### Step 3 — the one command

```powershell
python training/build_data.py `
  --schemas db_schemas.json `
  --teacher-endpoint $env:TEACHER_URL `
  --teacher <predictions.json> `
  --teacher-results <results.json> `
  --teacher-mode strict `
  --out training/out-distill
```

`--schemas` is required for a real build (the prompt must carry the same schema text the harness
shows at inference). `strict` is the distillation experiment; use `fallback` only if you deliberately
want the round-1 corpus size with teacher targets where they exist.

### Step 4 — verify before training

```powershell
python -c "import json;r=json.load(open('training/out-distill/teacher_report.json',encoding='utf-8'));print(r['mode'],r['kept'],r['by_reason'],r['applied_to_train_rows'],r['endpoint'])"
python -c "import json;s=json.load(open('training/out-distill/stats.json',encoding='utf-8'));print(s['schema_basis'],s['bird_train'],s['combined_train'],s['teacher']['kept'])"
```

Expected: `mode: exec-verified`, `endpoint.network_calls: 0`, `applied_to_train_rows == kept`,
`schema_basis: file`, and every row of `sft_bird_train.jsonl` carrying `target_source: teacher`
(strict mode). The count table prints the teacher block next to the damage counts.

### Step 5 — train on it

```powershell
training\.venv-train\Scripts\python training\train_qlora.py --data training/out-distill --out runs/r2-distill
```

Same recipe as round 1 and the scale run (see `training/RUN_NOTES.md`); the provenance records the
data hash, so a distilled-set run can never be confused with a gold-target run.

## 3. Expected kept fraction — what 200/241 actually measures

The only measured keep rate this repo owns is the round-1 demo
(`data_cache/artifacts/distill_sft_report.json`, described in `docs/LOCAL_BENCH.md` §6):

| quantity | value |
|---|---|
| input questions | 241 (`data/bird_large.json`) |
| kept (executed **and** equal to gold) | **200** = 83.0 % |
| dropped: mismatch | 11 |
| dropped: prediction error (non-executable) | 19 |
| dropped: no prediction | 10 |
| dropped: gold unusable | 1 |
| harness | offline SQLite (official BIRD dialect), not the course PostgreSQL |

**What it means.** It measures the *filter*: with a teacher whose answers were deliberately damaged,
83 % survived and every failure was counted. **What it does not mean.** It is not an estimate of
GPT-5.4-mini's accuracy — that teacher has never been run from this repository, and the demo file was
built by damaging a known-good set. Projecting it onto the 357 training questions gives roughly
**296 kept rows** at demo quality; treat that as an order of magnitude, not a number, and replace it
with the measured `teacher_report.json` the moment the real run happens.

Note the demo used the dev file on purpose (gold answers were needed to *demonstrate* the filter). The
production distillation must use `data/train_queries.json`; anything else leaks the evaluation set.

## 4. The blocker, stated precisely

**What is needed — either one is enough:**

1. **Endpoint access**: the base URL of the team's GPT-5.4-mini endpoint and a key that can use it
   (`TEACHER_API_KEY`, or another variable named with `--teacher-key-env`). The URL alone is not
   enough and the key alone is not enough.
2. **A predictions file** for `data/train_queries.json` produced by the team's pipeline
   (+ its results file for execution verification). This needs **no endpoint and no network** on this
   machine: `--teacher-endpoint` can be dropped entirely.

**What it unlocks — capability, not format.** Our own data pipeline can only clean and re-target SQL
we already have; where the BIRD gold is damaged (14 unparseable + 1 mutated on the 357 train rows, and
84 mutated + 348 unparseable in the 6,601-row scale corpus) it has no way to recover the answer. A
stronger teacher provides answers to those questions that are *verified to reproduce the gold rows*,
which is a capability increase in the training target — plus the FEAT_2…35 feature block only the
team's pipeline implements.

**What we must not do.** Fabricate teacher output. The only teacher files in the tree are
`training/fixtures/teacher_predictions.json` + `teacher_results.json` (5 synthetic rows, explicitly
format examples) and the round-1 demo file, which is a *damaged* set built to test the filter. Neither
is a teacher.

**Fallback (free, weaker, honest): self-distillation.** Run the current adapter over
`data/train_queries.json`, capture its predictions, execute-filter them with the same
`teacher_filter`, and train on the survivors:

```powershell
# 1. predictions from our own adapter (same serving stack as §9 of docs/REPORT.md), 357 questions —
#    tens of minutes of GPU at the rate the 2×2 runs achieved for 241 questions
# 2. python training/build_data.py --schemas db_schemas.json --teacher <own_predictions.json> \
#        --teacher-results <own_results.json> --teacher-mode strict --out training/out-selfdistill
```

That set contains only answers the adapter already gets right: it sharpens format/consistency and
cannot add capability the adapter lacks, so it is weaker than a real teacher by construction. Label it
`self-distillation` in whatever document quotes the result — the provenance records the file it came
from, and a self-distilled run must never be reported as a teacher run.

## 5. Cost

| step | cost |
|---|---|
| teacher calls | 357 (one per train question), plus retries; price is endpoint-specific and unknown here |
| filter | CPU only, seconds |
| retrain | the scale-run recipe: **112 min measured** for 3 epochs / 750 steps on an A100 80 GB (`training/RUN_NOTES.md` §6), so 2 epochs ≈ 75 min |
| fallback self-distillation | the 357 predictions at the serving rate the 2×2 runs achieved (241 questions per run, tens of minutes) — under an hour of GPU — then the same retrain |

## 6. Verification performed today (no teacher involved)

```powershell
python -m pytest tests/test_data.py -q -k "endpoint or teacher"   # 9 tests
python training/build_data.py --teacher-endpoint https://teacher.example/v1 --out training/tmp-blocked
#   -> exit 3, distill_plan.json only; nothing that looks like a training set
python training/build_data.py --self-test --teacher-endpoint https://teacher.example/v1 `
    --teacher training/fixtures/teacher_predictions.json `
    --teacher-results training/fixtures/teacher_results.json --out training/tmp-distilled
#   -> exit 0, kept=2 exec-verified, teacher_report.json endpoint.network_calls = 0
```

The network-free guarantee is a test, not a promise: `tests/test_data.py` runs the second command with
`socket.socket`, `socket.create_connection` and `socket.getaddrinfo` replaced by raisers on
`PYTHONPATH`, and it passes.
