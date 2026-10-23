# DECISIONS — architecture decision records

One record per decision that constrains the code we hand over. Format: **Context / Decision /
Consequence**, plus the evidence a reviewer can re-run. Records are append-only: a reversal gets a new
record that supersedes the old one, the old one is never edited.

Branch: `feat/lora-track`, based on `main` `e812451`.

---

## ADR-001 — The teacher's harness is never edited; measurement code lives beside it

**Context.** `benchmarks/` implements the grading harness (`base.py` builds one tool per `db_id`,
`bird.py`/`ambrosia.py` map the tool verdict to a prediction, `evaluate_*.py` score it). Its defects are
real and measured: the SQLite→PostgreSQL rewrite destroys 30 of 241 gold queries and silently mutates 3
more (`docs/EVAL.md` §2). Editing it would change the thing we are graded with — the teacher's harness,
not ours — and any "improvement" would be unverifiable against the course run.

**Decision.** Treat `benchmarks/`, `data/` and the three root entrypoints as read-only. All measurement
code is new code in `dev_tools/` (`fixed_eval.py`, `local_bench.py`, `dump_schemas.py`,
`run_benchmark.py`), and the graded path still runs the shipped files unchanged.

**Consequence.** Our numbers are always reported twice: `EX/241` under the shipped evaluator (comparable
with the course) and the corrected view (`EX/211`, live `EX/209`, honest maximum `208/241`) under
`fixed_eval`. The invariant is enforceable and enforced: `git diff main --stat -- benchmarks data
bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py` is empty (`docs/VERIFICATION.md` §5).
Cost: we cannot "fix" the ceiling for the grade — only report it honestly.

---

## ADR-002 — Prompts are English, matched to the question language

**Context.** BIRD and Ambrosia questions are English; the baseline `prompts.py` was Russian and told the
model to answer ambiguities in Russian prose. The harness compares the tool verdict with the exact token
`OK` (through `ToolResponse.status`), so any non-`ok` answer becomes `"ambiguous"` and scores 0 on BIRD —
a failure mode that has nothing to do with SQL quality (measured: Russian prompt, English question,
guaranteed zero on every item the gate mistook).

**Decision.** Rewrite every prompt in English: one system prompt with the schema placeholder, a strict
output contract (one `SELECT`, no fences/comments/trailing semicolon, no backticks, PostgreSQL dialect,
`NULLIF(denominator, 0)`), and a two-form ambiguity contract: exactly `OK` or `AMBIGUOUS: <one clause>`.
Tolerance is applied on our side (fences, punctuation, casing) instead of hoping for exact formatting.

**Consequence.** Question language, reasoning language and answer shape agree. The test suite asserts
`prompts.py` contains no Cyrillic character and that the rendered system prompt embeds the exact schema
string `build()` produced — the two things that would silently reintroduce the defect. Cost: the adapter
is trained on our prompt surface, so a graded run with the repository's original prompts would mis-align
the adapter (a known, documented risk; see ADR-006 and `PLAN.ru.md` Q7).

---

## ADR-003 — Default ambiguity mode is `birdsafe`, not `strict`

**Context.** The two benchmarks score the same verdict differently. BIRD has **0 ambiguous gold items in
241**, so every `ambiguous` answer is a guaranteed zero. Ambrosia is scored as pure ambiguity
classification (`evaluate_ambrosia.py`: correct ⇔ `gold_is_amb == pred_is_amb`), 70 of 240 gold items are
ambiguous, so answering never-ambiguous already yields a fixed **70.83 %** floor — and `strict` beats that
floor only if the gate's precision on ambiguous predictions is above 50 %.

**Decision.** Ship three modes (`strict`, `birdsafe`, `off`) and default to **`birdsafe`**: the gate still
runs (so its verdicts are logged for analysis) but never returns `ambiguous`, which makes a BIRD item
impossible to lose to the gate. The mode is selectable per run without touching code
(`TEXT2SQL_AMBIGUITY_MODE=strict`, or `Text2SQLConfig(ambiguous_mode=...)`; the constructor wins over the
env). A transport failure of the gate fails open (treated as unambiguous).

