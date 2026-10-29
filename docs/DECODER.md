# Constrained decoding: what was rejected, what was built, what it bought

Status: **measured on the rented A100 80 GB (2026-10-29)**. This file is the record
of the "грамматический декодер" item; the numbers live in
[REPORT.md §13](REPORT.md) and every one of them is reproducible from the artifacts
named there.

The item exists because round 1 shipped a *claim* of constrained decoding that was
not decoding. This file says what was actually rejected, why, and what replaced it.

---

## 1. The round-1 surrogate, and why it is not a decoder

What the branch had was `sql_utils.validate_sql` (a sqlglot gate) plus **one**
repair generation in `Text2SQLGenerator._generate_valid_sql`. That is
*post-hoc filtering with one retry*:

* the invalid text has already been generated — the tokens were spent;
* the gate is binary, so `invalid` costs a whole second call;
* the tool answers `status="error"` when the repair also fails, and the harness
  records the string `"error"` as the prediction. On `bird_large` with the base
  model that happened **40 times out of 241** (16.6 %, [REPORT.md §5.1](REPORT.md)).

A constrained decoder is a different object: it changes the **probability of the
next token** so that a non-SELECT continuation has probability zero. No token is
spent on invalid text, no second call is needed, and "the model wrote prose" stops
being an event that can happen.

The round-1 blocker was environmental, not conceptual: vLLM is Linux-only, the
workstation is Windows with no Docker, and `llama.cpp`'s GBNF server was not
available there either. `dev_tools/serve_openai.py` (transformers + a 4-bit load)
implements the OpenAI surface the harness needs but has **no** logits processor.
That is why this round moved the measurement to a Linux box with a real GPU.

## 2. Options considered and rejected

