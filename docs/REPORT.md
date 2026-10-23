# T2 — Serving base + adapter and measuring the branch through the real harness

Status: **in progress** (runs E1/E3 in flight; this file is being filled as measurements land).
Branch `feat/lora-track`, nothing committed. Every number below is attached to the
command that produced it and to the artifact that stores it.

---

## 1. What was measured, and against what

| run | model | data | artifact |
|---|---|---|---|
| `e1-bird-large` | base `Qwen2.5-Coder-7B-Instruct` (4-bit) | `data/bird_large.json` (241) | `runs/t2/e1-bird-large/query_results.json` |
| `e1fair-bird-large` | same base, **`use_evidence=True`** (stock entrypoint setting; §7) | same 241 | `runs/t2/e1fair-bird-large/query_results.json` |
| `e3-bird-large` | same base + `runs/e3-r16/adapter` (QLoRA r=16) | same 241 | `runs/t2/e3-bird-large/query_results.json` |
| `e1-ambrosia-large` | base | `data/ambrosia_large.json` (240) | `runs/t2/e1-ambrosia-large/query_results.json` (merged from 4 shards) |
| `e3-ambrosia-large` | adapter | same 240 | `runs/t2/e3-ambrosia-large/query_results.json` (merged from 4 shards) |

Ambrosia is run in **four shards of ≤14 databases each** (`runs/t2/chunks/ambrosia_part*.json`),
not in one process: the harness builds one tool per `db_id` and each tool holds its own
database connection, so 55 Ambrosia databases need 55 simultaneous tunnel channels. The
captain's forwarder allows 24; the first unsplit attempt starved the tunnel and produced
connection errors. Shards keep ≤14 channels in use and are merged afterwards:

```bash
python -c "import glob,json; out={}; [out.update(json.load(open(p,encoding='utf-8'))) for p in sorted(glob.glob('runs/t2/e1-ambrosia-part*/query_results.json'))]; assert len(out)==240; json.dump(out, open('runs/t2/e1-ambrosia-large/query_results.json','w',encoding='utf-8'), ensure_ascii=False, indent=2); print(len(out))"
# identical one-liner with e3-ambrosia-part* -> runs/t2/e3-ambrosia-large/query_results.json
```

`runs/t2/chunks/parts.json` records the shard sizes (40/40/69/91 = 240).

Predictions are produced by the **unchanged** course tool (`Text2SQLGenerator`, tool v2)
through `dev_tools/run_benchmark.py --predict-only`; scoring is
`dev_tools/fixed_eval.py` in `mode="local"` (the shipped evaluator's semantics: gold
rewritten `sqlite→postgres` with the repo's own regex, prediction executed as written,
`set(predicted) == set(gold)`), so the numbers are comparable with the team's frozen
42.32 % baseline.

## 2. Environment facts that shaped the runs (measured here)

1. **bf16 serving of the 7B does not fit this GPU.** `--load-in-4bit` is mandatory:
   bf16 used 16,016 of 16,376 MiB and Windows silently paged it — the same 878-prompt-token
   request took **184.7 s**; with 4-bit (7.4 GiB) it takes **1.95 s**. The 1.5B smoke
   answers in 1.45 s.
2. **Percent-encoded `DB_PASS` is required.** `benchmarks/base.py` builds the DSN with an
   f-string and does not encode; the course password contains `/`, so the harness must be
   given `quote(password, safe="")`. Without it every connection fails with a malformed
   DSN. (`dev_tools/fixed_eval.py` and `dump_schemas.py` encode internally.)
3. **`MAX_RETRIES` is inert.** It appears nowhere in `benchmarks/` or the tool; the v2 tool
   has its own budget (1 ambiguity call + 1 generation + ≤1 repair, each with transport
   retries). Setting the env var changes nothing — it is recorded as `MAX_RETRIES=1` only
   for completeness.
