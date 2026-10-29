# Procedure arms — retrieval few-shot and self-consistency

Two **procedure-only** changes to the tool: they alter what the model is asked and
how its samples are combined, not the weights. Both are **OFF by default**, so the
sealed baseline and the round-1 adapter numbers keep meaning what they meant.

The point is a fair comparison: round 2 measured the adapter at **+4 pp on BIRD
(inconclusive)**. These arms exist to test whether work *outside* the adapter moves
the same benchmark more than the adapter did. They are orthogonal cells, not a
replacement for the LoRA track, and nothing here trains, fine-tunes or quantises
anything.

| arm | what changes | default | module |
|---|---|---|---|
| retrieval few-shot | prompt carries `k` similar **train** questions with their gold SQL | OFF | `src/.../text2sql_tool/src/retrieval.py` |
| self-consistency | `n` seeded samples, majority vote over result sets, then over SQL text | OFF | `src/.../text2sql_tool/src/self_consistency.py` |

Nothing under `benchmarks/`, `data/` or the root entrypoints changes: the harness
contract (ADR-001) builds the tool as `tool_cls(db_uri=..., llm_client=...)`, so the
arms are switched on through `Text2SQLConfig` fields or, on the sealed path, through
the `TEXT2SQL_*` environment variables documented in §4.

---

## 1. Retrieval few-shot

```
question + evidence ──embed──▶ cosine over the train bank ──top-k ──▶ worked-example
                                                                     block in the SQL prompt
```

The bank is built once per process from `data/train_queries.json` (the 357 BIRD
**train** rows that ship with the repo) and scoped to the tool's `db_id`, so a
`california_schools` question can only retrieve `california_schools` examples.
Retrieval is per question; the block is injected into the *initial* SQL prompt only
(a repair prompt already carries the rejected SQL and the error).

### 1.1 Honesty constraints (each one is a hard rule, each one is tested)

1. **Train-only provenance.** `is_allowed_bank_source` accepts exactly two shapes:
   a file named `train_queries.json`, or a JSONL/JSON file of the official BIRD
   train split (name starts with `bird23_train` or it lives in a `bird_train/`
   directory). `bird_small.json`, `bird_large.json` and `mini_dev*` are refused by
   name with an explicit error — a caller that asks for a leak gets a loud failure,
   not an empty bank.
2. **Leakage guard (content).** Every candidate row is additionally dropped when its
   `question_id` is a dev id or when its exact question string (whitespace-collapsed,
   case-folded) is a dev question. Dev ids and questions come from
   `data/bird_large.json` and `data/bird_small.json` (241 + 22 rows, read-only).
   This is the layer that catches a *renamed* dev file: copying `bird_large.json` to
   `train_queries.json` passes layer 1 and yields an empty bank, with the drop
   counters in the metadata (`tests/test_procedures.py`).
3. **PostgreSQL examples.** BIRD gold SQL is SQLite. It is transpiled with the same
   `sqlglot` read/write pair as `training/build_data.py` (`sqlite → postgres`),
   re-parsed as PostgreSQL and dropped if it does not survive; a worked example full
   of backticks would teach the model exactly the identifier quoting that the graded
   harness executes as a silent zero.
4. **Fully offline.** The default embedder is a local sentence-transformer loaded with
   `local_files_only=True`; the alternative is a dependency-free hashing embedder.
   No API key is read and no network request is made.
5. **Visible in the artefact.** The rendered block, its SHA-256 and the retrieved
   examples (ids, scores, SQL) are written to the per-question procedure log and are
   reachable through `Text2SQLGenerator.procedure_metadata()`; `run.json` records the
   `TEXT2SQL_*` environment under `procedure_env` (§4).

### 1.2 Options