| option | why it was rejected |
|---|---|
| **Keep the gate + repair** (round-1 surrogate) | It is the baseline being measured, not a decoder: 40/241 invalid predictions survive it, and every rejection costs a second generation. Measuring it against itself would prove nothing. |
| **`guided_choice`** (a fixed list of allowed completions) | The space of PostgreSQL SELECT statements is infinite; a choice list can only hold a handful of canned queries. It cannot answer 241 unseen questions. |
| **`guided_json` with `{"sql": "<string>"}`** | This constrains the *envelope*, not the language: the string field accepts any text, including fences and prose, so the invalid-SQL rate would not move. It also changes the output contract (the tool would need a JSON-unwrap step), which would fold a prompt/parse change into the decoder delta. |
| **`guided_regex`** | xgrammar compiles a regex to a finite automaton. SQL needs **recursion** — `IN (SELECT …)`, subqueries in `FROM`, nested parentheses — which an FSA cannot express. Any regex that covers the BIRD gold set in full becomes a hand-written Parser, i.e. a grammar, at which point `guided_grammar` is the honest tool. The regexes that *are* expressible (`^\s*SELECT .*$`) only forbid the first three characters, and `.*` would re-admit fences mid-sequence. |
| **Post-hoc cleanup** (strip ```` ```sql ````, cut trailing prose, re-run sqlglot repair) | Rejected as a *decoder* claim on principle: it edits the model's text after generation, so the measured difference would belong to a post-processor nobody ships. It is also weaker than it looks — the round-1 gate already did the same class of work and still left 40/241. |
| **`llama.cpp` GBNF server** (the task's declared fallback) | Not needed: vLLM installed cleanly on the A100 and its `structured_outputs.grammar` path is exactly GBNF compiled by xgrammar. Kept as the fallback in case the install failed; it did not. |
| **bf16 serving of the 7B** | The A100 is shared: the captain's scale-up training held ~46 GB and a sealed-adapter evaluation server another ~7.4 GB while this ran. A bf16 7B (≈15 GB of weights plus KV cache) would have pushed the device past the ~60 GB ceiling the task set. |
| **bitsandbytes 4-bit inside vLLM** | The vLLM build on the box (0.32.0) has no `bitsandbytes` entry in its quantization registry (`awq`, `gptq`, `compressed-tensors`, `fp8`, `int8`, `mxfp4`, `torchao`, …) — `--quantization bitsandbytes` is not a valid choice there. The 4-bit arm is therefore **AWQ**: `Qwen/Qwen2.5-Coder-7B-Instruct-AWQ` (5.19 GiB of weights), which is the same 4-bit *precision* the branch's numbers use, but a different quantizer (AWQ vs bitsandbytes-nf4). That is a documented difference, not a hidden one — see §6. |

## 3. What was built

Three small pieces, all in `dev_tools/`, none of them touching `src/` or
`benchmarks/` (the graded harness stays byte-identical):

1. **`dev_tools/grammars/postgres_select.gbnf`** — a SELECT-only PostgreSQL grammar
   in GBNF (xgrammar dialect). Coverage, measured against the corpus rather than
   assumed: **241/241** of the `bird_large` gold queries after a lossless
   sqlite→postgres rewrite (`fixed_eval.sqlite_to_postgres_safe`), **215/241** of the
   raw sqlite gold (the misses are sqlite-only syntax: backticked identifiers and
   `LIMIT a, b`), and **211/241** of the gold as the *shipped* evaluator rewrites it
   — where the 30 misses are the `NULLIF(NULLIF,0)(…)` corruption already documented
   as defect R1-D1, i.e. text no engine can parse. Compile time with the serving
   tokenizer: **2.7 s**.
2. **`dev_tools/guided_proxy.py`** — an OpenAI-compatible proxy in front of vLLM.
   It injects `structured_outputs.grammar` **only** into SQL-generation requests
   (classified by the tool's own system prompt: `You are an expert PostgreSQL
   analyst`), leaves the ambiguity gate unconstrained (its answer must be `OK` /
   `AMBIGUOUS: …`), and exposes `GET /health` with the provenance
   `run_benchmark.py` copies into `run.json` — including a `guided_decoding` block
   (grammar file, SHA-256, request counts) and a device-memory sampler. It performs
   **no text transformation at all**: the only change to a request is one added
   field, which is what keeps the delta attributable to decoding.
3. **`dev_tools/run_benchmark.py --disable-sql-repair`** — wraps the real tool so
   the arm has no repair loop (neither the sqlglot syntax repair nor the execution
   repair). The gate itself stays, because the gate is the *measurement*: an
   unparseable first attempt must stay visible instead of being fixed away.

Supporting tools: `dev_tools/check_grammar.py` (compiles the grammar with the
serving tokenizer and reports gold acceptance; can also replay a run's completions
and prove every one of them is inside the grammar) and
`dev_tools/decoder_report.py` (assembles parse-validity, decode-side facts and
latency per arm). Offline tests: `tests/test_guided.py`.

## 4. Serving configuration (exact, reproducible)

```bash
# vLLM 0.32.0, torch 2.13.0+cu130, xgrammar 0.2.8 — separate venv, the training
# venv (/root/venv) is untouched.
/root/venv-vllm/bin/vllm serve /root/models/qwen2.5-coder-7b-awq \
    --served-model-name qwen2.5-coder-7b-awq --host 127.0.0.1 --port 8001 \
    --max-model-len 3072 --gpu-memory-utilization 0.09 --max-num-seqs 4 \
    --dtype float16 --quantization awq

# Proxy: two instances, one per arm (grammar / no grammar), same vLLM upstream.
python -m dev_tools.guided_proxy --port 8100 --upstream http://127.0.0.1:8001 ...   # arm G
python -m dev_tools.guided_proxy --port 8110 --upstream http://127.0.0.1:8001 --no-grammar ...  # arm N
```

Three environment obstacles were hit and fixed on the box, and they are worth
recording because they are the reason a first attempt "just running vLLM" fails:

* **`torch.compile` needs a C compiler.** Without `gcc`, inductor raised
  `RuntimeError: Failed to find C compiler` at engine start. Fixed by
  `apt-get install -y gcc`. (`--enforce-eager` is *silently ignored* by this build —
  the startup log still shows `enforce_eager=False` — so it is not a workaround.)
* **FlashInfer's sampling kernel needs `nvcc`.** `FlashInfer … get_cuda_path
  RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' does
  not exist`. Fixed with `VLLM_USE_FLASHINFER_SAMPLER=0` (the PyTorch sampler is
  used instead; sampling is greedy, so nothing about the output changes).
* **The proxy must impose the generation cap.** The harness never sends
  `max_tokens`, and vLLM would default to `max_model_len - prompt`. The proxy sets
  `max_tokens = 512` when the caller omits it, so `run.json`'s
  `max_tokens_source: server:/health` is again a true statement.

Measured footprint: weights 5.29 GiB, KV cache 10 112 tokens (concurrency 3.29× at
3 072 tokens), device-wide usage **60.6–72.6 GB of 80 GB** across the two arms (the
training run's 46.1 GB was resident throughout; another user's evaluation server came and
went during the grammar arm). `run.json` carries the sampled peak per run
(`device_memory`), closing the gap [REPORT.md §5.1](REPORT.md) flagged (the serving process
owns `torch.cuda.max_memory_allocated`, so the proxy samples `nvidia-smi` instead). The
engine was shut down as soon as the measurement ended, and it was never larger than
`--gpu-memory-utilization 0.09`.

## 5. The grammar, in one screen

`root ::= optws select_stmt optws`, with `WITH [RECURSIVE]`, `UNION [ALL] /
INTERSECT / EXCEPT`, all join flavours, subqueries in `FROM`/`WHERE`/`IN`/`EXISTS`,
`CASE`, `CAST(x AS t)` and `x::t`, `EXTRACT`, `SUBSTRING(x FROM a FOR b)`, window
functions (`OVER (PARTITION BY … ORDER BY …)`), `DISTINCT`, `GROUP BY`/`HAVING`,
`ORDER BY … [NULLS FIRST|LAST]`, `LIMIT`/`OFFSET`, double-quoted identifiers,
`'…'` strings with `''` escapes, numeric/scientific literals, `DATE '…'`-style
typed literals.

Deliberate, documented constraints (each measured, not hidden):

* **Keywords are upper-case.** Every one of the 241 gold queries uses upper-case
  keywords (lower-case appears 2× for `ORDER`, 6× for `AND`, 1× for `OR`), so this
  follows the corpus. The size of the constraint is measured, not assumed: **4 of arm
  N's 241 raw completions** (same model, no grammar) contain a lower-case SQL keyword,
  so the grammar removes a style the model was already 98.3 % consistent with.
* **No backticks, no fences, no trailing semicolon.** That is already the tool's
  own output contract; the grammar turns the request into a guarantee.
* **Aliases may omit `AS`** (both forms are legal PostgreSQL), so the model is not
  pushed into a style it would not choose.
* Known non-goals: set-returning functions in `FROM`, `LATERAL`, `FETCH FIRST`,
  dollar-quoted strings, `x = ANY (<subquery>)`, `AT TIME ZONE`. None appears in
  `bird_large`; the acceptance number above is the measured consequence.

The grammar is validated two ways before any benchmark number is quoted:
`dev_tools/check_grammar.py` (gold acceptance, above) and a 59-case battery of
constructs plus **negative** cases (fenced output, prose, trailing semicolon,
lower-case keywords must all be *rejected* — they are, and the offline test suite
pins the grammar's structural invariants so a broken rule cannot ship silently).

## 6. Threats to validity (read this before quoting a delta)

1. **Quantizer differs from the branch's baseline.** Arm N and arm G both run AWQ
   4-bit through vLLM; the round-1 E1/E3 numbers were bitsandbytes-nf4 through
   `serve_openai.py`. The *decoder* delta (G vs N) is measured entirely inside one
   serving stack, so it is clean; the *level* (EX/241 of either arm vs 51/241) is
   not a like-for-like model comparison and is not claimed as one.
2. **Shared GPU.** Training (100 % util. on the other stream) and a sealed-adapter
   evaluation server were resident for the whole measurement, and the *effective*
   throughput of the device moved by ~8× between the two arms' windows (the same
   short prompt took 0.6 s early and 5.0 s later with no grammar at all). Greedy
   decoding is deterministic, so EX is unaffected; **cross-window latency
   comparison is invalid**, which is why the grammar's cost is measured with an
   *interleaved* test instead (same prompt, alternating arm, 6 rounds —
   median 7.12 s with the grammar vs 4.99 s without = **×1.43 wall clock, ×1.92 per
   token**), and the harness wall clocks are reported as observed rather than as a
   speed claim.
3. **The grammar cannot fix schema errors.** A hallucinated column is syntactically
   perfect. The expectation, stated before the run, was that the *parse-validity* number
   would collapse while the *PostgreSQL-unexecutable* number moved much less. The
   measurement was blunter than the expectation: on the vLLM stack the tool's own
   fence-stripping already made parse-validity ≈ 100 % (so the grammar's parse-level gain
   is 0 questions), and the unexecutable set is 89 % identifier/function errors. §13 of
   the report shows both numbers side by side, precisely so that the grammar is not
   credited with an execution gain it cannot produce.
4. **Prompt tokens are unchanged.** Both arms use the tool's own prompts verbatim
   (`--disable-sql-repair` only removes the repair call), so no prompt engineering
   rides along with the decoder change. The artifact proof: both `run.json` files report
   385 921 prompt tokens.

## 7. What it bought (one paragraph, numbers in REPORT.md §13)

On the same 241 questions, same model, same prompts, repair off in both arms: **the
output-contract violation went from 241/241 completions to 0/241** (fences, prose and
trailing semicolons are structurally impossible under the grammar), all 241 guided
completions re-parse inside the compiled grammar, and **EX moved from 39/241 (16.18 %) to
44/241 (18.26 %) — b = 7, c = 2, exact McNemar p = 0.18, 95 % CI [−0.41, +4.56] pp, i.e.
not significant at n = 241.** The number the round-1 surrogate was blamed for (40/241
invalid predictions) did **not** collapse, because it was never a fence problem: 55 of the
62 unexecutable predictions are `UndefinedColumn`/`UndefinedTable`/`UndefinedFunction` and
only 2 are syntax errors — a grammar cannot name the right column. Cost: ×1.43 wall clock
(×1.92 per token), measured interleaved inside one GPU window.

Conclusion: a grammar decoder is the right tool for *guaranteeing the output contract*, and
the wrong tool for the accuracy gap this branch actually cares about — that gap is schema
grounding, not syntax. Shipping it would buy a clean contract and ~+2 pp that the paired
test cannot distinguish from noise, at ~1.4–1.9× decode cost; the honest recommendation is
to keep it as an optional arm (`dev_tools/guided_proxy.py`, off by default) rather than to
make it the default decoding path.