4. **The harness's own evaluation cannot be on the measurement path.** `benchmarks/evaluate_bird.py`
   creates a new SQLAlchemy engine per question and connects with no timeout; over the SSH
   tunnel that hung a `bird_small` run for minutes (`evaluate_bird.py:82`) and exhausted the
   tunnel's channel budget. Hence `--predict-only` + `fixed_eval`. The harness evaluation
   itself was still exercised once (see §6) and prints `total_tokens: 0` because
   `autogen_ext` 0.7.5's usage object has no `total_tokens` field.
5. **MCP import shim.** `src/adv_text2sql/mcp_servers/__init__.py` imports `fastmcp`, which
   is not installed; `dev_tools/fastmcp_shim/sitecustomize.py` on `PYTHONPATH` installs a
   minimal stand-in, so the real tool class is importable with no edits under `src/`.
   The shim also provides `ADVTEXT2SQL_FAULTHANDLER_SECONDS` for stack dumps on hangs.

## 3. Serving

```bash
training/.venv-train/Scripts/python.exe -m dev_tools.serve_openai \
    --model D:\AI\Models\qwen2.5-coder-7b-instruct --served-name qwen2.5-coder-7b \
    --port 8000 --max-new-tokens 512 --load-in-4bit --device cuda
# adapter run: add --adapter runs/e3-r16/adapter --served-name qwen2.5-coder-7b+e3-r16
```

Greedy decoding (temperature is accepted and ignored unless `--allow-sampling`), real
`usage` from the tokenizer, `/v1/models` reports the served name, streaming rejected with
400. The harness is configured exactly like vLLM:

```bash
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_API_KEY=EMPTY
export LLM_MODEL_NAME=qwen2.5-coder-7b          # or qwen2.5-coder-7b+e3-r16
```

## 4. Commands per run

```bash
# environment (loads repo/.env, percent-encodes DB_PASS, freezes the model config)
. runs/t2/run_env.ps1

# predictions (no scoring inside the harness)
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only \
    --run-id e1-bird-large --query-file data/bird_large.json \
    --answer-file data/bird_large.json --db-url localhost:5444 --runs-dir runs/t2

# scoring on the live PostgreSQL through the tunnel
python -m dev_tools.score_runs score --kind bird \
    --predictions runs/t2/e1-bird-large/query_results.json \
    --answers data/bird_large.json --db-url localhost:5444 \
    --out runs/t2/scores/e1-bird-large.json

# Ambrosia is an ambiguity-detection task (no SQL is executed)
TEXT2SQL_AMBIGUITY_MODE=strict python -m dev_tools.run_benchmark --allow-llm-spend --predict-only \
    --run-id e1-ambrosia-large --query-file data/ambrosia_large.json \
    --answer-file data/ambrosia_large.json --db-url localhost:5444 --runs-dir runs/t2
python -m dev_tools.score_runs score --kind ambrosia \
    --predictions runs/t2/e1-ambrosia-large/query_results.json \
    --answers data/ambrosia_large.json --out runs/t2/scores/e1-ambrosia-large.json

# paired comparisons (exact McNemar + 10k paired bootstrap, seed 0)
python -m dev_tools.score_runs compare \
    --current runs/t2/scores/e3-bird-large.json \
    --reference runs/t2/scores/e1-bird-large.json \
    --frozen ../artifacts/pred_scores.json \
    --out runs/t2/scores/paired-bird-e3-vs-e1.json
```

`--predict-only` exists because the harness's evaluation hangs on a flaky tunnel (§2.4);
the harness's own `run()` was exercised separately on `bird_small` (§6).

## 5. Results

Artifacts: `runs/t2/scores/*.json` (per-question ledgers included), predictions under
`runs/t2/<run-id>/query_results.json`. All BIRD numbers are `mode="local"` on the live
PostgreSQL through the tunnel, greedy decoding, `--max-new-tokens 512`, tool v2 with its
default `birdsafe` ambiguity gate (which never returns "ambiguous", so BIRD predictions are
never wasted on the gate).

### 5.1 BIRD `bird_large` (241 questions)

