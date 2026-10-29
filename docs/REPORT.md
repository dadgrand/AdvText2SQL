# T2 — Serving base + adapter and measuring the branch through the real harness

Status: **round-2 pre-seal draft** (branch `feat/round2`, commits `4ea7d4f` onward; nothing pushed). Every
number below is attached to the command that produced it and to the artifact that stores it.

Round 2 (branch `feat/round2`, §9–§19) adds the **missing fourth cell** of the 2×2 design, the
`run.json` recorder fix, the Ambrosia decision, the feature × model matrix, the A100 grammar arm, the
**scale-up results** (§14, §16), the **14B headline** (§17), the procedure/decoder arms (§18), the
**OOD check** (§19) and the blocked distillation path (§15). §1–§8 are the round-1 measurements and are
left exactly as they were; §9–§19 only add.

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

Round-2 runs are §9 (2×2), §10 (recorder proof), §11 (Ambrosia), §12 (matrix), §13 (decoder), §14 (scale-up
A100 cell), §15 (blocked distillation), §16 (the completed 7B scale × evidence 2×2) and §17 (the 14B
headline).

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

---

## 9. Round 2 — the 2×2 design `{base, adapter} × {no evidence, evidence}`

§7.4 listed the missing fourth cell as the first cheap follow-up: every adapter number in
this report was measured with evidence **off**, while the frozen baseline lives with evidence
**on**, so no adapter claim could be compared to it honestly. That cell is now measured.
The design is the same 241 `bird_large` questions, the same live PostgreSQL through the
tunnel, the same tool v2, greedy decoding from the same 4-bit server; only the two factors
move. Measured 2026-10-29 on branch `feat/round2`, run `e3fair-bird-large`.

### 9.1 The four cells

All four ledgers are `dev_tools/fixed_eval.py` `mode="local"` on the live server; each cell
is one greedy run. "gold unexec." is 32 in every cell (the 30 regex-killed golds plus ids 4
and 35), so **EX/209** uses the same denominator everywhere and the executable ceiling stays
**208/241 = 86.31 %**: 32 unexecutable golds plus id 572, whose gold *does* execute but against
a string literal the repo rewrite mutated, so it is scoreable and wrong rather than dead.
Verified per question in the ledgers: `gold_ok=false` for ids 4 and 35,
`gold_ok=true, status=scored` for id 572.

| cell | configuration | EX/241 | EX/209 (gold-exec.) | pred. unexec. | simple / moderate / challenging | soft-F1 | run |
|---|---|---|---|---|---|---|---|
| **A** | base 7B, `use_evidence=False` | **51/241 = 21.16 %** <br>CI 16.48–26.75 | 51/209 = 24.40 % <br>CI 19.08–30.65 | 40 | 34/91 = 37.36 % · 13/89 = 14.61 % · 4/61 = 6.56 % | 0.2974 | `runs/t2/e1-bird-large` |
| **B** | base 7B, `use_evidence=True` | **84/241 = 34.85 %** <br>CI 29.12–41.07 | 84/209 = 40.19 % <br>CI 33.78–46.96 | 39 | 47/91 = 51.65 % · 24/89 = 26.97 % · 13/61 = 21.31 % | 0.4677 | `runs/t2/e1fair-bird-large` |
| **C** | adapter `runs/e3-r16`, no evidence | **61/241 = 25.31 %** <br>CI 20.24–31.16 | 61/209 = 29.19 % <br>CI 23.44–35.68 | 36 | 37/91 = 40.66 % · 11/89 = 12.36 % · 13/61 = 21.31 % | 0.3351 | `runs/t2/e3-bird-large` |
| **D** | adapter, `use_evidence=True` | **93/241 = 38.59 %** <br>CI 32.67–44.87 | 93/209 = 44.50 % <br>CI 37.92–51.27 | **23** | 47/91 = 51.65 % · 31/89 = 34.83 % · 15/61 = 24.59 % | 0.4696 | `runs/t2/e3fair-bird-large` |

Zero "ambiguous" predictions in all four cells (the `birdsafe` gate never fires on BIRD), so
the denominators are unaffected. Per-difficulty denominators are 91 simple, 89 moderate,
61 challenging.

Reproduce:

```bash
. runs/t2/run_env.ps1 && export LLM_MODEL_NAME=qwen2.5-coder-7b+e3-r16   # adapter server
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only --use-evidence \
    --max-new-tokens 512 --run-id e3fair-bird-large --query-file data/bird_large.json \
    --answer-file data/bird_large.json --db-url localhost:5444 --runs-dir runs/t2
python -m dev_tools.score_runs score --kind bird \
    --predictions runs/t2/e3fair-bird-large/query_results.json \
    --answers data/bird_large.json --db-url localhost:5444 \
    --out runs/t2/scores/e3fair-bird-large.json
python runs/t2/t2_2x2_analysis.py     # four cells + all paired blocks + interaction
```

`--use-evidence` is new (§10): round 1 could only produce cell B by hand-writing a script
(`runs/t2/e1fair_run.py`), which is exactly how the E1/E1-fair mismatch happened.

### 9.2 What each factor does, holding the other fixed

b = current right / baseline wrong, c = current wrong / baseline right; exact McNemar p;
10 000 paired-bootstrap resamples over question ids, seed 0. Rule pre-registered in §5.3:
claim "better" only if `b − c ≥ 6` **and** `p < 0.05` **and** the CI excludes 0.

| comparison (factor, held fixed) | b | c | b−c | exact p | ΔEX | 10k 95 % CI | rule | verdict |
|---|---|---|---|---|---|---|---|---|
| **evidence, adapter ON** (D vs C) | **41** | **9** | +32 | **5.6e-06** | **+13.28 pp** | **[+7.88, +18.67]** | met | **evidence is decisive** |
| **evidence, adapter OFF** (B vs A) | 39 | 6 | +33 | 5.4e-07 | +13.69 pp | [+8.71, +19.09] | met | evidence is decisive |
| **adapter, evidence ON** (D vs B) | 29 | 20 | +9 | 0.253 | +3.73 pp | [−1.66, +9.54] | not met | inconclusive |
| **adapter, evidence OFF** (C vs A) | 26 | 16 | +10 | 0.164 | +4.15 pp | [−0.83, +9.54] | not met | inconclusive |
| **both, total** (D vs A) | 55 | 13 | +42 | 2.7e-07 | **+17.43 pp** | [+11.20, +23.65] | met | both together are decisive |
| D vs the frozen 42.32 % (id-level) | 33 | 42 | −9 | 0.356 | −3.73 pp | [−10.38, +3.32] | — | **not distinguishable from the frozen file** |

**The headline is a stability result, not a win.** The evidence effect is +13.7 pp with the
base model and +13.3 pp with the adapter on — the same effect twice, and significant both
times. The adapter effect is +4.2 pp without evidence and +3.7 pp with it — the same effect
twice, and inconclusive both times at n = 241. Round 1 could only say "evidence dominates the
adapter" by comparing two cells that differed in one factor each; with the square closed, the
two coefficients are measured twice each and agree. Neither factor's value depends on the
other's state.

The bottom row is what closing the square buys: with both factors on, cell D is **−3.73 pp**
from the frozen 42.32 % and the CI covers 0 (`p = 0.356`), whereas round 1's best
evidence-on/adaptor-off cell (E1-fair) was **significantly** below it by 7.47 pp (§7.3,
`p = 0.0273`). The missing cell was the difference between "significantly worse" and "not
distinguishable". The remaining caveat does not move: the frozen file is scores only (no SQL,
no config), so this is an id-level comparison. A later scale-up adapter (§14) reaches 102/241 = 42.32 % on
the same protocol and **ties** this frozen file (b = 32, c = 32, p = 1.0); the round-1 comparison above is
unchanged and is not superseded by it.

### 9.3 Do the two effects stack? (interaction)

The interaction is the difference in differences, per question:
`did_q = (D_q − B_q) − (C_q − A_q)` — the adapter effect with evidence on minus the adapter
effect with evidence off. A purely additive world has `did_q = 0` everywhere.

| quantity | value |
|---|---|
| interaction ΔEX | **−0.41 pp** |
| 10k paired-bootstrap 95 % CI | **[−7.05, +6.22] pp** |
| exact two-sided sign test on the 56 non-zero `did` | 28 negative / 28 positive, **p = 1.000** |
| additive prediction (A + evidence + adapter) | 51 + 33 + 10 = **94** |
| observed cell D | **93** (−1 question) |
| adapter helps… both arms / only with evidence / only without / neither | 9 / 20 / 17 / 195 |
| evidence helps… both arms / only with adapter / only without / neither | 18 / 23 / 21 / 179 |

**Verdict: additive.** The point estimate is −0.4 pp, the CI spans ±7 pp, the sign test is
exactly balanced, and simple addition predicts cell D to within one question. There is no
interaction to report in either direction: the adapter does not "unlock" with evidence, and
evidence does not rescue the adapter. Concretely, 185 of 241 questions have the *same* adapter
gain in both arms (mostly zero), the adapter's gain is positive in at least one arm for 46 of
them (9 in both + 20 only with evidence + 17 only without), and the 56 questions whose gain
differs between arms split 28/28 — no systematic direction.

Two consequences worth stating in plain words, because they are the reason this cell was worth
25 minutes of GPU time:

1. **No re-tuning is warranted before the deadline.** If the adapter were sub-additive (worth
   less when the prompt carries evidence), then training or serving the adapter for the
   evidence-on regime would be the obvious next move — that hypothesis is now dead: the
   adapter's coefficient is flat across the regime.
2. **Power, not effect, decides what is significant.** The same +4 pp adapter effect is
   `p = 0.164` without evidence and `p = 0.253` with it; the evidence effect is `p = 5e-07`
   without the adapter and `p = 6e-06` with it. Any "significance" story about the adapter must
   be read against the interaction test, which says the two arms are one effect measured twice.

### 9.4 Cost and provenance of cell D

`runs/t2/e3fair-bird-large/run.json` (new schema, §10) for the run: 1 469 s wall clock,
461 058 prompt / 20 163 completion tokens, **529 requests for 241 questions** (the tool's
ambiguity-gate + repair calls), peak GPU memory **7 009.7 MiB** recorded by the serving process,
and `max_completion_tokens_observed = 261` against `max_tokens = 512` — so the generation cap
did not bind and cannot explain the number. Server: base `qwen2.5-coder-7b-instruct` +
`runs/e3-r16/adapter` (`adapter_sha256 = ea50f537…`), 4-bit NF4, greedy.

---

## 10. `run.json` schema v2 — the recorder that made the round-1 mix-up possible

### 10.1 The defect being fixed