| config field | env | default | meaning |
|---|---|---|---|
| `retrieval_few_shot` | `TEXT2SQL_RETRIEVAL_FEWSHOT` | `False` | master switch |
| `retrieval_k` | `TEXT2SQL_RETRIEVAL_K` | `3` | examples per prompt, capped at `HARD_CAP_K = 10` |
| `retrieval_min_similarity` | `TEXT2SQL_RETRIEVAL_MIN_SIMILARITY` | embedder default | cosine floor; `None` uses `0.20` (hash) or `0.40` (sentence-transformer) |
| `retrieval_include_evidence` | `TEXT2SQL_RETRIEVAL_EVIDENCE` | `True` | whether evidence joins the similarity query **and** is rendered in the examples |
| `retrieval_bank` | `TEXT2SQL_RETRIEVAL_BANK` | `data/train_queries.json` | bank source (allowlisted, §1.1) |
| `retrieval_embedder` | `TEXT2SQL_RETRIEVAL_EMBEDDER` | `sentence-transformer` | `sentence-transformer` or `hash` |
| `retrieval_model` | `TEXT2SQL_RETRIEVAL_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | local weights only |
| `db_id` | – | derived from `db_uri` | override the bank scope |

The threshold default is **per embedder** on purpose: the two backends do not share a
similarity scale, so one number would be wrong for one of them. The hashing embedder
is a lexical fallback (`word unigrams + bigrams → 512 hashed buckets`, L2-normalised),
not a semantic model: paraphrases with no shared words score near zero. It exists
because this machine has **no local sentence-transformer weights**, and an arm must
not be unmeasurable just because a download is impossible.

With `--use-evidence` the harness passes one string in the form
`question: <q>, evidence (may be empty): <e>` (`benchmarks/bird.py`). The tool splits
that format for retrieval purposes only (`split_question_evidence`) and never rewrites
the question it sends to the model.

### 1.3 Cost (measured, offline)

Measured on this machine with the `hash` embedder, the shipped 357-row bank, the 241
dev questions of `data/bird_large.json`, and `tiktoken` `cl100k_base` as a token
approximation (Qwen's tokenizer differs; the block is appended to every SQL call):

| k | dev questions with ≥1 example | block chars (mean / max) | cl100k tokens (mean / max) |
|---|---|---|---|
| 1 | 240 / 241 | 408 / 750 | 110 / 213 |
| 3 | 240 / 241 | 1214 / 1696 | 325 / 538 |

So `k=3` costs roughly **0.3k prompt tokens per SQL call** — small next to a full
schema, but paid on every call, including calls that later need a repair. The
sentence-transformer arm's numbers are **not** in this table: its weights are not on
this machine, so only its degradation path was exercised (below).

### 1.4 Degradation

Everything that can go wrong becomes a recorded reason instead of an exception:
absent weights, an unknown embedder, a dev-only source, an unreadable file, an empty
bank. The tool then sends its normal prompt and `procedure_metadata()` reports
`"enabled": true, "available": false, "reason": "..."`. Retrieval never raises into
`query()`, so a missing model can only cost the arm, never a benchmark item.

Verified here (`python -m dev_tools.procedures_dryrun --embedder sentence-transformer`):

```
Few-shot retrieval is unavailable: EmbedderUnavailable: could not load local
sentence-transformer weights for 'sentence-transformers/all-MiniLM-L6-v2': We couldn't
connect to 'https://huggingface.co' to load the files, and couldn't find them in the
cached files.
```

The message comes from `transformers`' offline path; `local_files_only=True` is what
makes it a lookup failure rather than a download.

---

## 2. Self-consistency

```
n seeded samples at temperature > 0
      ├─ invalid SQL                    → not eligible
      ├─ valid, executes               → result-set signature
      └─ valid, database unreachable   → normalised SQL text