| run | EX/241 | EX/209 (gold-executable) | gold unexec. | pred. unexec. | simple / moderate / challenging | soft-F1 |
|---|---|---|---|---|---|---|
| **E1** base `Qwen2.5-Coder-7B-Instruct` (4-bit) | **51/241 = 21.16 %** <br>95 % CI 16.5–26.8 | **51/209 = 24.40 %** <br>CI 19.1–30.7 | 32 | 40 | 34/91 = 37.4 % · 13/89 = 14.6 % · 4/61 = 6.6 % | 0.2974 |
| **E3** base + `runs/e3-r16/adapter` | **61/241 = 25.31 %** <br>CI 20.2–31.2 | **61/209 = 29.19 %** <br>CI 23.4–35.7 | 32 | 36 | 37/91 = 40.7 % · 11/89 = 12.4 % · 13/61 = 21.3 % | 0.3351 |
| **E1-fair** base, `use_evidence=True` (§7) | **84/241 = 34.85 %** <br>CI 29.1–41.1 | **84/209 = 40.19 %** | 32 | 39 | 47/91 = 51.6 % · 24/89 = 27.0 % · 13/61 = 21.3 % | 0.4677 |
| team's frozen baseline (id-level only) | 102/241 = 42.32 % | — | — | — | n/a | — |

The 32 unexecutable gold items are **exactly** the known set — the 30 regex-syntax kills
plus ids 4 and 35 (`UndefinedColumn: column t1.Charter School (Y / NULLIF(N,0))`) — and
nothing else failed. So 209 questions are scoreable and the honest maximum is
**208/241 = 86.3 %** (id 572 executes but its gold `title` literal was mutated).

Wall clock and cost per run (from `runs/t2/<run-id>/run.json`, same 4-bit server):

| run | wall clock | prompt tokens | completion tokens |
|---|---|---|---|
| E1 `bird_large` | 912 s (15.2 min) | 473 301 | 22 209 |
| E3 `bird_large` | 1 250 s (20.8 min) | 453 007 | 21 509 |
| E1-fair `bird_large` (`use_evidence=True`) | 833 s (13.9 min) | 484 408 | 20 682 |
| E1 Ambrosia (4 shards) | 686 s (11.4 min) | 224 446 | 11 257 |
| E3 Ambrosia (4 shards) | **434 s (7.2 min)** | **103 484** | **3 785** |

**Serving VRAM (measured, all BIRD runs share one configuration).** `nvidia-smi` sampled at
1 Hz while replaying six real 860-prompt-token harness requests against the E1-fair server:
idle after load **8,211 MiB**, peak **8,218 MiB** of 16,376 MiB (`runs/t2/vram_samples.json`).
Spot samples taken during the E1 and E3 runs stayed in 7,684–8,009 MiB. So the 4-bit server
uses ~8.2 GiB regardless of the run; the bf16 contrast (16,016 MiB, paged, 184.7 s per
request) is in §2, item 1. Caveat: `run.json` does not carry `torch.cuda.max_memory_allocated`
(the server owns the process), so the per-run peak is a sampled figure, not a per-process
counter — recording it per run is a follow-up for `serve_openai.py` (§7.4).

The E3 Ambrosia row is itself evidence of the collapse (§5.2): once the gate answers
"ambiguous", the pipeline stops before SQL generation, so the adapter spent less than half
the tokens of the base model on the same 240 questions.

### 5.2 Ambrosia `ambrosia_large` (240 questions, `TEXT2SQL_AMBIGUITY_MODE=strict`)

Strict mode is required here: the default `birdsafe` never answers "ambiguous", which on
Ambrosia *is* the 70.83 % constant floor by construction.

| run | correct | detection rate | never-ambiguous floor | predicted ambiguous | precision / recall | gold ambiguous |
|---|---|---|---|---|---|---|
| E1 base | 143/240 | **59.58 %** | 70.83 % | 61 | 0.279 / 0.243 | 70 |
| E3 adapter | 70/240 | **29.17 %** | 70.83 % | **240** | 0.292 / **1.000** | 70 |

