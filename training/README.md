# training/ — LoRA track: data build, distillation, feature prompts, QLoRA runbook

This directory is the branch-native, tested version of the R3 research pipeline
(`research/advtext2sql/r3-scratch/build_lora_data.py` + `train_qlora.py`, which the captain executed
and fixed). It lives inside the course repo, writes only into its own output directories, and touches
nothing under `benchmarks/`, `data/`, `src/` or the root entry points.

| file | role |
|---|---|
| `build_data.py` | BIRD-train + Ambrosia-train -> SFT jsonl (`--self-test`, `--teacher`, `--teacher-endpoint`, `--feature-prompt`, `--bird-extra`) |
| `teacher_filter.py` | distillation filter: keep a teacher answer only if it parses **and** matches gold execution |
| `distill.md` | **the distillation procedure** (R2-7): the exact pipeline run, the one command, the expected keep rate, the access blocker and the self-distillation fallback |
| `train_qlora.py` | QLoRA SFT recipe with lazy imports, `--plan` dry run and `provenance.json` |
| `fixtures/` | canned offline teacher predictions + recorded results (format examples, used by the tests) |
| `../tests/test_data.py` | the offline acceptance suite for everything above |

Status of the code in this directory: **data build and planner are executed and tested offline;
the training run itself has not been executed on the authoring machine** (no `trl`/`peft` installed
and no GPU work was done). `--plan` is the honest pre-flight: it prints the fully resolved config,
the TRL compatibility decision and the provenance record without loading a model.

---

## 1. Environment

Two tiers, because the data build must stay runnable without a GPU stack.

**Tier A — data build only (what `--self-test` needs):**

```bash
pip install "sqlglot==30.23.0"
```

`sqlglot` is pinned hard. Every number in the COUNT TABLE (dropped=14, mutated=1, usable=342,
 the 241->211 gold ceiling,
the split, the Ambrosia head) was measured with 30.23.0, and the evaluator's transpile behaviour is a
function of the version (`benchmarks/evaluate_bird.py` transpiles gold at scoring time). A different
version aborts the build; `--allow-version-drift` continues but the counts stop being comparable.

**Tier B — training (needs CUDA). Reproduced, exact pins in `requirements-train.txt`:**

```bash
py -3.12 -m venv training/.venv-train
python -m pip install --target training/.venv-train/Lib/site-packages pysocks   # SOCKS proxy, Windows
training/.venv-train/Scripts/python -m pip install -r training/requirements-train.txt
training/.venv-train/Scripts/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

The pins that matter (all learned by failing — see `RUN_NOTES.md` §0): `torch==2.5.1+cu121`,
`transformers==4.57.6` (`<5`, because transformers 5 hard-requires torch>=2.6 and then breaks inside
peft), `trl==0.18.2` (`completion_only_loss` does not exist in 0.16.1; 0.21+ renamed SFTConfig
fields), `accelerate>=1.10` (transformers 4.57 passes `keep_torch_compile=` to `unwrap_model`).
`train_qlora.py` probes the installed `SFTConfig` and refuses to train on an incompatible build with
an actionable message instead of silently training on the full sequence.

Measured on this machine (RTX 4070 Ti SUPER, 16 GB): 7B / 3 epochs / 3072 tokens / batch 1 x accum 16
= **29m18s, 11.01 GiB peak allocated**, adapter 161 MB, early-stopped at step 125 with the step-50
weights kept. Full numbers, loss curve and one greedy sample: `RUN_NOTES.md`.

**Offline is a first-class mode.** Nothing in `--self-test`, `--plan`, or `tests/test_data.py`
downloads anything: the tokenizer is loaded with `local_files_only=True` and only when
`--tokenizer-model` is passed explicitly (`--self-test` forces it off), and the teacher executor is
injected rather than dialled. The tests run the build with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`
so a download attempt would fail loudly.

---

## 2. Quick start

### 2.1 The 1-second offline regression (`--self-test`)

```bash
cd repo
python training/build_data.py --self-test
```