vote: largest group of result sets; if none executed, largest group of SQL text;
ties → the earliest sample. Never raises.
```

### 2.1 Selection rules

1. Only candidates that passed the sqlglot gate are eligible.
2. If ≥1 eligible candidate executed successfully: group by a canonical result-set
   signature (row order ignored, values type-preserving) and pick the largest group.
3. Otherwise group by normalised SQL text (parsed PostgreSQL when possible,
   whitespace-collapsed otherwise — the normalisation itself can never raise) and
   pick the largest group. This is the posture when the database is unreachable and
   also when every candidate was rejected.
4. A tie always goes to the lowest sampling index, so a repeated run is a repeat, not
   a new draw.

`choose()` in `self_consistency.py` is pure: the tool owns sampling and execution,
which is what makes the rules testable offline.

### 2.2 Options

| config field | env | default | meaning |
|---|---|---|---|
| `self_consistency` | `TEXT2SQL_SELF_CONSISTENCY` | `False` | master switch |
| `self_consistency_n` | `TEXT2SQL_SC_N` | `5` | candidates, capped at `HARD_CAP_N = 9` |
| `self_consistency_temperature` | `TEXT2SQL_SC_TEMPERATURE` | `0.7` | must be `> 0`; capped at `2.0` (local server limit) |
| `self_consistency_seed` | `TEXT2SQL_SC_SEED` | `0` | candidate `i` is drawn with `seed + i` |
| `self_consistency_budget_s` | `TEXT2SQL_SC_BUDGET_S` | `120` | wall-clock cap on the whole round, generation **and** execution |
| `self_consistency_row_cap` | `TEXT2SQL_SC_ROW_CAP` | `200` | rows kept per candidate for the comparison |

Sampling parameters travel through the client's `extra_create_args`
(`{"temperature": t, "seed": s}`), which overrides the client-level `temperature=0`
that `benchmarks/base.py` sets. A client that does not accept `extra_create_args`
(e.g. a test stub) is tolerated: the arm falls back to the client's decoding settings
and logs a warning — a run must not claim to have sampled when it did not.

Configuration errors are loud at construction time, exactly like an unknown
ambiguity mode: `self_consistency` with `temperature = 0` raises, a non-positive
budget raises, and an unknown embedder raises. A silently ignored arm would make a
run report numbers for a configuration nobody chose. `n > 9` and `k > 10` are clamps
and are logged.

### 2.3 Cost and latency

* **LLM calls:** `n` generations instead of one. On the local single-request server
  they are sequential, so the added latency is roughly `(n − 1) ×` one generation.
  A candidate that fails validation leaves the vote without a repair attempt: repairs
  stay out of the sampling round on purpose (they would multiply the call count and
  bias the vote toward the samples that happened to be repairable).
* **Executions:** up to `n` executions, each under the tool's `execution_timeout_ms`
  (5 s default), each keeping at most `self_consistency_row_cap` rows. Once one
  candidate reports the database unreachable, the remaining executions are skipped.
* **Hard ceilings:** `n ≤ 9` and the round obeys `self_consistency_budget_s`; a round
  that runs out of budget votes on the candidates it already has.
* **Never raises:** a failed draw is a missing candidate, and a round without a valid
  candidate returns `status="error"` — the same posture as a failed single generation.

---

## 3. What the artefact records

`Text2SQLGenerator.procedure_metadata()` returns, per tool:

```json
{
  "retrieval_few_shot": {
    "enabled": true, "available": true, "k": 3, "min_similarity": null,
    "threshold_used": 0.2, "source": ".../data/train_queries.json",
    "source_kind": "train_queries", "source_sha256": "f364cadf...",
    "rows_built": 357, "drops": {"dev_question_id": 0, "dev_question_text": 0,
    "no_gold_sql": 0, "untranspilable": 0, "duplicate": 0},
    "embedder": "hash", "embedding_model": "hash-512", "dimensions": 512,
    "last": {"injected": 3, "block": "...", "block_sha256": "...",
             "examples": [{"source_question_id": 55, "score": 0.518, "sql": "..."}]}
  },
  "self_consistency": {
    "enabled": true, "n": 5, "temperature": 0.7, "seed": 0,
    "last": {"drawn": 5, "database_executed": false,
             "selection": {"mode": "sql-text-majority", "selected_index": 0,
                           "vote_count": 3, "groups": [...]},
             "candidates": [{"index": 0, "valid": true, "sql": "...",
                             "execution": "unreachable", "row_count": null}]}
  }
}
```

With `TEXT2SQL_PROCEDURE_LOG=procedures.jsonl` (a relative path lands in the run
directory, which `dev_tools/run_benchmark.py` `chdir`s into) every question appends one
JSONL record with `db_id`, `question`, `retrieval` (including the **verbatim** injected
block and its SHA-256) and `self_consistency` (all candidates and the vote). The DSN is
never written — it carries credentials — only the database name.

`dev_tools/run_benchmark.py` adds `procedure_env` to `run.json`: every `TEXT2SQL_*`
variable of the process, so an artefact names the arms that produced it.

---

## 4. Enabling an arm

The tool is built by the sealed harness as `tool_cls(db_uri=..., llm_client=...)`, so
an eval arm is switched on from the environment. Explicit `Text2SQLConfig` fields win
only where the environment says nothing; a malformed value raises instead of
disabling the arm.

Windows PowerShell (the runbook shell), retrieval with the dependency-free embedder
and self-consistency, recorded into the run directory:

```powershell
$env:TEXT2SQL_RETRIEVAL_FEWSHOT = "1"
$env:TEXT2SQL_RETRIEVAL_EMBEDDER = "hash"          # sentence-transformer needs local weights
$env:TEXT2SQL_RETRIEVAL_K         = "3"
$env:TEXT2SQL_SELF_CONSISTENCY    = "1"
$env:TEXT2SQL_SC_N                = "5"
$env:TEXT2SQL_SC_TEMPERATURE      = "0.7"
$env:TEXT2SQL_SC_SEED             = "0"
$env:TEXT2SQL_PROCEDURE_LOG       = "procedures.jsonl"