**E3 collapsed to "always ambiguous"**: it answered `ambiguous` for all 240 questions, so
it scores exactly the always-ambiguous constant (70/240 = 29.17 %), one of the documented
Ambrosia constants. E1 is also *below* the never-ambiguous floor. Neither number is an
"Ambrosia accuracy"; they are ambiguity detection next to constant baselines (70.83 % /
29.17 % / 62.5 % / 37.5 %).

### 5.3 Paired comparisons (exact McNemar + 10 000 paired-bootstrap resamples, seed 0)

| comparison | n | b | c | exact p | ΔEX | 95 % CI | verdict |
|---|---|---|---|---|---|---|---|
| **E3 adapter vs E1 base (BIRD)** | 241 | 26 | 16 | 0.164 | **+4.15 pp** | **[−0.83, +9.54] pp** | **inconclusive** |
| E3 adapter vs frozen (BIRD) | 241 | 20 | 61 | 5.7e-06 | −17.01 pp | [−23.65, −9.96] pp | significantly worse than the frozen file |
| E1 base vs frozen (context) | 241 | 10 | 61 | 4.6e-10 | −21.16 pp | [−27.39, −14.94] pp | same, slightly worse |
| E3 adapter vs E1 base (Ambrosia) | 240 | 53 | 126 | 4.9e-08 | −30.42 pp | [−40.4, −20.0] pp | significantly worse |
| **E1-fair vs E1** (evidence only, §7) | 241 | 39 | 6 | 5.4e-07 | **+13.69 pp** | [**+8.71, +19.09**] pp | **evidence is decisive** |
| **E1-fair vs frozen** (§7) | 241 | 21 | 39 | 0.0273 | **−7.47 pp** | [−13.69, −1.24] pp | gap narrows, still open |
| **E3 adapter vs E1-fair** (§7) | 241 | 15 | 38 | 0.0022 | −9.54 pp | [−15.35, −3.73] pp | mixes adapter and evidence — not an adapter measurement |

Applying the pre-registered rule (claim "better" only if `b − c ≥ 6` **and** `p < 0.05`
**and** the CI excludes 0): `b − c = 10` and the CI's lower bound is −0.83 pp, but
`p = 0.164`, so the rule is **not** met. The honest statement is
**"inconclusive at n = 241"**: the adapter fixes 26 questions the base model failed and
breaks 16 it got right, which is +10 net and 4.15 pp, but a discordance pattern of 26/16
is compatible with chance (exact McNemar p = 0.16).

The adapter does move the difficulty profile: simple 37.4 → 40.7 %, challenging
6.6 → 21.3 %, moderate 14.6 → 12.4 %; soft-F1 0.297 → 0.335; invalid-SQL predictions
40 → 36. That is consistent with T1's sample: the adapter learned the *dialect contract*
(one PostgreSQL statement, no fences/backticks/IFNULL) rather than schema linkage, which
helps most where the base model produced invalid SQL and least where the schema is hard.

### 5.4 What the numbers do and do not say

* **BIRD, adapter vs base: +4.15 pp, not significant.** Not a claim of improvement.
* **Ambrosia: the adapter is significantly worse** (−30.4 pp) because it answers
  "ambiguous" unconditionally. On a 684-row dataset with 146/480 ambiguous Ambrosia rows
  and 2:1 negatives, the gate degenerated to the majority-ish shortcut; the 4-bit adapter
  at r=16 transfers a *style*, not a calibrated decision. Any claim about Ambrosia needs a
  different training mix or a decision threshold, not more epochs.
* **The frozen 42.32 % is not reproducible from here** (see limitations); both our runs are
  ~17–21 pp below it. The difference is dominated by configuration, not by the adapter.
* Every number above is reachable from the artifacts without re-running the model:
  `python -m dev_tools.score_runs compare ...` recomputes the paired tables; the ledgers
  carry per-question scores.
* The two `E1-fair` rows answer the "fair comparison" question; §7 is the analysis.

---

## 6. Harness wiring proof (unchanged entrypoint)