Runs end-to-end on `repo/data` with no network and no tokenizer (~1 s, well under the 60 s budget),
and asserts the measured baseline (both damage classes, see §3). Expected tail:

```
=================== COUNT TABLE ===================
schema_basis=synth-from-gold  seed=42  valid_frac=0.15  token_basis=chars/3.6 estimate (tokenizer disabled)
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
  ...
combined training rows       684
...
[self-test] baseline reproduced: 14 unparseable + 1 mutated (question_id 2) -> 342 usable; the
reconciled damage classes agree with research/advtext2sql/reconcile_damage.py and the counts in
research/advtext2sql/captain-verified-numbers.md (which predates the mutation class, so its
usable=343 is the superseded number).
```

If any count moves, the build exits non-zero and prints the diff. That is intentional: every cost,
step and EX comparison downstream assumes these numbers. `--self-test` hard-asserts the two damage
counters (14 unparseable, 1 mutated) and the Ambrosia head; the per-split train/valid and combined
numbers are printed and checked for internal consistency, because which db the mutated item belongs
to moves them (it is `california_schools`, so 35 -> 34 items there, and the seeded per-db shuffle
shifts every later database).

Artifacts land in `training/self_test_out/` (gitignored):
`sft_bird_train.jsonl`, `sft_bird_valid.jsonl`, `sft_ambrosia_train.jsonl`,
`sft_ambrosia_valid.jsonl`, `sft_all_train.jsonl`, `stats.json`, `dropped.json`.

### 2.2 A real build (requires the harness schema dump)

The prompt must contain **the same schema text the harness shows at inference**. A harness build run
writes it (`benchmarks/base.py::_dump_db_schemas_json` -> `db_schemas.json`, also gitignored):

```bash
# once, with the DB tunnel up (needs the teacher credentials), e.g.
uv run --env-file .env bird_benchmark.py          # produces db_schemas.json in the CWD
mv db_schemas.json training/db_schemas.json

python training/build_data.py --schemas training/db_schemas.json --out training/out
```

If a `db_id` from the data is missing in the dump, the build **refuses** and lists the missing ids.
`--allow-missing-schemas` downgrades that to explicitly labelled `TABLE unknown  -- STUB` prompts
(`schema_basis="file+stub"`, a dry run you cannot accidentally train on — see §5).

Without `--schemas` at all the build also refuses; `--allow-synth-schemas` (or `--self-test`)
synthesises a schema per `db_id` from that db's own gold SQL. That is **smoke-only**: it leaks the
query's own columns into the prompt, so `schema_basis="synth-from-gold"` is recorded and
`train_qlora.py` refuses to train on it unless `--allow-smoke-data` is given.

### 2.3 Determinism

One `random.Random(seed)`, fixed stage order, `\n` newlines, no paths inside `stats.json`. Two runs
with the same seed produce byte-identical jsonl files; the test suite asserts this by hashing.

---

## 3. What the COUNT TABLE means

| stage | value | why it matters |
|---|---|---|
| BIRD raw | 357 | `repo/data/train_queries.json` (train split only, never the dev files) |
| destroyed by the repo's own rewrite | 14 | the evaluator's `sqlite_to_postgres` + naive `NULLIF` regex makes these unparseable; repairing them would teach damage, so they are dropped and written to `dropped.json` |
| mutated but still parseable | 1 | `question_id` 2: the regex fires **inside a quoted identifier** and rewrites `"Charter School (Y/N)"` to `"Charter School (Y / NULLIF(N,0))"`. The statement still parses, so the parse test alone keeps it — and the example would teach the model the regex's damage. Dropped too (`reason: mutated-but-parseable`), same rule as the 14 |
| parseable after rewrite | 343 | 357 - 14; the number that made 343 look usable |
| usable | 342 | 357 - 14 - 1, after dedupe (0 duplicate questions, 0 duplicate normalised SQL) |
| train/valid | 290/52 | split by `db_id` at 15 %, seed 42; the 3 longest-SQL groups per db are force-included in valid (template generalisation probe). The mutated id belongs to `california_schools`, so that db has 34 items instead of 35 and the seeded per-db shuffle shifts every later db — hence 290/52, not 291/52 |
| Ambrosia | 480 rows = 146 ambiguous + 334 ok | ambiguous -> `AMBIGUOUS: ...`, ok -> `OK`; negatives down-sampled 2:1 (292) |
| ambrosia built | 438 (394/44) | 146 + 292, then 10 % valid hold-out |
| combined | 684 | 290 + 394, shuffled so both tasks alternate inside a batch |