python -m dev_tools.run_benchmark --check          # plan + procedure_env in the plan
python -m dev_tools.run_benchmark --allow-llm-spend --predict-only ...
```

`dev_tools/matrix.py` assembles arm commands but launches nothing; put the same
variables in the shell (or in the per-arm process environment) before running the
assembled command. To keep a matrix cell honest, do not change the variables between
two runs that will be quoted as the same cell — `run.json.procedure_env` is what makes
that checkable afterwards.

The default `sentence-transformer` embedder requires the weights to be present on
disk. On this machine they are not, so the first arm is either run with
`retrieval_embedder=hash` (a *lexical* arm, not the semantic one) or the weights must
be fetched on a machine with network access and copied into the Hugging Face cache.
Do not describe a `hash` run as a semantic-retrieval run.

---

## 5. Dry run (no database, no GPU, no network)

```powershell
python -m dev_tools.procedures_dryrun                       # writes runs/t16-dryrun/
python -m dev_tools.procedures_dryrun --no-evidence         # evidence out of the query
python -m dev_tools.procedures_dryrun --embedder sentence-transformer   # expect exit 2
```

The dry run enables **both** arms against the real 357-row bank with the `hash`
embedder, a deterministic stub LLM (`dev_tools/stub_llm.py`, canned answers) and an
injected engine that raises `ConnectionRefusedError` on connect. It asserts that the
retrieval arm activated, that every question injected a non-empty block, that every
question drew a candidate, and writes `dry_run.json`, `prompts.txt` (the exact prompt
per question) and `procedures.jsonl`. Exit code `0` means both arms were audible;
`2` means an arm was requested but did not activate (the transcript says why).

Measured on this machine (2 dev questions, `n=3`, `k=2`):

```
bank: data/train_queries.json (sha256 f364cadf83fd7ae7...), rows 357,
      drops {'dev_question_id': 0, 'dev_question_text': 0, 'no_gold_sql': 0,
             'untranspilable': 0, 'duplicate': 0}
embedder: hash / hash-512 (dims 512), k=2, threshold=0.2
vote: sql-text-majority -> candidate #0 with 2 vote(s)
      [database unavailable (3 candidate(s) not executed); majority over normalised SQL text]