```bash
. runs/t2/run_env.ps1
python bird_benchmark.py     # hard-coded data/bird_small.json, use_evidence=True
```

`bird_benchmark.py` completed with exit 0 (wall 790 s, of which ~8 min were a dead tunnel
and retries) — no code edits, only env. It produced 22/22 predictions and printed its own
report (13.64 %, 3/22, per-difficulty simple 22.22 / moderate 0.00 / challenging 33.33).
**That number is not a measurement of the model**: most of its `run_evaluation` calls hit
the tunnel outage (`psycopg OperationalError`/`ConnectionTimeout`), so it is recorded only
as proof that the harness runs unchanged against our server.

## 7. Is the gap to the frozen baseline configuration or model?

**Short answer: it was mostly configuration, and `use_evidence` is the part we can name.**
Turning on the stock entrypoint's evidence flag moves the same base model on the same 241
questions from 21.16 % to **34.85 %** — that is **13.69 pp of the 21.16 pp gap (65 %)**.
The remaining **−7.47 pp does not close**, and it cannot be attributed with the artifacts we
have.

### 7.1 What was held fixed, what changed

| | E1 | E1-fair |
|---|---|---|
| model, dtype | Qwen2.5-Coder-7B-Instruct, 4-bit NF4 | identical |
| serving | `serve_openai.py`, greedy, `max_new_tokens=512` | identical |
| tool | v2, `birdsafe` ambiguity gate, pruning, one repair | identical |
| database | live PostgreSQL through the tunnel, 8 BIRD databases | identical |
| questions | `data/bird_large.json`, 241 | identical |
| **prompt** | `question: <q>` | **`question: <q>, evidence (may be empty): <e>`** (`benchmarks/bird.py:32-33`; evidence non-empty on **221/241**) |
| predictions / ledger | `runs/t2/e1-bird-large`, `runs/t2/scores/e1-bird-large.json` | `runs/t2/e1fair-bird-large`, `runs/t2/scores/e1fair-bird-large.json` |

### 7.2 The `use_evidence` question on its own

| comparison | EX | b | c | exact McNemar p | ΔEX | 10k bootstrap 95 % CI |
|---|---|---|---|---|---|---|
| **E1-fair vs E1** | 84/241 = 34.85 % vs 51/241 = 21.16 % | **39** | **6** | **5.4e-07** | **+13.69 pp** | **[+8.71, +19.09] pp** |

Enabling evidence fixes **39** questions and breaks **6**. That is not noise and it is not a
couple of questions: it is the single largest effect measured in this whole branch so far,
larger than the adapter's entire contribution. Per difficulty it helps everywhere
(simple 34→47, moderate 13→24, challenging 4→13 of 61) and it lifts soft-F1 from 0.297 to
0.468.

**So the T2 comparison to the frozen baseline was not apples-to-apples.** Whoever produced
the frozen 42.32 % ran the stock entrypoint, which sets `use_evidence=True`; our E1/E3 ran
`run_benchmark.py`, which did not. That is a defect in our own harness wrapper, not in the
frozen file.

### 7.3 The rest of the gap: not explained, and it stands

| comparison | E1-fair | frozen | b | c | exact p | ΔEX | 95 % CI |
|---|---|---|---|---|---|---|---|
| **E1-fair vs frozen 42.32 %** | 84/241 = 34.85 % | 102/241 = 42.32 % | 21 | 39 | **0.0273** | **−7.47 pp** | **[−13.69, −1.24] pp** |

The frozen file is still significantly better. So **the gap stands at 7–14 pp** and the
honest sentence is: "the base model with the correct prompt is 7.5 pp below the team's
frozen run, and we cannot say why."

What is ruled out, and what is not:

* **Decoding length is ruled out.** No prediction reaches the 512-token cap: E1-fair
  completions are 10–194 tokens (median 49, p95 89), E1 is 10–203 (median 51, p95 103), and
  all 241 predictions in both files parse as PostgreSQL (`runs/t2/inspect_predictions.py`).
  A larger `max_new_tokens` cannot buy anything.