**Why the mutated target is dropped, not repaired.** The evaluator applies the same regex to *gold*
at scoring time, so a training target that carried the rewritten identifier would teach the model to
emit `"Charter School (Y / NULLIF(N,0))"` — a name PostgreSQL accepts syntactically but no such column
exists, so the query errors at execution. Fixing it by hand is also wrong: the repaired text is our
invention, not the benchmark's semantics, and it would quietly disagree with the gold the score is
computed against. Dropping one poisoned example out of 357 costs 0.3 % of the data; keeping it teaches
the exact failure the rest of this pipeline exists to avoid.
The detection rule is principled rather than textual: the rewrites are compared **after
canonicalisation**, so formatting noise and the intended `x / col -> x / NULLIF(col, 0)` fix do not
register as damage — only a change that survives both does (`transpile_only()` is the reference).

The 146 Ambrosia "ambiguous" rows carry no SQL of their own, only `ambig_queries` — a **Python repr**
of a list, not JSON (`"'['SELECT ...']'"`). `json.loads` raises on it, which silently gave 129/146
rows a `TABLE unknown` stub in the first iteration. `parse_ambig_queries()` therefore walks a chain:
already-a-list -> `ast.literal_eval` -> `json.loads` -> recurse once for double-encoded values ->
regex salvage -> empty. The basis of every row is counted into
`stats.json["ambrosia_ambig_parse_basis"]` (all 146 resolve via `literal_eval`), and a test asserts
that all 146 rows reach the output with a synthesised schema and **0** `TABLE unknown` prompts.

Token lengths are measured with the model chat template when a local tokenizer exists; otherwise the
build falls back to a `chars/3.6` estimate and says so (`token_basis` in the COUNT TABLE and in
`stats.json`). Do not set `--max-seq-len` from an estimate; re-measure with the real tokenizer and the
real schema corpus.

---

## 4. The two modes that make this track worth running

### 4.1 `--teacher`: distillation instead of imitating raw SQLite gold

```bash
python training/build_data.py --schemas training/db_schemas.json \
  --teacher training/teacher_predictions.json --teacher-results training/teacher_results.json
```

Rationale: BIRD gold was written by annotators in SQLite style over SQLite databases. Our team's
Text2SQL tool answers with a *feature-driven pipeline* (light schema + evidence + dialect rules +
verification pass) and produces PostgreSQL. Training the adapter on raw gold teaches the annotators'
style and the exact syntax that loses points at scoring time (the evaluator transpiles gold but
executes the prediction verbatim). Distillation replaces the target with the teacher's SQL wherever
the teacher is **demonstrably not worse than gold**, so the adapter imitates the behaviour we actually
ship, on the questions where that behaviour is verified.

The keep rule is deliberately strict, and both halves are required:

1. the teacher SQL parses as PostgreSQL after canonicalisation, **and**
2. its execution result equals the gold execution result, compared as an unordered row set with a
   1e-6 numeric tolerance.

Execution is an **injected** callable (`execute(question_id, sql)`); `teacher_filter.py` never opens a
connection and never imports a driver. Runs are therefore one of two explicitly reported modes:

- `exec-verified` — an executor was injected (in production: the harness's psycopg runner);
- `parse-only` — no executor was injected. Kept rows are labelled `keep-parse-only` and
  `teacher_report.json` says `"exec_checked": false`. Parse-only is a reviewable smoke mode, never a
  claim of correctness.

`--teacher-results` supplies a recorded-results file and installs `ReplayExecutor`, the offline
stand-in used by the fixtures: `{"<qid>": {"gold": <result|{"error": ...}>, "teacher": <same>}}`.
The executed side is identified by normalised SQL equality with gold, so an identical teacher query
replays the gold result (correct: identical SQL, identical result).

Output: `teacher_report.json` (kept/dropped per reason: `keep-exec-match`, `unparseable-postgres`,
`exec-mismatch`, `exec-error`, `empty-prediction`, `gold-exec-error`), plus per-row
`"target_source": "teacher"|"gold"` in the jsonl. `--teacher-mode fallback` (default) keeps the gold
target where the teacher failed; `strict` keeps only teacher-verified BIRD rows. **The valid split
always keeps gold targets** — it is the set BIRD EX is measured on.

Input format: a JSON object `{question_id: sql}`, a JSON list of records (`question_id` +
`prediction`/`predicted_sql`/`sql`), or JSONL. `training/fixtures/teacher_predictions.json` is a
worked example.

### 4.2 `--feature-prompt`: train under the inference prompt surface

```bash
python training/build_data.py --schemas training/db_schemas.json \
  --feature-prompt training/feature_block.txt
```

Rationale: at inference the team's tool puts more in front of the model than schema + question —
dialect notes, identifier-quoting rules, numeric traps, schema-linking hints. If the adapter is
trained without that block but evaluated with it, the comparison confounds **weights** with **prompt**:
a gain could come from either. Feature-prompt mode trains under the same user-prompt surface used at
inference, so the LoRA-vs-base comparison isolates the weights.

Rendered SQL user prompt (the block sits between schema and evidence):

```
Schema:
TABLE frpm
  - CDSCode (text)
  ...
Feature block:
<contents of feature_block.txt, verbatim>
Evidence: <evidence string, BIRD only, when present>
Question: <question>
```

Rules, all asserted by tests:

- the block is inserted **verbatim** — no re-indent, no wrapping, no placeholder substitution; only
  line endings are normalised (`CRLF`/`CR` -> `LF`) and trailing newlines are trimmed;
- it applies to the SQL task only (the Ambrosia ambiguity head keeps its own prompt surface);
- it is recorded in `stats.json["feature_prompt"]` with the file name, char count and sha256-16, so a
  run is reproducible from the record.

What this does **not** replicate: the tool's multi-turn retries, the verification pass, and the
heavy schema variant. Those are harness-side behaviour, not prompt text; claiming otherwise would
overstate the experiment. A train/serve mismatch in any of them remains a confound to state out loud.

---

## 5. QLoRA training and the planner

```bash
# dry run: resolved config + TRL decision + provenance.json, no GPU, no model, no torch import
python training/train_qlora.py --plan --data training/out --out training/runs/e3-r16-alllinear

# the real run (7B, ~30-55 min on a 16 GB card; see the R3 design for the arithmetic)
python training/train_qlora.py --data training/out --out training/runs/e3-r16-alllinear \
  --epochs 3 --max-seq-len 3072 --per-device-batch 1 --grad-accum 16 --lr 1.5e-4 \
  --r 16 --alpha 32 --target-modules all-linear --seed 42
```

`--plan` prints the fully resolved config (every default folded in: `effective_batch`,
`steps_per_epoch`, `total_steps`, quantization, LoRA scaling, `completion_only_loss: true`) plus:

```
[sqlglot] installed=30.23.0 pin=30.23.0 match=True
[trl] installed=None required=>=0.16,<0.21 decision=blocked
[trl] action: pip install 'trl>=0.16,<0.21'
[data] dir=training/out data_hash=<sha256> schema_basis=file rows=train=684 valid=52
[gpu] not probed: --plan never imports torch
[provenance] training/runs/e3-r16-alllinear/provenance.json
```

**Lazy imports.** Importing `train_qlora` needs only the standard library; `torch`, `trl`,
`transformers`, `peft` and `datasets` are imported inside the training path and the TRL probe. This is
verified by a test that installs a meta-path blocker for those packages and then imports the module
(and runs `--plan`).

**`provenance.json`** is written by both paths:

```json
{
  "model": {"id": "Qwen/Qwen2.5-Coder-7B-Instruct", "quantization": "4bit-nf4 double-quant, compute bf16"},
  "data": {"data_hash": "<sha256 over the sorted per-file hashes>",
           "files": {"sft_all_train.jsonl": {"rows": 684, "sha256": "...", "sha256_16": "..."}},
           "schema_basis": "file"},
  "seeds": {"data": 42, "train": 42, "data_seed": 42},
  "pins": {"sqlglot": {"pin": "30.23.0", "installed": "30.23.0", "match": true},
           "trl": {"required": ">=0.16,<0.21", "installed": "0.16.1", "decision": "ok"}},
  "git": {"commit": "<HEAD>", "branch": "feat/lora-track", "dirty": false},
  "config": {"...": "the fully resolved config, including steps"},
  "trl": {"...": "the probe output: SFTConfig field names + completion_only_loss"}
}
```

The data guard runs **before** the GPU check: a dataset whose `schema_basis` is `synth-from-gold` or
`file+stub` is refused (the prompt would leak its own columns and the EX would be meaningless),
unless `--allow-smoke-data` is passed. A missing `stats.json` is refused for the same reason: an
unknown schema basis is not a safe basis.

One deviation from `r3-lora-design.md` §2.1 must be stated: the design says
`apply_chat_template(..., add_generation_prompt=False)` for training. This pipeline uses `True`,
because with TRL's prompt/completion columns the rendered prompt must already end with the assistant
role header; otherwise the completion is trained as if it were user text. The executed scratch
pipeline (which produced the baseline numbers) does the same.

---

## 6. What is possible offline, and what is not

| action | offline? | notes |
|---|---|---|
| `build_data.py --self-test` | yes | ~1 s, no network, no tokenizer, asserts the baseline |
| `build_data.py --schemas ...` | yes | given an already-dumped `db_schemas.json` |
| producing `db_schemas.json` | **no** | needs one harness build run: DB tunnel + credentials |
| `--teacher` with `--teacher-results` | yes | replayed results; proves the filter, not the teacher |
| `--teacher` against the real teacher | **no** | needs the harness executor / PostgreSQL |
| `--teacher` without results | yes | parse-only, explicitly reported |
| `--feature-prompt` | yes | a text file; no external dependency |
| `train_qlora.py --plan` | yes | no GPU, no torch, writes provenance |
| `train_qlora.py` (real) | **no** | CUDA + `trl`/`peft`/`bitsandbytes` + a model load |
| evaluating the adapter (EX) | **no** | DB tunnel + `MAX_RETRIES=1`; report `EX/241 (EX/211)` |

---

## 7. Tests

```bash
cd repo
python -m pytest tests/test_data.py -q
```

19 offline tests, ~15 s: self-test runtime/count table, both damage classes (14 unparseable + the
mutated id 2 recorded in `dropped.json` and absent from every training file), determinism (hash
equality), the `ambig_queries` fallback chain, all 146 real schemas and zero `TABLE unknown`, schema
refusal (no corpus / partial corpus) and the `--allow-missing-schemas` path, teacher keep/drop counts
(API with an injected executor + CLI with the canned fixture), strict distillation, feature-prompt
verbatim insertion and its missing-file error, the TRL compatibility decision (fake `trl` packages
with old/new `SFTConfig` fields), lazy imports, `--plan` config + provenance, the smoke-data guard,
and dev-set leakage. No test downloads model weights.

---

## 8. Open items

- The real run needs `db_schemas.json` from a harness build (teacher credentials + MIPT tunnel). Until
  then every number here is a data/plumbing number, not an EX number.
- Teacher predictions must come from the team's feature-driven tool on BIRD-train questions; the
  executor must return rows (not formatted strings) so the comparison stays set-based.
- `--max-seq-len` must be re-measured with the real schema corpus before the long run; the current
  p50/p95/p99 come from synthesised or stub schemas.