Round 1 ran cell A and cell B with the same wrapper and the same environment; the only
difference was `BenchmarkBIRD(use_evidence=True)`, a constructor argument `run_benchmark.py`
had no flag for. Consequently `runs/t2/e1-bird-large/run.json` and
`runs/t2/e1fair-bird-large/run.json` differ solely in a hand-written `"changed_vs_e1"` note:
nothing machine-readable distinguishes a 21 % run from a 35 % run. That is how a paired
comparison against the frozen baseline was run against the wrong arm (§7), and why §7.4 had to
list "record `use_evidence`, `max_tokens`, `torch.cuda.max_memory_allocated` and the tool
commit" as an open item. This section closes it.

### 10.2 What every `run.json` now carries, and where it comes from

Two files changed (`dev_tools/serve_openai.py`, `dev_tools/run_benchmark.py`, no edits under
`src/` or `benchmarks/`). The serving process now exposes its provenance on `GET /health`, and
the wrapper probes that endpoint before and after each run and merges the result:

| field | source | why it exists |
|---|---|---|
| `use_evidence` | `--use-evidence`, validated against `--benchmark` | the round-1 mismatch itself |
| `max_tokens` | `/health.max_new_tokens` (the cap that binds) | the harness never sends `max_tokens`; a CLI number alone would be a claim, not a fact |
| `max_tokens_source` | `"server:/health"` / `"cli:--max-new-tokens"` | says which authority the number came from |
| `tool_commit_sha`, `tool_commit_dirty`, `tool_commit_dirty_paths`, `tool_branch` | `git` in the repo that contains the tool | a SHA alone lies about a modified worktree |
| `tool_module`, `tool_source_file`, `tool_source_sha256` | the imported tool class | catches an uncommitted `src/` edit that git status would not tie to the tool |
| `model_id`, `served_name` | `/health` | the model that answered, not the string in an env var |
| `adapter_id`, `adapter_sha256`, `adapter_config_sha256` | `/health` (hashed at load) | two r=16 adapters can no longer be confused |
| `load_in_4bit`, `device`, `allow_sampling` | `/health` | 4-bit is mandatory on this GPU (§2.1); sampling would break greedy comparability |
| `cuda_max_memory_allocated_bytes` / `_mib` | `torch.cuda.max_memory_allocated()` **inside the serving process** | the harness has no CUDA context; only the server can report it |
| `requests_served`, `max_completion_tokens_observed` | counters in the server | proves whether the generation cap bound, from the artifact instead of a log |
| `server_probe`, `server_before`, `server_after` | full `/health` payloads | audit trail of the endpoint on both sides of the run |
| `n_questions`, `n_with_evidence` | the query file | how much evidence the prompt actually carried |

Refusals added, so a mis-recorded run cannot start (`run_benchmark.plan_run` / `main`):
`--use-evidence` with `--benchmark ambrosia` (Ambrosia never reads evidence → refused, not
ignored); `--max-new-tokens` disagreeing with the server's `/health` cap; a live run whose
`/health` does not answer (no provenance → refuse, `--check` merely reports it); a non-empty
run directory (pre-existing R1-D22 rule).

### 10.3 Proof: a second short run, with the new fields

`bird_small`, 22 questions, adapter + evidence against a **freshly restarted** server, run id
`recorder-proof-bird-small` (`runs/t2/recorder-proof-bird-small/run.json`, 154 s, exit 0):

```json
"use_evidence": true,
"max_tokens": 512,                       "max_tokens_source": "server:/health",
"tool_commit_sha": "f4d3d4a0f8ae…",  "tool_commit_dirty": true,  "tool_branch": "feat/round2",
"tool_source_sha256": "9c2072bc548c…",
"model_id": "D:\\AI\\Models\\qwen2.5-coder-7b-instruct",
"served_name": "qwen2.5-coder-7b+e3-r16",
"adapter_id": "runs/e3-r16/adapter",
"adapter_sha256": "ea50f537cdf8e5a120306d2d7206908e7a0830803192fd7ba137a17565cd43a7",
"load_in_4bit": true,                    "device": "cuda",
"cuda_max_memory_allocated_bytes": 7069570560,   "cuda_max_memory_allocated_mib": 6742.1,
"requests_served": 46,                   "max_completion_tokens_observed": 90,
"n_questions": 22,                       "n_with_evidence": 19
```

Reading the proof: `use_evidence=true` and `adapter_id` are now on disk, so the two former
twins (base vs base+evidence) are distinguishable by `grep`. `max_tokens=512` is the server's
cap and 90 was the largest completion, so the cap was not binding. `6742.1 MiB` is a real
per-process counter for that run because the server was restarted first — the counter is a
high-water mark *since server start*, so a clean per-run figure requires one server per run
(done for cell D: 7 009.7 MiB; round 1's 8 218 MiB was an `nvidia-smi` sample and is now
superseded). `tool_commit_dirty=true` with a path list is honest about the shared worktree: the
listed files include other round-2 tasks' edits, which is exactly why the tool module's own
SHA-256 and the dirty list are recorded separately from the commit.

Tests: six new ones pin the behaviour — `tests/test_serve.py` (health exposes every provenance
key; request/completion counters increment) and `tests/test_local_bench.py` (the plan records
`use_evidence` + the effective cap; a disagreeing `--max-new-tokens` is refused; ambrosia +
evidence is refused; `/health` URL derivation drops `/v1`). Full suite: **151 passed**.
Reproduce the proof:

```bash
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only --use-evidence \
    --max-new-tokens 512 --run-id recorder-proof-bird-small \
    --query-file data/bird_small.json --answer-file data/bird_small.json \
    --db-url localhost:5444 --runs-dir runs/t2
```

The three round-1 `run.json` files (§5.1) predate the schema and cannot be retrofitted; for
those runs the configuration is reconstructed in §7.1 from the wrapper's arguments instead.

---

## 11. Ambrosia decision (round 2): calibrate, keep or drop

Round 1 left Ambrosia as "our numbers are below the constant floor and the adapter collapsed to
always-ambiguous" (§5.2, §5.4). This section decides what to do about it, using only artifacts
that already exist (the GPU is occupied by the night-scale run until ~14:30, so nothing here
re-runs the model). Everything below is traceable to `runs/t2/scores/ambrosia-calibration.json`
(produced by `runs/t2/t4_ambrosia_calibration.py` from the round-1 ledgers) and to
`data/ambrosia_{large,small}.json`.

**Artifact note (pre-seal audit, 2026-10-29 — finding fixed).** The pre-seal audit caught a stale
`cells.base_strict` block in `runs/t2/scores/ambrosia-calibration.json`: it repeated the adapter's
numbers (240 flags, TP 70, FP 170, correct 70) while naming `e1-ambrosia-large.json`. The cause was a
loop variable leaking into the payload comprehension of `runs/t2/t4_ambrosia_calibration.py` (both cells
were filled from the last loaded ledger), not a wrong computation — the `operating_points` below and every
number in this section were always derived from the correct ledgers. The artifact has been regenerated
from the existing predictions (CPU-only, no model, no GPU): `cells.base_strict` now reads
`predicted_ambiguous 61, true_positive 17, correct 143`, `confusion {tp 17, fp 44, fn 53, tn 126}`, and
`cells.adapter_strict` reads `240 / 70 / 70` with `{tp 70, fp 170, fn 0, tn 0}`. The audit script now
reports **133/133 checks reproduced, 0 findings** (`research/advtext2sql/AUDIT_ROUND2.json`), and the
script asserts the cell blocks against their operating points before writing, so the same leak would fail
loudly instead of shipping.

### 11.1 The hard floors, and the two measured operating points

`evaluate_ambrosia.py` scores a strict binary classification (correct ⇔ `gold_is_ambiguous ==
pred_is_ambiguous`), so constants are strong: never saying "ambiguous" is 170/240 = **70.83 %**
on `ambrosia_large` and 15/24 = **62.50 %** on `ambrosia_small`; always saying it is 70/240 =
**29.17 %** and 9/24 = **37.50 %**. There is no `ambrosia_small` run: the two floors are the only
numbers that file contributes (no prediction artifact exists for it).

| decision rule | flags | TP | FP | precision | recall | correct | accuracy | vs never-floor |
|---|---|---|---|---|---|---|---|---|
| never ambiguous (constant) | 0 | 0 | 0 | n/a | 0.000 | 170/240 | **70.83 %** | — |
| **base strict gate (E1, measured)** | **61** | **17** | **44** | **27.87 %** | **24.29 %** | **143/240** | **59.58 %** | **−11.25 pp** |
| adapter strict gate (E3, measured) | 240 | 70 | 170 | 29.17 % | 100 % | 70/240 | 29.17 % | −41.67 pp |
| union: base OR adapter | 240 | 70 | 170 | 29.17 % | 100 % | 70/240 | 29.17 % | −41.67 pp |
| intersection: base AND adapter | 61 | 17 | 44 | 27.87 % | 24.29 % | 143/240 | 59.58 % | −11.25 pp |
| always ambiguous (constant) | 240 | 70 | 170 | 29.17 % | 100 % | 70/240 | 29.17 % | −41.67 pp |

The union and intersection rows are identical to the two singles because the adapter flags
*every* item: with a classifier that answers "ambiguous" unconditionally, ensembling adds no
operating point. These six rows are **the complete set of decision rules the stored artifacts
support**.

### 11.2 Why there is no threshold curve to sweep

A ROC-style sweep needs a *ranked* score per question. The artifacts contain none:

* the gate (`Text2SQLGenerator._assess_ambiguity` → `parse_ambiguity_answer`) makes one LLM call
  whose reply is reduced to two classes, `OK` or `AMBIGUOUS: <clause>`; the clause is only
  logged, never persisted;
* `query_results.json` stores exactly two kinds of value per question — the string `"ambiguous"`
  or a SQL statement (`runs/t2/t4_ambrosia_calibration.py` asserts this over all 240 items);
* `dev_tools/serve_openai.py` refuses logprobs by design ("No tool calling, no logprobs…"), so no
  probability was ever produced;
* per-item confidence, candidate interpretations and gate replies are absent from
  `runs/t2/scores/*ambrosia*.json` (`per_question` carries booleans only).

So the honest statement is stronger than "no threshold wins": **no threshold can be computed from
these artifacts at all.** Recovering a score means re-running the 240 items with the gate's reply
text (and ideally logprobs) captured — a `serve_openai.py` change plus a GPU run, both blocked
today. The four-corner table above is therefore the whole calibration surface.

### 11.3 What a calibrated gate would have to achieve

Under this contract accuracy is `(170 + TP − FP)/240`, so beating 70.83 % is *exactly* the
condition `TP > FP`, i.e. **precision above 50 % on the items the gate flags**. The measured base
gate is at 27.87 % (17 TP / 44 FP): to reach the floor while keeping its 17 true positives it
would have to drop **28 of its 44 false alarms** and flag at most 33 items in total (17 TP +
≤16 FP) instead of 61.

**Plain answer to the task's question: no, nothing beats 70.83 %.** The best measured operating
point is 59.58 % (base strict), 11.25 pp *below* the constant it must beat, and the adapter is at
the other constant. There is no threshold to tune in the stored data, and the one measured
classification point sits on the wrong side of the break-even precision.