**Consequence.** BIRD keeps its full ceiling on every run; Ambrosia must be run explicitly with `strict`
(the runbook's §6/S1/S4 steps say so), where its upside is measurable against the 70.83 % floor. Cost: one
extra LLM call per item in `birdsafe`; use `off` when only BIRD matters and tokens do.

---

## ADR-004 — At most one repair per generation, and never after a failed generation

**Context.** Predictions are executed verbatim, so SQL that does not parse is a zero — a repair attempt is
worth one call. The first implementation repaired *unconditionally*, including after a generation that
returned nothing (`RepairRequest(sql=raw or "")`): with `max_llm_retries=2` an all-failing question cost
**9** LLM calls where the budget allowed **6**, and the repair prompt carried an empty "Rejected SQL"
block. Found by verification round 1 (finding F3), fixed and regression-tested.

**Decision.** The budget per question is: one ambiguity call (unless `off`) + one generation + **at most
one** repair, each worth at most `max_llm_retries+1` transport attempts. A repair only follows a
generation that returned non-empty SQL; a generation that produced nothing ends the question; a blank
reply is a failed attempt (retried) rather than a repairable answer. The same one-shot rule applies to the
optional execution-feedback correction.

**Consequence.** Deterministic, assertable cost: happy path 2 calls, one repair 3, all-failing transport
`2*(R+1)` (6 at `R=2`, was 9), empty reply no longer triggers a repair. `tests/test_tool.py` asserts all
four counts and that no repair prompt can carry an empty SQL string. Cost: a model that fails once on
transport and once on SQL is not given a third chance — accepted, because a second repair is far more
likely to be a model without the schema than a fixable typo.

---

## ADR-005 — Report four denominators, never one

**Context.** Three different "BIRD numbers" are in circulation and they are not interchangeable:
`EX/241` (what the shipped evaluator prints), `EX/211` (241 − 30 unparseable gold, i.e. the questions that
can be answered at all), and `EX/209` (241 − 32 gold queries that actually execute on the live server,
because ids 4 and 35 fail with `UndefinedColumn` after the identifier rename). One further question parses
and executes but answers a *different* question (id 572, mutated literal), so the honest maximum is
**208/241 = 86.3 %**.

**Decision.** Every BIRD result is reported as `EX/241` **with** `EX/211` (and `EX/209` when a live run
produced it), never as a single percentage. The evaluator emits all of them plus Wilson CIs so a report
cannot pick one by accident. The Ambrosia number is always shown next to its 70.83 % constant floor.
Offline SQLite figures (`local_bench`) are labelled `proxy, out of harness`.

**Consequence.** A reader can tell model quality from harness damage: `EX/211 − EX/209` is execution
fragility, `209 − 208` is the one unwinnable item. Cost: the deliverable carries more numbers, and any
sentence with a single `%` is incomplete by policy.

---

## ADR-006 — Distillation is the primary data recipe; raw gold is a fallback

**Context.** The repo ships BIRD gold in SQLite dialect (`CAST(... AS REAL)`, backticks, `IIF`, `IFNULL`)
while the harness executes on PostgreSQL; 16/357 training targets carry backticks and 25 use SQLite-only
functions, and the repo's own rewrite corrupts 14 + 1 targets before training even starts. A 684-row
dataset over 8 schemas buys behaviour and dialect alignment, not capability (measured: the first adapter
learns the output contract and still invents identifiers — `training/RUN_NOTES.md` §3).

**Decision.** Prefer teacher predictions (`--teacher/--teacher-results`) filtered by
`training/teacher_filter.py`: keep a teacher SQL only if it parses as PostgreSQL **and** its execution
result equals gold through an injected executor; every rejection is counted by reason. Raw gold with the
safe rewrite remains the fallback when no verified teacher file exists. Train under the inference prompt
surface (`--feature-prompt`) so the adapter sees the same schema block it will see at serving time.

**Consequence.** Imitation targets are executable and semantically verified, so variance drops and the
dialect penalty disappears from the targets. Cost: a teacher run is needed before a strong build, and the
filter's honesty depends on the executor — which is why it is injected (tests never touch a database) and
why parse-only mode is reported explicitly rather than silently accepted.

---

## ADR-007 — Train on the PostgreSQL schema dump, not the SQLite one

**Context.** The same 8 databases differ between the two dumps by **218 identifier-casing differences**
(2 tables + 216 columns) and **59 type-text differences**; PostgreSQL folded unquoted identifiers to lower
case in 4 of 8 databases (`satscores.AvgScrMath` → `avgscrmath`). The tool prints what the server reports,
so a model trained on the SQLite schema learns identifiers that do not exist at serving time.

**Decision.** `db_schemas.json` for training is dumped from the live PostgreSQL through the tool's own
renderer (`dev_tools/dump_schemas.py`, byte-identical check `--verify-against-tool`). The SQLite dump is a
tunnel-down fallback only, and a graded run regenerates the dump before training.

**Consequence.** Training and serving cannot drift silently, and the same file feeds `--plan` provenance
(`schema_basis=file`). Cost: the tunnel is on the critical path of a *good* dataset; the offline fallback
is explicitly second-best and is refused for a real training run unless forced.

---

## ADR-008 — Offline-first substrate: SQLite execution harness + local OpenAI-compatible server

**Context.** The grade needs the course PostgreSQL (tunnel, credentials, an LLM endpoint) and that access
died mid-session. Iterating only against the server would make every experiment cost a tunnel, tokens and
an hour. vLLM is Linux-only and this workstation has no Docker.

**Decision.** Two offline substrates, both contract-tested: `dev_tools/local_bench.py` for the official
BIRD SQLite protocol (gold execution, official EX, distillation filter) and
`dev_tools/serve_openai.py` for an OpenAI-compatible endpoint (transformers + optional PEFT adapter,
`--fake` for wiring). Neither is ever imported by the graded path.

**Consequence.** The data build, the training run, the scoring logic and the whole tool contract are
testable with no database, no network and no key (138 tests, ~35 s). Cost: the SQLite protocol is *not*
the grade, so its numbers are clearly named `bird-sqlite-official` and never pasted into the graded table;
and `serve_openai` is slower than vLLM (no batching), which matters only for wall-clock, not for accuracy.