database connections attempted by the injected engine: 4 (all refused)
stub LLM calls: 6 (no GPU, no network)
```

**What it does not prove:** any accuracy change. The answers are canned, so the dry
run proves the plumbing — prompt injection, vote, metadata, fallbacks — and nothing
about quality. The measurement belongs to `eval-run` against the real endpoint.

---

## 6. Test map

`tests/test_procedures.py` (offline, no database, no network, no API key):

| what is pinned | test |
|---|---|
| dev files are refused by name; train files are accepted | `test_source_allowlist_accepts_only_train_files` |
| a dev source raises instead of leaking | `test_loading_a_dev_source_raises_instead_of_leaking` |
| a dev row is dropped by id and by exact question string | `test_content_guard_drops_a_dev_row_by_id_and_by_text` |
| a dev file renamed to `train_queries.json` yields no bank | `test_a_dev_file_renamed_to_train_queries_yields_no_bank` |
| the shipped bank contains no dev id/text, no backtick, and is stable | `test_shipped_train_bank_is_leak_free_and_deterministic` |
| transpilation drops SQL that PostgreSQL cannot parse | `test_transpile_rejects_what_postgresql_cannot_parse` |
| the hash embedder is deterministic and batched | `test_hash_embedder_is_deterministic_and_order_preserving` |
| nearest training question first, deterministically | `test_retrieval_returns_the_closest_training_question_first` |
| threshold and database scope | `test_retrieval_threshold_and_database_scope_are_enforced` |
| block rendering and the evidence switch | `test_examples_block_is_rendered_deterministically` |
| evidence split only for the harness format | `test_question_evidence_split_understands_only_the_harness_format` |
| example injection visible in prompt + metadata + log, no DSN in the log | `test_tool_injects_examples_and_records_them` |
| absent weights degrade to a recorded no-op, item still answered | `test_retrieval_without_local_weights_degrades_cleanly` |
| the arm is off by default; unknown embedder is loud | `test_retrieval_is_off_by_default_and_unknown_embedder_is_loud` |
| result-set majority, tie → earliest | `test_vote_prefers_the_largest_result_set_group` |
| row order ignored, value types preserved | `test_result_signature_ignores_row_order_but_not_values` |
| database-down fallback = SQL-text majority | `test_vote_uses_sql_text_when_the_database_is_unreachable` |
| no valid candidate → `none` | `test_vote_returns_nothing_without_a_valid_candidate` |
| seeded sampling, `temperature=0` refused, caps | `test_sampling_args_are_seeded_and_refuse_greedy_decoding` |
| `n` candidates, result-set vote, seeds/temperature on the wire | `test_self_consistency_samples_n_candidates_and_votes_by_result_set` |
| one connection attempt once the database is down | `test_self_consistency_falls_back_to_sql_text_when_the_database_is_down` |
| all-invalid round never raises | `test_self_consistency_never_raises_and_reports_no_sql` |
| a repeated round reproduces query and sampling arguments | `test_self_consistency_round_is_reproducible` |
| `n` and the time budget are enforced | `test_self_consistency_caps_n_and_the_time_budget` |
| greedy path untouched when off | `test_self_consistency_is_off_by_default_and_leaves_greedy_calls_untouched` |
| greedy self-consistency config refused | `test_self_consistency_rejects_a_greedy_configuration` |
| env overrides, typos raise, empty env keeps defaults | `test_environment_*`, `test_empty_environment_keeps_the_defaults` |
| the sealed constructor path honours the environment | `test_constructor_only_harness_path_honours_the_environment` |
| no log without an enabled arm | `test_procedure_log_is_not_written_when_both_arms_are_off` |
| `run.json` carries `procedure_env` | `test_run_provenance_records_the_procedure_environment` |

---

## 7. Known limits

* **Bank size is the repo's 357 train rows.** The official BIRD train split has 6,601
  rows over 69 `db_id`s, but those `db_id`s do not intersect the 8 dev databases
  (round-1 scale notes, `data_cache/bird_train/README.md`), so it contributes **zero**
  usable examples here. Retrieval diversity is therefore limited by construction;
  the honest way to grow the bank is more train questions for the same 8 databases,
  not more train *databases*.
* **`hash` is lexical.** A `hash` arm answers "does retrieval help at all when the
  surface words overlap", not "does semantic retrieval help". Only the
  sentence-transformer arm is semantic, and its weights are absent on this machine.
* **Examples cost prompt tokens on every call** (§1.3), and self-consistency
  multiplies generation cost by `n` (§2.3). Both arms together are roughly `n`
  generations plus `n` executions per question.
* **The two arms interact by design and are not separable in one cell:** with
  retrieval on, all `n` candidates share the same example block, so the vote measures
  sampling variance *given* that block. Treat `{adapter} × {retrieval} × {vote}` as
  the matrix, not as three independent switches to quote one at a time.
* **No accuracy numbers here.** This document describes options and costs; the
  measurement (BIRD EX, paired comparison against the round-1 baseline) is
  `eval-run`'s task, on the real endpoint.