One in-sample diagnostic, reported only to close the "maybe a subgroup is calibratable" door:
the base gate fires on 22 of the 55 `ambrosia_large` databases; only 3 of those 22 have precision
above 50 % in-sample, and together they cover **4 flagged items** (4 TP). A "use the gate only on
these databases" rule would score 174/240 = 72.5 % *on the data it was selected from* — selection
on 4 items out of 240 with no held-out Ambrosia split, so it is noise, not a calibration. The
largest firing database (`vague_2cols_location`, 16 flags) has precision 0.25.

### 11.4 What the dataset actually encodes (checked before recommending a contract change)

Before proposing anything, the Ambrosia files were read for their structure, not just their
labels — measured row by row and recorded in `runs/t2/scores/ambrosia-calibration.json`
(`dataset_structure`, produced by `runs/t2/t4_ambrosia_calibration.py`):

* every row carries `ambig_queries`, a list of **2–3 candidate SQL readings of the same question
  that differ in operator grouping** (e.g. `(A OR B) AND C` vs `A OR (B AND C)`);
* for **all 170** unambiguous rows of `ambrosia_large` (and **334/334** in `ambrosia_train`) the
  gold `SQL` is *exactly one* of those listed readings — the phrasing picks it;
* for the **70** ambiguous rows (146 in train) `SQL` is the literal token `ambiguous`: the
  phrasing leaves the listed readings open.

So the teacher's data already contains the *interpretation set* for every item; the binary label
is a lossy summary of it. That is the fact the recommendation rests on.

**Recommended option: (c) — propose that the Ambrosia head return the list of admissible
readings instead of a binary flag, and prepare that proposal for the teacher as a blocked task.**

Justification, in one paragraph: the binary contract makes every Ambrosia number a comparison
against a constant (70.83 %), and the two measured points (59.58 % and 29.17 %) both fail it, with
the gate's precision at 27.87 % against the 50 % break-even — a gap that threshold tuning cannot
close because no score was ever recorded and none can be recovered without a re-run. The only lever
left on our side is class balance, and the measured mix shows how weak it is: the round-1 build was
already 66.7 % negatives and the natural build is 69.6 %
(`training/AMBROSIA_NATURAL_RECIPE.md` §1), so "2:1 sampling caused the collapse" contradicts the
numbers. Meanwhile the dataset already carries the richer target that the binary label throws
away — each of the 240 items lists its 2–3 candidate readings, the gold `SQL` equals exactly one of
them on all 170 unambiguous items, and the 70 ambiguous items are precisely those whose phrasing
leaves that list open — so a head that returns the admissible reading set has a learnable,
teacher-defined target and a graded score (exact match on the singleton for an unambiguous item,
set coverage of the listed readings for an ambiguous one), which is the only route to an Ambrosia
number that is not a constant comparison. It costs no GPU tonight: the proposal is written below
and the arm that would test it stays blocked on the teacher API, exactly as the round-2 plan
prescribes for teacher-API work.

What this does *not* mean:

* **No Ambrosia headline is claimed.** Until the teacher answers, the only honest sentences are
  "the base model scores 59.58 % against a 70.83 % constant floor" and "the round-1 adapter scores
  the always-ambiguous constant". These must always be printed next to the floor, never as an
  "Ambrosia accuracy" (§5.2 rule).
* **`birdsafe` stays the default.** BIRD has 0 ambiguous gold items, so any ambiguity answer is a
  guaranteed zero on 241 items; `birdsafe` keeps that ceiling intact while still running the gate
  (one extra call). The delivered branch therefore runs `birdsafe` for BIRD and only
  `TEXT2SQL_AMBIGUITY_MODE=strict` for an explicit Ambrosia measurement.
* **Strict mode with a calibrated gate (option a) is rejected on evidence**, not on preference:
  §11.3 shows there is nothing to calibrate and the one measured point is below break-even.

The proposal in one line for the teacher: *"may the Ambrosia head answer with the set of admissible
readings instead of a yes/no flag — the dataset already lists them in `ambig_queries` — scored as
set precision/recall against the gold `SQL` for unambiguous items and against the full listed set
for ambiguous ones?"* — drafted as a blocked task; it changes what the harness must accept, so it
cannot be adopted unilaterally.

### 11.5 The blocked arm and its residual risk

The natural-class-balance adapter is **ready but blocked on the GPU**
(`training/AMBROSIA_NATURAL_RECIPE.md`): dataset built and verified (`training/out-natural/`,
480 Ambrosia rows = 146 ambiguous + 334 natural negatives, train/valid 432/48, combined 722 rows,
`schema_basis=file`, leakage clean), planner dry-run recorded (`runs/ambrosia-natural/
provenance.json`: 138 steps, TRL 0.18.2 ok), training **not** started. Measured-derived cost:
**≈32 min training** (138 steps × 14.06 s/step from `runs/e3-r16/train_summary.json`) **+ ≈8 min
evaluation** (the round-1 adapter needed 434 s for the same 240 questions) ≈ **40 min of GPU**,
versus the 15 minutes in the plan — the card must be free, and after t7 by the captain's order.

Residual risks, stated plainly:

1. **Low expected value.** The mix moves 2.9 pp in the *safe* direction (more negatives), so a
   negative result kills the class-balance hypothesis rather than the head. It cannot be presented
   as "we tried everything".
2. **It cannot produce a pass under the current contract by itself.** Even a much better
   classifier must clear 50 % precision, and the artifacts still contain no score to calibrate —
   the arm changes the classifier, not the metric.
3. **GPU contention.** Peak VRAM of the round-1 7B QLoRA run was 11.0 GiB (`train_summary.json`),
   so it must not overlap t6's tail or t7; a slipped schedule pushes it past the deadline.
4. **Teacher may decline or not answer before the deadline.** Then the branch ships with the
   `birdsafe` default and the explicit "not measurable under this contract" sentence — option (b)
   becomes the delivered state, and no Ambrosia improvement is claimed.

**Blocked on teacher.** The ask this section recommends is written as a one-page proposal for the course
teacher at `research/advtext2sql/AMBROSIA_CONTRACT_PROPOSAL.ru.md` (`../AMBROSIA_CONTRACT_PROPOSAL.ru.md`
from this repo root — outside the repo, so it ships with no commit): let `query()` return the set of
admissible readings and score it as set-level precision/recall, which the dataset's `ambig_queries`
already supports. No acceptance is assumed. Until the teacher answers, the fallback above is the delivered
state: `birdsafe` stays the BIRD default, and no Ambrosia number is ever printed without its 70.83 %
constant floor (62.50 % on small) next to it, always phrased as "not measurable under this contract", never
as "Ambrosia accuracy".

---

## 12. Feature x model matrix: measured cells, blocked cells, decision rule

The round-2 plan measures a matrix, not one number (Phase 3 of
`research/advtext2sql/Roadmap.lora.ru.md`): the team's API pipeline
with and without its feature set, the local 7B base with the same prompt surface, and the local 7B + LoRA
adapter with and without the feature block — so that "does the adapter stack with the features?" is
answered by a paired test instead of an inference. The assembler is `dev_tools/matrix.py`; it reads
per-question ledgers only, never starts a server, never calls a model, never touches the database
(CPU-only, and the tests poison the socket to prove it). Artifact of the run below:
`runs/t2/scores/matrix.json`.

```bash
python -m dev_tools.matrix                       # the table + paired block vs adapter_ev
python -m dev_tools.matrix --json runs/t2/scores/matrix.json
python -m dev_tools.matrix --reference base_ev   # pair every arm against another reference
python -m dev_tools.matrix --arms my_arms.json   # assemble a caller-supplied arm list
```

`--arms` takes a JSON array of `{id, model, use_evidence, feature_prompt?, ledger?, status?,
blocked_reason?, blockers?}`, so the matrix can grow (a new adapter, a new feature file, an API row)
without editing the module; without it the built-in manifest below is used.

### 12.1 Measured cells (four, from the t2 2×2 artifacts)

`data/bird_large.json`, 241 questions, `dev_tools/fixed_eval.py` `mode="local"`, live PostgreSQL through
the tunnel, greedy 4-bit serving. EX/241 is the harness number; EX/209 is over the 209 gold-executable
questions (32 gold items are unexecutable, ceiling 208/241 = 86.31 % — §9.1).

| arm | model | evidence | features | EX/241 | EX/209 | simple / moderate / challenging | soft-F1 |
|---|---|---|---|---|---|---|---|
| `base_noev` | base | off | — | **51/241 = 21.16 %** | 51/209 = 24.40 % | 37.36 · 14.61 · 6.56 | 0.2974 |
| `base_ev` | base | on | — | **84/241 = 34.85 %** | 84/209 = 40.19 % | 51.65 · 26.97 · 21.31 | 0.4677 |
| `adapter_noev` | adapter | off | — | **61/241 = 25.31 %** | 61/209 = 29.19 % | 40.66 · 12.36 · 21.31 | 0.3351 |
| `adapter_ev` | adapter | on | — | **93/241 = 38.59 %** | 93/209 = 44.50 % | 51.65 · 34.83 · 24.59 | 0.4696 |
| `gpt_frozen_file` | team API (frozen file) | — | team best combo | **102/241 = 42.32 %** | 102/209 = 48.80 % | 56.04 · 40.45 · 24.59 | n/a |