* **Tool revision is not testable from here.** The frozen artifact is per-question scores
  only — no SQL, no prompt, no commit, no config. A different `src/` revision (different
  system prompt, different ambiguity gate, different repair policy), a different
  `max_tokens`, or a different model id would all produce exactly this signature and we
  cannot separate them.
* **The adapter does not explain it either way.** E3 was measured without evidence:
  E3 vs E1 is +4.15 pp (b=26, c=16, p=0.164, inconclusive) and E3 vs E1-fair is −9.54 pp
  (b=15, c=38, p=0.0022). The second number is *not* an adapter measurement — it mixes
  adapter-on/evidence-off against adapter-off/evidence-on, so it says only that the
  evidence effect (~+13.7 pp) dominates the adapter effect (~+4 pp). The clean adapter
  comparison remains E3 vs E1.

### 7.4 What would settle it definitively

Re-run the frozen configuration end-to-end and compare like with like: the team's tool
revision (a commit hash), its exact prompt, its decoding parameters and its `max_tokens`,
then run base **and** adapter with evidence on. That needs the team's tool revision and the
configuration of the May run — neither is in the artifacts we have. Two concrete follow-ups
that are cheap on our side:

1. **E3-fair** (adapter + `use_evidence=True`, one ~15-minute run, `runs/t2/` already has the
   script pattern): the missing member of the 2×2 design `{base, adapter} × {no evidence,
   evidence}`. Until it exists, no adapter claim should quote the frozen baseline.
2. Record `torch.cuda.max_memory_allocated`, `max_tokens`, `use_evidence` and the tool commit
   in every `run.json` (today only the wrapper's argv-equivalent is recorded) so a future
   comparison cannot repeat the E1/E1-fair mismatch by accident.

## 8. Honest limitations

* **Sharding does not change predictions.** Each Ambrosia question is answered by the tool
  built for its own `db_id`, so the shard boundary cannot alter a prompt; the merged file
  is a pure union (240/240 ids, no duplicates). It changes only how many connections the
  harness holds at once.
* **Transient tunnel errors are retried, once.** A dropped forwarder surfaces as
  `OperationalError: server closed the connection unexpectedly`; in the first E1 scoring
  pass that mis-scored gold questions 699 and 714 as failures (they execute fine). 
  `dev_tools/fixed_eval.py` now retries one time on `OperationalError`, which is why the
  final ledger shows exactly the 32 genuinely-dead gold items and nothing else.
* **`pred_err` is a model property, not an infrastructure one.** 40 (E1) and 36 (E3)
  predictions failed to execute at all (bad identifiers, mostly), which is the tool's real
  failure rate on this data.

* **In-domain schemas.** All 8 BIRD schemas are the course's own; there is no
  out-of-distribution check, so the numbers say nothing about generalisation.
* **The 208/241 ceiling.** The shipped evaluator destroys 30 gold queries outright and
  mutates 3 more (ids 4, 35, 572), so no submission can exceed **208/241 = 86.3 %** in
  `mode="local"`. EX is therefore reported over three denominators: EX/241 (the harness
  number), EX/209 (gold that actually executes on the live server) and the 208 maximum.
* **DB snapshot drift.** The team's frozen 42.32 % baseline was produced in May against a
  snapshot we cannot re-create; the current server has 900+ databases and no snapshot
  guarantee. The paired comparison against it is id-level (scores only — the frozen file
  has no SQL), so it cannot be re-executed.
* **Single seed.** One greedy run per model, no decoding variance, no repeated samples.
  Greedy makes runs reproducible, not uncertainty-free.
* **Ambrosia** numbers are ambiguity detection next to the constant "never say ambiguous"
  floor (170/240 = 70.83 %), never a benchmark accuracy.
* **4-bit serving** is a different numerics from the 4-bit training forward pass; adapter
  behaviour under bf16 serving may differ slightly.
* **The tool is v2** with pruning/ambiguity-gate; the frozen baseline may have used a
  different tool revision, so part of any delta is tool-side, not adapter-side.