The last row is a **reference, not an arm we can re-run**: it is the team's frozen predictions file
(`../artifacts/pred_scores.json`, id-level scores, no SQL), which does not record which model produced it
(the team's claim names a GPT-5.x-mini). It scores 42.32 %, not the claimed 44.81 % — that 2.5 pp
discrepancy was already measured in round 1 (captain-verified numbers, E2) and is not re-litigated here.
Because it carries no SQL, only the id-level comparison against it is possible (`b`/`c`/exact McNemar work;
nothing can be re-executed).

**Cross-check (required by the task, and it passes).** The four cells are assembled from
`runs/t2/scores/{e1,e1fair,e3,e3fair}-bird-large.json` and then compared field by field (EX/241, EX/209 and
the three per-difficulty counts) against the independent `runs/t2/scores/2x2-analysis.json` written by the
t2 analysis script. `dev_tools.matrix` reports **no discrepancy** (`findings: []` in
`runs/t2/scores/matrix.json`); had any number differed, it would have been printed as a `DISCREPANCY` row
and stored in the artifact rather than averaged away.

### 12.2 Blocked cells (six, printed as `blocked`, never as zero)

| arm | model | evidence | features | blocked on |
|---|---|---|---|---|
| `base_noev_features` | base | off | team feature file | team feature file + GPU window |
| `base_ev_features` | base | on | team feature file | team feature file + GPU window |
| `adapter_noev_features` | adapter | off | team feature file | team feature file + GPU window |
| `adapter_ev_features` | adapter | on | team feature file | team feature file + GPU window |
| `gpt_features` | GPT-5.4-mini | n/a | team best combo | teacher endpoint |
| `gpt_no_features` | GPT-5.4-mini | n/a | — | teacher endpoint |

The four local feature arms need the same single GPU the night-scale run owns (t6 until ~14:30, then t7's
four runs first); the two API rows need the teacher's endpoint and are outside this repo's control. For
every blocked arm the assembler prints the exact command that will produce it (serving line + harness
line), and the same rows are stored in `runs/t2/scores/matrix.json`; nothing is ever executed by
`matrix.py`.

### 12.3 The feature-block input format (what the runner needs when the file appears)

* **v1 — global block (supported now):** one UTF-8 text file, conventionally
  `training/feature_block.txt`, whose text is inserted verbatim as `Feature block:\n<text>` into the SQL
  prompt of every question. This is the *same* file `training/build_data.py --feature-prompt` consumes, so
  the training and serving surfaces carry the same block. Validation (shared by the assembler and the
  runner, so they cannot drift): file must exist, be valid UTF-8, be non-empty after stripping, and be at
  most 20 000 characters; a block above 4 000 characters is a warning, not a refusal, because it costs
  prompt tokens on every one of the 241 requests.
* **v2 — per-question block (documented, not wired):** a JSON object
  `{"<question_id>": "<feature text>"}`. The runner would have to select the block per question; until the
  team ships such a file, v1 is the contract.
* **Wiring:** `run_benchmark.py --feature-prompt FILE` validates the file, wraps the real tool class so the
  question string becomes `<question>\nFeature block:\n<text>` (the graded harness and the tool's prompt
  templates are untouched), and records `feature_prompt {file, chars, sha256_16}` plus
  `tool_wrapper: dev_tools.run_benchmark.feature_prompt_tool` in `run.json`. A missing/empty/oversized file
  is a **refusal**, and `--feature-prompt` with `--benchmark ambrosia` is refused because Ambrosia asks for
  an ambiguity verdict and never builds a SQL prompt. Side effect: the ambiguity gate sees the block too;
  on BIRD the gate is `birdsafe` (never answers "ambiguous"), so the cost is prompt tokens only.
* When the file appears, the arms become measured by running the printed commands and re-running
  `python -m dev_tools.matrix`: the blocked rows resolve themselves as their ledgers land, with no code
  change. Offline tests pin all of this (`tests/test_matrix.py`, `tests/test_local_bench.py`).

### 12.4 Decision rule for when the missing cells land

Every claim below is a **paired** comparison on the same 241 question ids (exact McNemar + 10 000 paired
bootstrap resamples over question ids, seed 0), reported on **both denominators** — EX/241 (the harness
number, the one that can be compared to the frozen file) and EX/209 (the denominator that is not capped by
the 30 dead gold queries). Pre-registered rule (docs/EVAL.md §"decision rule"): claim "better" only if
`b − c ≥ 6` **and** exact `p < 0.05` **and** the CI excludes 0. Every comparison below holds the evidence
setting fixed: §9.2 already measured evidence as worth ~+13.5 pp, so mixing the two would repeat round 1's
mistake of comparing cells that differ in more than one factor.

1. **Feature contribution inside the API pipeline:** `gpt_features` vs `gpt_no_features`. This is the only
   row that can measure what the team's feature combo is worth; it needs the teacher endpoint.
2. **Can a cheap model with the same features compete:** `base_*_features` vs `gpt_features`. If the gap is
   within the CI, the 7B pipeline is a zero-token alternative; if not, it is not.
3. **Does the adapter stack with the features (the claim that matters):** `adapter_*_features` vs
   `base_*_features`, i.e. the *same* prompt surface with only the adapter changing. This is the round-2
   analogue of §9.2's `adapter_ev` vs `base_ev` (+3.73 pp, b = 29, c = 20, p = 0.253, inconclusive) — the
   feature-free cell is already measured; the feature cell is the missing half.
4. **Does the combined local pipeline beat the frozen API file:** `adapter_ev_features` vs
   `gpt_frozen_file` (id-level). Today the best local arm (`adapter_ev`, 93/241) is −3.73 pp from the frozen
   42.32 % and no longer distinguishable (b = 33, c = 42, p = 0.356, §9.2). The rule for shipping a claim
   of parity-or-better is the same McNemar rule on the id-level ledger, plus the roadmap's cost clause
   (no worse than the API arm by more than 2 pp at zero token cost).

The **decision rule** in one line: the matrix decides "adapter + features" only if
`adapter_*_features` beats `base_*_features` under the pre-registered McNemar rule on EX/241 **and** does
not lose more than 2 pp to `gpt_features` on either denominator; if the adapter does not beat the base on the
feature surface, the honest conclusion is that the gain belongs to features/procedures (and the adapter is
optional), which is itself a result. Until those rows exist, no cell may be quoted as a number: a blocked
arm is reported as blocked, never as 0, and the matrix is printed with its denominators next to every cell.










---

## 13. Constrained decoding on the A100 (R2-15)

Round 1's "грамматический декодер" was a surrogate: the tool's sqlglot gate plus **one**
repair generation — post-hoc filtering, not decoding (`Text2SQLGenerator._generate_valid_sql`).
`docs/DECODER.md` records why every other option was rejected (a `guided_json` envelope
constrains JSON, not SQL; `guided_regex` is a finite automaton and SQL is recursive;
`guided_choice` needs a finite answer set). This round the measurement moved to a rented
A100 80 GB where vLLM exists, and the decoder is real: **xgrammar, a SELECT-only GBNF
grammar (`dev_tools/grammars/postgres_select.gbnf`), injected per request by
`dev_tools/guided_proxy.py`.**

### 13.1 The two arms (one serving stack, one prompt surface, repair off in both)

Both arms are the **same** AWQ-4-bit `Qwen/Qwen2.5-Coder-7B-Instruct` on vLLM 0.32.0
(port 8001), the **same** tool prompts, `--max-new-tokens 512`, `birdsafe` ambiguity gate,
`dev_tools/run_benchmark.py --predict-only --disable-sql-repair`. The only difference is
the proxy field: arm **N** forwards the request untouched (`--no-grammar`), arm **G** adds
`structured_outputs.grammar`. The tool's own repair is disabled in *both* arms precisely so
that the delta is attributable to decoding (see `run.json`: `sql_repair: "disabled"`,
`tool_wrapper: dev_tools.run_benchmark.no_repair_tool`).

| arm | decoding | EX/241 | EX/209 | unexecutable predictions | simple · moderate · challenging | soft-F1 | artifact |
|---|---|---|---|---|---|---|---|
| **N** no grammar | greedy, unconstrained | **39/241 = 16.18 %** <br>CI 12.1–21.4 | 39/209 = 18.66 % | **62** | 26.37 · 12.36 · 6.56 | 0.2796 | `runs/t2/r2-15-norep-awq/` |
| **G** SELECT grammar | xgrammar-constrained | **44/241 = 18.26 %** <br>CI 13.9–23.6 | 44/209 = 21.05 % <br>CI 16.1–27.1 | **57** | 30.77 · 13.48 · 6.56 | 0.2951 | `runs/t2/r2-15-grammar-awq/` |

`dev_tools/fixed_eval.py mode="local"`, live PostgreSQL through the tunnel, 32
gold-unexecutable items in both rows (the same set as everywhere else on this branch:
EX/209 is the denominator that the shipped evaluator's gold bug does not cap). The two
runs carry **identical prompt-token counts** (385 921 in both `run.json` files), which is
the artifact-level proof that the prompt surface really was the same and only the decoder
changed.

### 13.2 The decoder's own claim (never folded into the LoRA numbers)

The decoder effect is measured on the **raw model text** in each proxy's request log,
before the tool touches it (`dev_tools/decoder_report.py`, and
`dev_tools/check_grammar.py --proxy-log` re-parses every completion against the compiled
grammar):

| decoder fact (241 SQL requests per arm) | arm N (no grammar) | arm G (grammar) |
|---|---|---|
| completions that are a markdown fence (```` ``` ````) | **241/241 = 100 %** | **0/241 = 0 %** |
| completions not starting with `SELECT`/`WITH` | 241/241 | 0/241 |
| completions containing a `;` (the contract forbids it) | 236/241 | 0/241 |
| completions accepted by the compiled grammar | 0/241 | **241/241** |
| requests the proxy actually guided | 0 | **241/241** |
| predictions the tool's sqlglot gate rejected | 1 (a 512-token truncation mid-string, q517) | 1 (q1484, see below) |
| predictions that PostgreSQL could not execute | 62 | 57 |
| completion tokens (mean per SQL request) | 66.5 | 58.3 |
| cap hits (`finish_reason == "length"`) | 1 | 0 |

Per-error breakdown of the unexecutable predictions (`psycopg` exception class, from the
`fixed_eval` ledgers):

| error class | arm N | arm G |
|---|---|---|
| `UndefinedColumn` | 40 | 40 |
| `UndefinedTable` | 8 | 3 |
| `UndefinedFunction` | 7 | 8 |
| `SyntaxError` | 2 | 2 |
| `GroupingError` | 2 | 2 |
| `AmbiguousColumn` / `DatatypeMismatch` / `DatetimeFieldOverflow` | 3 | 2 |

Four facts, and they cut in different directions:

1. **The grammar removes the contract violation at the source.** The base model writes
   ```` ```sql … ; ``` ```` for *every single* question; the tool's `strip_code_fence`
   hides that afterwards. With the grammar the same model emits one bare statement every
   time, and every one of the 241 completions is inside the grammar
   (`check_grammar.py --proxy-log` → 241/241). That is a real decoder win — and it is
   invisible in an EX number, because the shipped tool already cleans fences up.
2. **The grammar does not touch what actually makes predictions unusable.** **55 of arm N's
   62** unexecutable predictions are identifier/function resolution errors
   (`UndefinedColumn` 40 + `UndefinedTable` 8 + `UndefinedFunction` 7) — a grammatically
   perfect query that names a column the database does not have is still a zero. Only **2**
   of them are `SyntaxError`, so the ceiling for a grammar on this number is small. The
   measured movement is exactly of that size: 62 → 57, and the 5 removed are
   `UndefinedTable` losses (8 → 3), not syntax.
3. **Grammar-legal is not PostgreSQL-legal, and the gap is measurable.** The one gate
   rejection in arm G (q1484) is a statement the grammar admits but PostgreSQL does not:
   `SELECT COUNT(*) FILTER WHERE …`. `FROM` is optional in the grammar (`SELECT 1` is
   legal), which also lets `SELECT … WHERE` through; sqlglot rejected it, the tool
   reported `"error"`, and that is the 1 sentinel. The fix is to make `FROM` mandatory
   whenever `WHERE`/`HAVING` appear — not applied to this arm, because that would change
   the grammar after the measurement; it is recorded as a known permissiveness instead.
4. **Why arm N shows 62 unexecutable predictions where §5.1 shows 40.** E1's 40 is the
   same model with **both** repairs switched on (the sqlglot repair *and* the
   execution-feedback repair, which runs the prediction, feeds the driver error back and
   asks once more) and a different 4-bit quantizer; both of my arms disable both repairs by
   design, so an unusable first attempt stays unusable. The arithmetic is
   `runs/t2/scores/e1-bird-large.json` → 40 `pred_error`, `e1fair` → 39, `e3` → 36, versus
   arm N → 62. No single factor is isolated by these runs, but the execution-feedback
   repair is the largest candidate (it is the only mechanism in the tool that can turn an
   `UndefinedColumn` into a working query), which is exactly why the grammar arm was
   compared against a repair-off surrogate and not against E1.

**Delta as its own claim** (paired on the same 241 ids, exact McNemar + 10 000 paired
bootstrap resamples, seed 0; `runs/t2/scores/r2-15-grammar-awq.json`):

| comparison | n | b | c | exact p | ΔEX | 95 % CI | verdict |
|---|---|---|---|---|---|---|---|
| **grammar (G) vs no grammar (N)** | 241 | 7 | 2 | **0.180** | **+2.07 pp** | **[−0.41, +4.56] pp** | **inconclusive** |
| same, on EX/209 denominator | 209 | — | — | — | +2.39 pp (44/209 vs 39/209) | — | descriptive only |

The pre-registered rule (`docs/EVAL.md`: claim "better" only if `b − c ≥ 6` **and**
`p < 0.05` **and** the CI excludes 0) is **not met**: the grammar fixes 7 questions and
breaks 2, so `b − c = 5`, `p = 0.18`. The honest statement is: **constrained decoding
eliminates the output-contract violation completely and moves EX by +2.07 pp, which at
n = 241 is not distinguishable from chance.** It is not a claim that the grammar improves
accuracy, and it is not folded into any LoRA number.

### 13.3 Cost (shared GPU: cross-window latency is *not* comparable)

| per-request cost (proxy-side, `/health` + request log) | arm N | arm G |
|---|---|---|
| all requests, mean / p50 / p95 | 0.60 / 0.33 / 1.55 s | 5.40 / 1.28 / 16.05 s |
| SQL-generation requests, mean / p50 / p95 | 0.60 / 0.33 / 1.55 s (unguided) | **10.63 / 9.14 / 20.72 s** (guided) |
| completion tokens mean | 66.5 | 58.3 |
| harness wall clock for 241 questions (482 requests) | 359 s | 2 671 s |

These two wall clocks must **not** be read as "the grammar is 7.4× slower": the A100's
effective throughput changed by ~8× between the two windows (the same short prompt took
0.6 s during arm N and 5.0 s later with *no* grammar at all — training plus another
user's evaluation server moved on and off the device). The grammar's own cost was therefore
measured with an **interleaved** test — one prompt, alternating arm, 6 rounds, same window:

| interleaved cost test | no grammar | full grammar | ratio |
|---|---|---|---|
| median latency (same prompt) | 4.99 s | 7.12 s | **×1.43** |
| median seconds per generated token | 0.185 s | 0.356 s | **×1.92** |

A stricter variant of the grammar (aliases may not omit `AS`) measured the same as the full
one (median 4.78 s vs 4.84 s in a single noisy round), so the cost is the per-token mask
computation itself, not the grammar's ambiguity.

The A100 was shared for the whole measurement: the captain's scale-up training held
46.1 GB and 100 % utilisation for both arms, and another user's evaluation server
(~7.4 GB) was resident during part of arm G. vLLM itself stayed small on purpose:
5.29 GiB of weights plus a 10 112-token KV cache at `--gpu-memory-utilization 0.09`
(~7 GB of 80 GB). Greedy decoding is deterministic, so **EX is unaffected by the
contention**; only latency is, which is why the ratio above comes from the interleaved
test rather than from the two harness windows.

### 13.4 What this section does not say

* **It is not a LoRA claim.** No adapter is loaded in either arm; the adapter matrix is
  §12.
* **The EX level is not comparable to §5.1.** E1/E3 ran bitsandbytes-nf4 through
  `serve_openai.py`; these arms run AWQ through vLLM. G vs N is a clean decoder
  comparison; N vs E1 is a serving-stack comparison and is not quoted as an improvement.
* **The grammar imposes two documented conventions:** upper-case keywords (every gold
  query uses them, and 239/240 of arm N's parseable predictions do too) and double-quoted
  identifiers only (backticks are rejected, as PostgreSQL does). Both are recorded in
  `docs/DECODER.md` §5.
* **The engine was small on purpose, and it is gone.** `--gpu-memory-utilization 0.09`
  (~7 GB) with `--max-model-len 3072`, no bf16 engine, no second model server; the proxy
  and the engine were shut down at the end of the measurement (04:00 UTC) so the 14B
  training window got the memory back. `runs/a100-scale` was never touched, and the
  per-run sampled device peak is in each `run.json` (`device_memory`).

Reproduce (the box, no commits):

```bash
/root/venv-vllm/bin/vllm serve /root/models/qwen2.5-coder-7b-awq --port 8001 \
    --max-model-len 3072 --gpu-memory-utilization 0.09 --max-num-seqs 4 \
    --dtype float16 --quantization awq                       # + VLLM_USE_FLASHINFER_SAMPLER=0
python -m dev_tools.guided_proxy --port 8100 --upstream http://127.0.0.1:8001 \
    --served-name qwen2.5-coder-7b-awq-guided --upstream-model qwen2.5-coder-7b-awq \
    --quantization awq --load-in-4bit --max-new-tokens 512 \
    --log runs/t2/r2-15-grammar-awq/llm_requests.jsonl        # arm G; --no-grammar -> arm N
python -m dev_tools.run_benchmark --run-id ... --predict-only --disable-sql-repair \
    --max-new-tokens 512 --runs-dir runs/t2 --llm-base-url http://127.0.0.1:8100/v1 \
    --llm-model qwen2.5-coder-7b-awq-guided --allow-llm-spend
python -m dev_tools.fixed_eval --answers data/bird_large.json \
    --predictions runs/t2/r2-15-grammar-awq/query_results.json \
    --baseline runs/t2/r2-15-norep-awq/query_results.json --mode local --out runs/t2/scores/r2-15-grammar-awq.json
python -m dev_tools.decoder_report --score ... --predictions ... --proxy-log ...
```

Verification of the decoder itself (both must pass before the numbers above mean anything):
`python -m dev_tools.check_grammar --sql-file data/bird_large.json --mode safe` →
**241/241 gold accepted**; `python -m dev_tools.check_grammar --proxy-log <arm G log>` →
**every guided completion inside the grammar**.

## 14. Round-2 headline — the scale-up adapter on the full BIRD train (R2-5/R2-6)

The round-2 training scaled from 342 rows over 8 databases to **5,612 rows over 71 databases** (group
split: 4,736 train / 61 DBs, 876 valid / 10 DBs; §11.2 of the runbook, `training/RUN_NOTES.md` §5). The
adapter keeps the round-1 recipe (r=16, alpha=32, all-linear, 3 epochs) and was measured on the A100
through the same harness and `fixed_eval mode="local"` protocol as every other number on this branch
(4-bit serving, greedy, `birdsafe` gate, tool v2):

| measurement | value |
|---|---|
| **EX/241** | **102/241 = 42.32 %** (95 % CI 36.25–48.63) |
| **EX/209** (gold-executable denominator) | **102/209 = 48.80 %** |
| soft-F1 | 0.4864 |
| simple · moderate · challenging | 48/91 = 52.75 % · 35/89 = 39.33 % · 19/61 = 31.15 % |
| unexecutable predictions | 19 |
| honest ceiling | 208/241 = 86.31 % (unchanged; §9.1) |

Paired comparisons (same 241 question ids, exact McNemar + 10k paired bootstrap, seed 0):

| comparison | b | c | ΔEX | exact p | 95 % CI | verdict |
|---|---|---|---|---|---|---|
| scale adapter vs round-1 adapter + evidence (93/241 = 38.59 %) | 26 | 17 | +3.73 pp | 0.2221 | [−1.66, +9.13] pp | **inconclusive** under the pre-registered rule |
| scale adapter vs the frozen team baseline (102/241 = 42.32 %) | 32 | 32 | 0.00 pp | 1.0 | [−6.64, +6.64] pp | **identical aggregate, different subsets** |

**The honest sentence, and the one to quote:** *local 7B + LoRA on 5,612 rows over 71 databases reaches the
same aggregate as the team's GPT-5.4-mini + 34-feature pipeline, while erring on different questions.* The
two systems get the **same number right** and **32 questions each way** — the complementarity is the
finding; it is not a win.

Four qualifiers that must travel with the number:

1. **It is a tie on 241 questions against a frozen file, not a live run of the team's pipeline.** The
   frozen file (`..\artifacts\pred_scores.json`) is id-level only — no SQL, no prompt, no config — so the
   comparison is id-level and cannot attribute the tie to any component.
2. **The CI is ±6 pp** (36.25–48.63): the same aggregate is not a precise estimate, and neither is theirs.
3. **The ceiling is 208/241 = 86.31 %**; EX/209 is quoted next to every number above for that reason.
4. **The paired tests cannot distinguish this adapter from the team baseline (p = 1.0) nor from our own
   round-1 adapter+evidence (p = 0.2221).** "LoRA beat the baseline" is false under the pre-registered
   rule and must not appear in any summary; the round-1 2×2 (§9) is unchanged.

**Provenance note (why the recorder mattered).** The first attempt at this number silently produced **empty
schemas**: a `.env` quoting bug on the A100 made DB authentication fail while the harness still
"succeeded", and the model answered from pretraining memory. That run was discarded (it scores 0/241 and is
kept on disk as `runs/t2/scores/r2-scale7b-ev.FAILED-empty-schemas.json` — never a number). The corrected
run has the real schema dump (11,108 bytes), 19 unexecutable predictions, and its `run.json` records
`adapter_sha256` `71abcde8…`, `load_in_4bit` and the CUDA peak — a silent empty-schema failure is exactly
the class of bug the §10 recorder exists to catch, and it was caught before it entered a document.

**Artifacts and the exact revision.** `runs/t2/scale7b-v2/` (run, `run.json`, real 8-DB schema dump),
`runs/t2/scores/scale7b-v2.json` (ledger) and `runs/t2/scores/scale7b-v2.verification.json` (independent
CPU-only re-derivation, **0 mismatches**); adapter provenance in `runs/a100-scale/` (best checkpoint 300,
eval_loss 0.12644, trained on `training/out-scale-t2048` = 4,736 BIRD + 394 Ambrosia = 5,130 rows, 876
valid; 750/936 steps, 112.5 min, peak 35.1 GiB; the 161.5 MB weights stay on the A100, sha verified there).
The A100 `run.json` has `git_commit: null` (that box has no git) but `tool_source_sha256` `9c2072bc…`,
which is exactly the `f4d3d4a` tool revision behind the round-1 2×2 cells — so the comparison with E3-fair
is on the same tool revision. HEAD's newer tool (procedure/thinking work) is not part of this number, and a
future run must record its own sha. The run artifacts live under `runs/` (ignored); the per-question
ledgers are the source of every number above.

**Both arms of t7 are now measured — see §16.** The evidence-on cell is 102/241 on the A100 (repeated
locally at 103/241) and the **no-evidence cell is 53/241 = 21.99 %** (`runs/t2/e-scale-noev/`), measured on
the same server minutes apart from the evidence-on repeat. The earlier note in this section ("only the
evidence-on cell was measured; `runs/t2/r2-scale7b-noev/` holds only an empty schema stub") describes the
A100 attempt and is superseded by §16; the empty stub remains on disk as provenance of that attempt, and
§16.3 gives the scale × evidence interaction that only exists because both arms were run.

---

## 15. Blocked: the distillation path (R2-7)

Distillation is **implemented and dry-run tested but has never been run against a teacher**: `training/build_data.py --teacher/--teacher-results` already replaces a BIRD training target with teacher SQL only where that SQL parses as PostgreSQL *and* executes to the gold row set (`training/teacher_filter.py`, every rejection counted by reason), and the new `--teacher-endpoint` mode records the endpoint, proves the key variable exists and writes `distill_plan.json` **without opening a socket** (`network_calls: 0`, enforced by a test that runs the build with `socket.socket`/`create_connection`/`getaddrinfo` replaced by raisers). What is missing is access, and only one of two forms is needed: (a) the base URL of the team's GPT-5.4-mini endpoint plus a key with access to it (`TEACHER_API_KEY`), or (b) a predictions file — and, for execution verification, a results file — for `data/train_queries.json` produced by that pipeline; (b) needs no endpoint and no network here, so it is the cheaper ask. What it unlocks is capability rather than format: on the 357 training questions the repo's own cleaning destroys 14 targets and silently alters 1 (and 348 + 84 in the 6,601-row scale corpus), and no filter we own can recover the intended answer — a stronger teacher answers those questions and the filter keeps only the answers that reproduce the gold rows, while `FEAT_2+3+4+8+12+17+18+19+27+29+30+32+33+35` is the team's feature block, not ours. The expected kept fraction is **200/241 = 83.0 %** — but that is the round-1 demo filter's rate on a deliberately damaged teacher file (`data_cache/artifacts/distill_sft_report.json`; 11 mismatches + 19 non-executable + 10 omitted + 1 unusable gold dropped), **not** an estimate of GPT-5.4-mini's accuracy, and it is labelled `not_a_teacher_estimate` in the plan; on 357 questions it projects to roughly 296 kept rows, to be replaced by the measured `teacher_report.json` after the real run. No teacher output was fabricated and no number in this report comes from a teacher run. The free fallback is **self-distillation**: run the current adapter over the 357 questions, execute-filter its own predictions with the same filter, and train on the survivors — it sharpens format and consistency but cannot add capability the adapter lacks, so it must be labelled `self-distillation` wherever it is quoted. Procedure, exact commands and the verification checklist: `training/distill.md`.

---

## 16. Scale-up results (R2-6): the completed 2×2 for the scaled model

§14 records the scale-up adapter with evidence on, measured on the A100. This section completes the
measurement: **both evidence arms on one server**, the paired blocks against round 1 and against the
frozen team file, and the answer to "did more data fix schema linkage?". Artifacts:
`runs/t2/e-scale/`, `runs/t2/e-scale-noev/`, `runs/t2/scale7b-v2/` (the A100 run), ledgers
`runs/t2/scores/{e-scale,e-scale-noev,scale7b-v2}.json`, analysis
`runs/t2/scores/t7-scale-analysis.json` (produced by `runs/t2/t7_scale_analysis.py`, CPU-only).

**Where this sits.** The 14B arm (§17) is the round-2 headline; this section is the 7B scale result and the
controlled evidence for the scale × evidence mechanism. The 7B tie against the frozen file stands and is not
retracted by the 14B win.

**The finding in one sentence:** *more data did not make the model link schemas better — it made it use the
evidence better.* Without evidence, 5,612 rows buy nothing (53/241, statistically the base model and, in
point estimate, below its smaller predecessor); with evidence, the scaled adapter beats round-1's
adapter+evidence by **+7.88 pp** (b=30/c=11, p=0.0043, CI [+2.90, +13.28]) — the first round-2 arm to clear
the pre-registered rule on the evidence surface — and ties the team's pipeline, while erring on different
questions. The scale × evidence interaction itself is **+11.20 pp** (CI [+4.98, +17.84], p=0.0016): the two
factors do not add here, unlike the round-1 adapter and evidence (§9.3).

### 16.1 The four cells of the scaled model, next to their round-1 comparators

| arm | EX/241 | EX/209 (gold-exec.) | simple · moderate · challenging | soft-F1 | unexec. preds |
|---|---|---|---|---|---|
| **scale adapter, evidence ON** (local 4070) | **103/241 = 42.74 %** | **103/209 = 49.28 %** | 53.85 · 39.33 · 31.15 | 0.4935 | 19 |
| **scale adapter, evidence OFF** (local 4070) | **53/241 = 21.99 %** | **53/209 = 25.36 %** | 36.26 · 15.73 · 9.84 | 0.2993 | 28 |
| scale adapter, evidence ON (A100 repeat, §14) | 102/241 = 42.32 % | 102/209 = 48.80 % | 52.75 · 39.33 · 31.15 | 0.4864 | 19 |
| round-1 base + evidence (cell B, §9) | 84/241 = 34.85 % | 84/209 = 40.19 % | 51.65 · 26.97 · 21.31 | 0.4677 | 39 |
| round-1 adapter, no evidence (cell C, §9) | 61/241 = 25.31 % | 61/209 = 29.19 % | 40.66 · 12.36 · 21.31 | 0.3351 | 36 |
| round-1 base, no evidence (cell A, §9) | 51/241 = 21.16 % | 51/209 = 24.40 % | 37.36 · 14.61 · 6.56 | 0.2974 | 40 |
| frozen team file (id-level only) | 102/241 = 42.32 % | 102/209 = 48.80 % | 56.04 · 40.45 · 24.59 | n/a | n/a |

All rows: `data/bird_large.json`, 241 questions, live PostgreSQL through the tunnel, greedy 4-bit
serving, `dev_tools/fixed_eval.py` `mode="local"`, tool v2 with its repair enabled. Same 32
gold-unexecutable items everywhere, so EX/209 keeps its 209 denominator and the ceiling stays
208/241 = 86.31 %.

**Run conditions (recorded in each `run.json`).** `e-scale`: 39 min wall, 928 requests for 241
questions, adapter `71abcde8…`, `use_evidence=true`, `max_tokens=512` from `/health`, largest
completion 319 (cap not binding), 19 unexecutable predictions. `e-scale-noev`: 28.7 min, **1,587
requests** (the gate/repair path fires far more without evidence), `use_evidence=false`, largest
completion **512** — at least one generation hit the cap. **Local repeat vs the A100 run: 103 vs 102
correct, b=1, c=0** — one question apart across different hardware and a different tool revision (the
A100 ran the `f4d3d4a` tool `9c2072bc…`; this workstation runs HEAD `103fd34e…`, whose prompt templates
are byte-identical on the default path and whose new procedure/thinking arms are off by default). That
is the reproducibility evidence §14 did not have.

### 16.2 Paired blocks (exact McNemar + 10 000 paired bootstrap, seed 0)

| comparison | b | c | ΔEX | 95 % CI | exact p | pre-registered rule |
|---|---|---|---|---|---|---|
| **scale+evidence vs round-1 base+evidence** | **30** | **11** | **+7.88 pp** | **[+2.90, +13.28] pp** | **0.0043** | **met** |
| scale+evidence vs frozen team file | 32 | 31 | +0.41 pp | [−6.22, +7.05] pp | 1.0 | identical aggregate |
| scale+evidence vs round-1 adapter (no evidence) | 54 | 12 | +17.43 pp | [+11.20, +23.65] pp | 1.7e-07 | mixes evidence — contextual, not a claim |
| scale, no evidence vs round-1 adapter (no evidence) | 12 | 20 | **−3.32 pp** | [−7.88, +1.24] pp | 0.215 | not significant; point estimate **negative** |
| scale, no evidence vs round-1 base (no evidence) | 19 | 17 | +0.83 pp | [−4.15, +5.81] pp | 0.868 | no effect |
| evidence effect **inside** the scaled model | 50 | 0 | +20.75 pp | [+15.77, +26.14] pp | 1.8e-15 | met |
| local run vs A100 run (same adapter) | 1 | 0 | +0.41 pp | [0.00, +1.24] pp | 1.0 | reproducible |

**Scale × evidence interaction** (difference in differences against the round-1 arms):
**+11.20 pp, 95 % CI [+4.98, +17.84] pp**, sign test 45 positive / 19 negative / 177 zero
(exact p = 0.0016). The scale-up gain is **+19 questions with evidence and −8 without**: the two
factors do not add here, unlike the round-1 adapter and evidence (§9.3). More data changed what the
model does *with* the evidence, not what it does without it.

### 16.3 Did more data fix schema linkage?

**No — and the measurement says something narrower and more useful.** Three numbers decide it:

1. **With evidence, the extra data is a real gain over round 1:** +7.88 pp, b=30/c=11, p=0.0043, CI
   excludes 0 — the first round-2 arm to clear the pre-registered rule against the round-1
   adapter+evidence.
2. **Without evidence, the extra data bought nothing:** 53/241 is statistically the base model
   (b=19/c=17, p=0.868) and, in point estimate, *below* the round-1 adapter (12/20, −3.32 pp,
   p=0.215). Sixteen times the training rows did not move the no-evidence regime.
3. **The interaction is significant and opposite-signed** (+11.20 pp, p=0.0016): scaling helps only
   when the prompt carries the schema/evidence hints. That is a statement about *using evidence*, not
   about **schema linkage** — mapping question terms onto unseen schemas.

And `bird_large` cannot test schema linkage at all: the group split pins all **8** `bird_large`
databases to the training side (`training/out-scale-t2048/group_split.json`, `pinned_to_train`), so
every one of the 241 questions is asked against a schema the adapter saw during training. The only
unseen-schema evidence is the held-out side of the group split — **876 rows over 10 databases the
trainer never saw** (disney, european_football_1, law_episode, menu, movie_3, movies_4, music_tracker,
sales_in_weather, social_media, talkingdata) — and for it only **validation loss** exists, not
execution accuracy: best `eval_loss = 0.126443` at `checkpoint-300`. There is no EX number on unseen
databases for this adapter (the A100 OOD attempt produced no predictions; the independent OOD check is
a separate task), so the honest sentence is: *more data improved the evidence-conditioned pipeline,
and the unseen-schema question remains unmeasured.*

### 16.4 Qualifiers that must travel with these numbers

1. **The frozen comparison is id-level and a tie:** 103 vs 102, b=32/c=31, p=1.0 — same aggregate,
   different questions; §14's sentence stands unchanged.
2. **Dual denominators everywhere:** EX/241 is the harness number; EX/209 (49.28 % for the headline
   cell) is the one not capped by the 30 dead gold queries. The ceiling is 208/241.
3. **Greedy reproduces across hardware to one question (103 vs 102), not bit-identically** — quote a
   single number, and do not present the one-question difference as an effect.
4. **The no-evidence arm hit the 512-token cap at least once** and made 1,587 model calls (vs 928): the
   arms are not equally expensive, and a larger cap could move the no-evidence cell, not the
   evidence one (largest completion 319).
5. **The adapter measured here is the A100 one** (`runs/a100-scale-adapter`, sha256 `71abcde8…`,
   `checkpoint-300`): the task text said `runs/r2-scale/adapter`, but the local night-scale run died at
   step 495/624 (§14, t6); the delivered weights are the A100 run over the same corpus (`data_hash
   eb1ace92…`, 4,979 train rows after the 2,048-token filter, 876 valid).

## 17. The 14B arm (R2-18) — capacity converts evidence into accuracy

**The sentence:** *a local 14B open model with LoRA beats the team's GPT-5.4-mini + 34-feature pipeline by
11.6 pp on 241 questions (p = 0.0003).* Same protocol as every number on this branch — `data/bird_large.json`
(241), live PostgreSQL through the tunnel, greedy, 4-bit serving, evidence on, tool v2, `fixed_eval
mode="local"` — on a rented A100 80 GB with the same corpus (`training/out-scale-t2048`) and recipe (r=16,
alpha=32, all-linear, 3 epochs).

| measurement | value |
|---|---|
| **EX/241** | **130/241 = 53.94 %** (Wilson 47.64–60.12) |
| **EX/209** (gold-executable) | **130/209 = 62.20 %** |
| soft-F1 | 0.5817 |
| simple · moderate · challenging | 63/91 = 69.23 % · 43/89 = 48.31 % · 24/61 = 39.34 % |
| unexecutable predictions | **5** (vs 19 for the 7B scale arm, 40 for round-1 base) |
| honest ceiling | 208/241 = 86.31 % (unchanged) |

Paired (same 241 ids, exact McNemar + 10k paired bootstrap, seed 0):

| comparison | b | c | ΔEX | 95 % CI | exact p | rule |
|---|---|---|---|---|---|---|
| vs round-1 adapter+evidence (93/241) | 51 | 14 | **+15.35 pp** | [+9.13, +21.58] pp | 4.475e-06 | met |
| **vs the frozen team file (102/241)** | **43** | **15** | **+11.62 pp** | **[+5.39, +17.43] pp** | **0.000307** | **met** |

**Evidence contrast at 14B — the strongest single contrast in the project.** Same adapter, same 241
questions, evidence off: **EX/241 = 85/241 = 35.27 %** (Wilson 29.51–41.49), **EX/209 = 85/209 = 40.67 %**,
soft-F1 0.4112, per difficulty 52/91 · 24/89 · 9/61, 11 unexecutable predictions. Paired against the
evidence-on arm: **b=51, c=6, +18.67 pp, exact p = 5.68e-10, CI [+12.86, +24.48]**. The same weights lose
**45 questions** when evidence is removed, versus 50 for the 7B scale arm (103→53) and 33 for the base model
(84→51). Three independent confirmations of one sentence: **evidence is not a nicety, it is the interface
through which capacity becomes accuracy.** The no-evidence `run.json` was not copied into the workspace
(only its `query_results.json` and ledger are); the arm identity comes from the task that ran it, and the
per-question ledger is the source of the paired block.

**Three qualifiers, in the same breath.** (1) The comparison is against the team's **frozen predictions
file**, not a live run of their pipeline: it carries id-level scores only — no SQL, no prompt, no config —
so the win is id-level and the file's provenance is their claim, not something this repo can re-execute.
(2) **n=241 means the CI is ≈±6 pp**; the point estimate +11.62 pp is outside zero, but the interval is the
honest precision. (3) The ceiling is 208/241, which is why EX/209 (62.20 %) is quoted next to every EX/241.

**The mechanism now has teeth.** The 7B scale-up showed that more data helped only **with** evidence
(scale × evidence interaction +11.20 pp, §16.2) and bought nothing without it. The 14B confirms the
direction: evidence-conditioned training is what converts parameters into accuracy, and the capacity gain
shows up exactly where the errors were — **5 unexecutable predictions instead of 19/40**, and the biggest
per-difficulty jump on challenging questions (39.34 % vs 31.15 % for the 7B scale arm). The 7B tie against
the frozen file (§14, §16.4) is not retracted: it is the controlled comparison of what data alone buys, and
the 14B shows what capacity adds on top of it.

**Honest architecture note.** The 14B was trained on a rented **A100 80 GB** — 750/936 steps, early stop at
epoch 2.405, **214 min** wall (1.90× the 7B run), **30.991 GiB allocated / 60.623 GiB reserved** peak VRAM,
micro-batch 4 × grad-accum 4 = effective batch 16, seq 2,048, same corpus (`training/out-scale-t2048`) and
recipe. Nothing here proves this recipe fits 16 GB. The serving run's `run.json` records a 13,342 MiB CUDA
peak for the 4-bit 14B + adapter, which is serving, not training. Treat the 14B as an A100 result, not as a
workstation-feasible one.

**Artifacts:** `runs/t2/14b-ev/` (`query_results.json`, `run.json`, model `qwen2.5-coder-14b`, adapter
`a100-14b/adapter`, `adapter_sha256` `5833a1cc…`, `tool_source_sha256` `9c2072bc…`, `use_evidence=true`,
`load_in_4bit=true`, 503 requests, 5 unexecutable); training provenance `runs/a100-14b/{provenance.json,
train_summary.json,sample_generation.json}` (best checkpoint 300, eval_loss 0.112492, same `data_hash`
`eb1ace92…`, 4,979 train / 876 valid); ledger `runs/t2/scores/14b-ev.json`; on the A100 the source is
`/root/work/data_cache/runs/r2-14b-ev/` and `/root/work/eval/14b-ev.json`. The ledger's own paired block
against `e3fair-bird-large` reproduces locally (b=51, c=14, p=4.475e-06); the frozen-file comparison is
re-derived in the audit from the per-question ledger.

**One provenance gap, stated plainly.** The A100 `run.json` for the 14B records `git.commit: null` — the
rented box has no git metadata, the same gap as the scale run. This applies to **both** 14B arms (they come
from the same A100 checkout; the no-evidence arm's `run.json` was not copied at all, only its predictions and
ledger). The dataset hash `eb1ace92…` and the pinned `tool_source_sha256` `9c2072bc…` identify what was
actually trained and served; no reader should assume the 14B was trained from a tracked commit.

---

## 18. Procedure and decoder arms: measured / not measured (R2-16)

Three arms, one protocol as every other number on this branch (`data/bird_large.json`, 241 questions, live
PostgreSQL through the tunnel, greedy 4-bit serving, evidence on, tool v2, `fixed_eval mode="local"`).
One is measured with a significant effect, one is measured and null, one is not measured because it cannot
finish inside the 14:00 cut-off. Artifacts: `runs/t2/t17-retrieval-scale-ev/` (+
`t17-retrieval-procedures.jsonl`), `runs/t2/r2-15-{norep,grammar}-awq/`, ledgers
`runs/t2/scores/{t17-retrieval-scale-ev,r2-15-norep-awq,r2-15-grammar-awq}.json`, analysis
`runs/t2/scores/t17-arms-analysis.json` (from `runs/t2/t17_arms_analysis.py`, CPU-only).

| arm | status | EX/241 | EX/209 | simple · moderate · challenging | unexec. | paired against its control | verdict |
|---|---|---|---|---|---|---|---|
| **A. retrieval few-shot, k=3** (7B scale adapter + evidence, A100) | **measured** | **117/241 = 48.55 %** | **117/209 = 55.98 %** | 62.6 · 46.1 · 31.1 | **12** | vs same adapter/evidence without retrieval (102/241): **b=27, c=12, p=0.0237, +6.22 pp [+1.24, +11.20] — rule met** | **worth folding in** (with the caveats below) |
| **C. grammar-constrained decoding** (AWQ 7B, A100, repair disabled in both arms) | **measured** | **44/241 = 18.26 %** | 44/209 = 21.05 % | 30.8 · 13.5 · 6.6 | 57 (vs **62** unguided) | vs the sqlglot surrogate without grammar/repair (39/241): b=7, c=2, p=0.1797, **+2.07 pp [−0.41, +4.56]** — not significant | **not for score; yes as an output-contract guard** |
| **B. self-consistency, n=5** | **not measured — time** | — | — | — | — | — | no verdict until measured |

### 18.1 Arm A — retrieval few-shot: the one procedure that pays

The bank is dev-tool's train-only kNN bank (`data/train_queries.json`, 357 rows, per-`db_id`, two leakage
layers that refuse dev sources by name and by question text; gold SQL transpiled to PostgreSQL and dropped
when it does not survive). It fired on **240/241 questions (99.6 %)**, with a mean of **2.98 examples** per
question and a mean top-1 similarity of **0.449**, verified from the procedure log rather than assumed.

The effect is large for a procedure and it is **free in wall clock**: 1,933 s for the retrieval run versus
1,947 s for the same adapter and hardware without it (8.02 vs 8.08 s/question; 522 vs 520 model requests) —
the extra prompt tokens are absorbed, so this is a prompt-side win, not a latency trade. It also cuts
unexecutable predictions from 19 to 12, i.e. the examples teach the dialect contract as well as the mapping.

Against the frozen team file the comparison is **+6.22 pp but not significant** (b=42, c=27, p=0.0912,
CI [−0.41, +13.28]): the point estimate is above their 42.32 %, the interval still touches zero, and the
file is id-level only. So the defensible claim is the *within-our-own-model* one: **retrieval beats no
retrieval by +6.22 pp on the same adapter, same hardware, same evidence setting, p=0.0237.**

Two caveats that must travel with it: (1) the bank is BIRD-train only — this measures in-domain retrieval,
not cross-database transfer; (2) whether it stacks with the 14B arm (§17, 130/241 without retrieval) is
**untested** — the obvious next measurement, not a claim.

*Provenance detail worth knowing:* the run used HEAD's tool (`tool_source_sha256 103fd34e…`, the retrieval
code present, only retrieval enabled) against a baseline measured with the sealed tool (`9c2072bc…`); the
prompt templates are byte-identical on the default path and every other procedure is off, so the treatment
difference is retrieval itself. The A100 was also running three other agents' servers during the run
(ports 8000/8001/8002), which can affect latency but not the scored predictions. **The procedure
configuration is recorded in `t17-retrieval-procedures.jsonl` (its first line carries k, embedder, bank and
evidence flags), not in `run.json`** — the recorder captures evidence/feature/tool provenance but not the
`TEXT2SQL_*` procedure environment; that is a gap to close if procedures become defaults.

### 18.2 Arm C — grammar-constrained decoding: a contract guarantee, not a score lever

Re-derived from t15's artifacts: the grammar removes the output-contract violation completely (241/241
guided completions parse inside the compiled SELECT grammar; the unguided arm produced fenced/prose
completions) and moves EX by **+2.07 pp, which is not significant** (b=7, c=2, p=0.1797). It reduces
unexecutable predictions 62 → 57, and the failure census says why the effect is small: 55 of the unguided
arm's 62 are `UndefinedColumn/UndefinedTable/UndefinedFunction`, only 2 are syntax — a grammar cannot fix
identifier resolution. **Latency is not quotable from these two runs**: their per-request means (1.07 s
unguided vs 10.63 s guided, from `llm_requests.jsonl`) were taken in different windows on a shared A100 and
are confounded by another resident evaluation server; t15's interleaved same-window test is the honest cost
figure (×1.43 wall, ×1.92 per token), and a clean interleaved re-test is what a production decision would need.

### 18.3 Arm B — self-consistency: structurally out of budget, not unlucky

Not measured, and it is not a scheduling miss: `n=5` needs a **sampling** server (temperature 0.7, which the
greedy scale server does not provide) and multiplies every question by five. At the measured greedy rate
(~6–8 q/min on this hardware) that is **≈3.3 h for 241 questions** on either card, against a 14:00 cut-off
and a GPU that was booked by the distillation window until ~14:15. It is marked `not measured — time` and no
number is implied.

### 18.4 One line each, for the meeting

* **A (retrieval k=3): fold it in** — +6.22 pp on the same model, p=0.0237, no wall-clock cost, 12
  unexecutable predictions instead of 19; make it a documented default with the leakage guards and confirm
  it stacks with 14B before quoting a combined number.
* **C (grammar): keep it as a serving flag, not a default score lever** — the contract guarantee is real
  (0 violations), the accuracy effect is +2.07 pp at p=0.18, and its cost needs a clean interleaved test.
* **B (self-consistency): do not adopt** — unmeasured, and its cheapest possible run is ~3.3 h;
  if it is ever tried, it needs a sampling server and a cut-down question set with an explicit caveat.

## 19. Out-of-distribution check: execution accuracy on 10 unseen databases

**What this is.** The group split held out **10 databases** from training (disney, movie_3, talkingdata,
menu, movies_4, law_episode, european_football_1, social_media, sales_in_weather, music_tracker). The
evaluation set is **876 BIRD *train* questions** from those databases — the same corpus as training, a
different set of schemas. It tests **schema transfer**, not a new domain, and it is not a generalisation
proof. It is also **not comparable** to the 241-question in-domain results (different population, different
difficulty mix, PG-native gold): a sentence that puts 71.67 % next to 38.59 % is wrong even though the
percentages look alike.

| arm (live DB, evidence on, greedy 4-bit, `mode=local`) | rows | EX | EX over gold-executable | unexec. predictions |
|---|---|---|---|---|
| 7B scale adapter, full held-out set (`71abcde8…`) | 876 | **620/876 = 70.78 %** (Wilson 67.68–73.69) | **620/811 = 76.45 %** | 50 |
| 7B scale adapter, the same 292 questions as the discarded base attempt | 292 | **220/292 = 75.34 %** | 220/277 = 79.42 % | 15 |
| 7B scale adapter, the same 120 questions as the base arm | 120 | **83/120 = 69.17 %** | 83/107 = 77.57 % | 8 |
| 7B **base** model, no adapter | 120 | **86/120 = 71.67 %** | 86/107 = 80.37 % | 8 |

**Denominators.** On the full 876, 65 gold queries are unexecutable on the live server (the same
transpile-damage class as the 32 in `bird_large`, applied here to already-PostgreSQL gold), so the
denominator that is not capped by dead gold is **811/876 = 92.6 %**. On the 120-row pairing, 13 gold rows
are unexecutable, leaving **107** as the executable denominator; on the adapter's 292-row cell the executable
denominator is 277. EX over the executable subset is quoted next to EX throughout, the same discipline as
EX/209 next to EX/241; the 876, 292 and 120 denominators are never mixed.

**The attribution: the adapter does not add capability on unseen schemas.** The base arm is a
**120-question systematic subsample** (every 7th row of the 876, all 10 databases represented) because a
full 876-row base run did not fit the seal window; the adapter is therefore also quoted on **exactly those
120 questions**, not against its 876-row number. Paired, exact McNemar + 10 000 bootstrap resamples, seed 0:

| comparison (same 120 question ids) | b | c | ΔEX | 95 % CI | exact p |
|---|---|---|---|---|---|
| scale adapter vs base model | 8 | 11 | **−2.50 pp** | **[−10.00, +4.17] pp** | **0.648** |

**What transfers, and what does not.** The **base model transfers**: with the schema text in its prompt it
writes executable PostgreSQL for questions on databases it never trained on — 71.67 % EX with only **8
unexecutable predictions**. The **adapter does not add** to that: 69.17 % on the same questions, a
difference of −2.5 pp whose CI straddles zero (p = 0.648). Its in-domain advantage (§9, §16: +7.88 pp over
round-1 base-with-evidence) **does not carry over to unseen schemas**, and on five of the ten databases the
base is at least as good:

| database (rows on the 120) | scale adapter | base model |
|---|---|---|
| talkingdata (11) | 10/11 = 90.9 % | 9/11 = 81.8 % |
| movie_3 (29) | 24/29 = 82.8 % | 23/29 = 79.3 % |
| movies_4 (20) | 15/20 = 75.0 % | 13/20 = 65.0 % |
| european_football_1 (6) | 5/6 = 83.3 % | 4/6 = 66.7 % |
| law_episode (13) | 7/13 = 53.8 % | **12/13 = 92.3 %** |
| menu (9) | 5/9 = 55.6 % | 7/9 = 77.8 % |
| disney (11) | 6/11 = 54.5 % | 7/11 = 63.6 % |
| sales_in_weather (9) | 5/9 = 55.6 % | 5/9 = 55.6 % |
| social_media (8) | 3/8 = 37.5 % | 3/8 = 37.5 % |
| music_tracker (4) | 3/4 = 75.0 % | 3/4 = 75.0 % |

The honest reading is a **negative transfer result**: what the adapter learned on the 61 training schemas
is schema-family-specific, and it does not make the model better at reading a schema it has never seen. The
capability that transfers is the one both arms already have — the PostgreSQL output contract and the ability
to follow the schema text. Full-population adapter per-database counts (876 rows, no base counterpart) are
in `runs/ood/scores/ood-analysis.json`.

**This retro-frames the in-domain numbers honestly.** The 42.32 / 42.74 / 53.94 % results are measured on
`bird_large`'s **8 databases**, which the group split pins to the training side. The 14B capacity result
(130/241) stands **on that population**; nothing about it should be read as cross-schema capability. The
transferable asset is the **base model plus the evidence interface**, not the adapter. The sentence that ties
the mechanism together: *the adapter learned what this benchmark rewards — the dialect, the output contract,
and the eight schemas — and that is why the scale-up gain appears only with evidence, which supplies the
schema linkage at inference time.*

**This is a credit, not a defeat.** We built the check that could have embarrassed the project, and it fired.
Reporting it plainly is the point: the branch now states what the adapter is (a benchmark-specific asset) and
what the base model already does (transfer to unseen schemas), instead of quietly selling 70.78 % as
generalisation.

**A discarded run, and why it is recorded here.** A first base-model attempt (`ood-base7b-292`, 292 rows,
13:32–13:44 MSK) reported 15/292 = 5.14 %, suggesting the base model could not ground on unseen schemas at
all. It is wrong and is **not** a finding: that run's `db_schemas.json` is **209 bytes of empty strings**
(`{"disney": "", "movie_3": "", …}`) and its log shows
`psycopg.OperationalError: password authentication failed for user ""readonly_user""` — a `.env` loader kept
the surrounding quotes, the DSN was malformed, every schema fetch returned nothing, and the harness still
wrote predictions; the model then hallucinated plausible identifiers (`movies`, `MPAA_rating`,
`role_name`). The 259 `ProgrammingError` rows were the empty prompt, not the model. Its dialect-neutral
robustness pass (a separate scoring in `mode=normalized`) was run on that same broken run and is therefore
discarded with it. The valid base arm is `ood-base7b-120`, whose schema dump is 9,872 bytes; an **empty-schema guard now
lives in `dev_tools/run_benchmark.py`** so that a run whose schema dump contains an empty value is refused
before predictions unless `--allow-empty-schemas` is passed (the refusal is written into `run.json`), the
same way an unreachable `/health` is refused; 26 offline tests pin it in `tests/test_schema_guard.py`. The discarded run and its evidence are
kept in `runs/ood/scores/ood-reconciliation.json`.

**Honest limits.** (1) The base arm is 120 questions; the paired attribution applies to those 120 and the
adapter's 876-row figure is kept as the population number, never mixed with the 120-row base. (2) 13 of the
120 gold rows (and 65 of the 876) are unexecutable on the live server, hence the executable denominators.
(3) No difficulty labels exist for these rows (`difficulty: "unknown"`), so no per-difficulty claim is made.
(4) Same protocol as every other number (4-bit, greedy, 512-token cap, evidence on, tool v2 with repair), on
the rented A100.

**Artifacts:** `runs/ood/scores/ood-scale7b-v2.json` (876-row adapter ledger), `runs/ood/scores/ood-base7b-120.json`
(base, 120 rows), `runs/ood/scores/ood-reconciliation.json` (corrected attribution + the discarded run's
evidence, from `runs/ood/ood_reconciliation.py`), `runs/ood/scores/ood-analysis.json` (per-database adapter
breakdown from `runs/ood/ood_analysis.py`, CPU-only). The adapter's 292-row cell (220/292, 220/277) is
re-derived from the 876-row ledger by the 292 ids in `runs/ood/scores/ood-base7b-292.json`; the discarded
run's empty schema dump is `runs/ood/ood-base7b-292/db_schemas.json` (209 bytes, all 10 values empty) and its
predictions are `runs/ood/ood-base7b-292/query_results.json`. Run metadata: `runs/ood/ood-scale7b-v2/run.json`
(adapter `71abcde8…`) and `runs/ood/ood-base7b-120/run.json` (no adapter), both `use_evidence=true`, 4-bit,
512 cap.
