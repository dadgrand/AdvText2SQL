# V1 — Verification of branch `feat/lora-track` (offline suite + live-DB reconciliation)

Verifier: `qa-review` (independent gate, task t10) · Date: **2026-10-23**
Branch: `feat/lora-track` @ `220991b` (wip eval) ← `80b60f8` (tool v2) ← `e812451` (`main`, `docs: minor readme change`)
Environment: Windows, Python **3.12.10**, `sqlglot 30.23.0`, `pytest`. No network, no LLM key was used by any
command below. Live PostgreSQL **15.15** measurements are quoted from the captain's session (§7, artifacts under
`research/advtext2sql/artifacts/`); the verifier did not re-execute the 241 gold queries.

**Verdict: PASS.** I1, I2 and I3 each meet their acceptance criteria, the three implementations work together as
one suite, and the teacher's harness is untouched. **Blocking findings: none.** Three non-blocking observations
are in §9.

The verifier wrote only this file. `benchmarks/`, `data/`, `src/` (apart from the tool's own modules) and the root
entrypoints are byte-identical to `main`.

---

## 1. Commands executed (exit codes and excerpts)

All commands run from `research/advtext2sql/repo` unless a path says otherwise. `PYTHONDONTWRITEBYTECODE=1` was set;
`python -m compileall` intentionally produces gitignored `__pycache__` only.

| # | command | exit | output excerpt |
|---|---|---|---|
| 1 | `python -m pytest tests -q` | **0** | `77 passed in 151.42s` — one suite, not per-file |
| 2 | `python -m pytest tests/test_eval.py -q` (I1) | **0** | `36 passed in 4.90s` |
| 3 | `python -m pytest tests/test_tool.py -q` (I2) | **0** | `22 passed in 131.58s` |
| 4 | `python -m pytest tests/test_data.py -q` (I3) | **0** | `19 passed in 14.82s` |
| 5 | `python -m compileall -q src dev_tools training` | **0** | no output (clean) |
| 6 | `python -m compileall -q dev_tools` (I1 §5.4) | **0** | no output |
| 7 | `python -m dev_tools.fixed_eval --answers data/bird_large.json --out $TEMP/gold_damage_large.json --gold-damage` (I1 §5.1) | **0** | `database-free audit` → `30/241 (expected 30)`, `silently_corrupted_ids: ["4","35","572"]` |
| 8 | `python training/build_data.py --self-test` (I3 §2.1) | **0** | `dropped=14 mutated=1 usable=342 train/valid=290/52`, `ambrosia built=438 (394/44) combined=684` |
| 9 | `python training/train_qlora.py --plan --data training/self_test_out --out $TEMP/lora_plan_v1` (I3 §5) | **0** | resolved config, `train_rows=684`, `trl decision=blocked`, `provenance.json` written |
| 10 | `python training/train_qlora.py --plan --data training/out --out $TEMP/lora_plan_v1` (declared form, dir absent) | **0** | `[data] dir=training/out data_hash=None … rows=train=None` — tolerated in `--plan` |
| 11 | `python research/advtext2sql/reconcile_damage.py` (captain's checker, run as mandated) | **0** | `30` unparseable, `3` mutated `[4, 35, 572]`, `208` untouched, `B+C = 33 -> ceiling 208/241` |
| 12 | verifier's own classification (inline `python -`, §2) | **0** | `bird_large A=30 B=3 C=208`; `train_queries A=14 B=1 C=342` |
| 13 | verifier's cross-check vs `fixed_eval` (inline, §2.3) | **0** | all four set differences `[]`; `expected_n_damaged` 30 and 14 |
| 14 | verifier's Ambrosia floor (inline, §4) | **0** | `n=240 gold_ambiguous=70 constant_not_ambiguous_correct=170 accuracy=70.83%` |
| 15 | verifier's Cyrillic scan (inline, §6) | **0** | `prompts.py chars=5084 cyrillic=0` (all four v2 modules 0) |
| 16 | verifier's never-raise probe (inline, §6) | **0** | `returned type: dict`, `{'status': 'error', 'query': None}`, 9 transport attempts, 5.52 s |
| 17 | verifier's I2-claim check: `validate_sql` over original gold (inline, §2.4) | **0** | `original gold accepted by tool gate: 241 / 241` |
| 18 | `git status --porcelain --untracked-files=all` / `git diff main --name-only` / `git diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py` | **0** | scopes clean, harness diff **empty** (§5) |

Deviations, stated: command 7's `--out` was redirected to `$TEMP` so the run does not create an `eval/` directory
outside the documented write scopes. Commands 9–10 write `provenance.json` to `$TEMP` for the same reason.
I1 §5.2 (scoring against the live DB) is the only declared verify command not executed by the verifier: it needs
the teacher's tunnel and credentials, and the captain re-ran the equivalent on the live server (§7). No other
declared command was skipped.

---

## 2. Damage reconciliation — the authoritative table

The branch circulates two "damage" numbers. Both are true, and they measure different things. Definitions used
here (verified independently, then cross-checked against two existing implementations):

* **Class A — unparseable after the repo's rewrite.** `benchmarks/evaluate_bird.py:15-30` transpiles gold with
  SQLite→PostgreSQL and then applies the naive regex `(\b\w+\b)/(\b\w+\b) -> NULLIF(\2,0)`
  (`evaluate_bird.py:28`). sqlglot already guards every division, so the regex turns
  `x / NULLIF(y,0)` into `x / NULLIF(NULLIF,0)(y, 0)`, which PostgreSQL cannot parse.
* **Class B — parseable but changed.** The same regex also fires inside quoted identifiers and string literals
  that contain `/`; the statement still parses, but no longer means the same thing.
* **Class C — untouched.**

### 2.1 `data/bird_large.json` (n = 241)

| class | count | question ids |
|---|---|---|
| A. unparseable after rewrite | **30** | 62, 85, 116, 125, 169, 219, 298, 317, 371, 402, 415, 416, 417, 487, 506, 598, 639, 701, 716, 743, 760, 775, 818, 834, 835, 1482, 1490, 1525, 1531, 1533 |
| B. parseable, mutated | **3** | 4, 35, 572 |
| C. untouched | **208** | all others |

### 2.2 `data/train_queries.json` (n = 357)

| class | count | question ids |
|---|---|---|
| A. unparseable after rewrite | **14** | 254, 263, 273, 424, 433, 486, 1360, 1376, 1400, 1442, 1454, 1458, 1493, 1528 |
| B. parseable, mutated | **1** | 2 |
| C. untouched | **342** | all others |

Dropped training targets = A + B = **15** (`14 unparseable + 1 mutated`); usable = 357 − 15 = **342**.

### 2.3 Cross-check against the two existing implementations — no set differs

| comparison | bird_large | train_queries |
|---|---|---|
| verifier A vs `research/advtext2sql/reconcile_damage.py` | identical (30) | identical (14) |
| verifier A vs `fixed_eval.gold_damage_report` | identical (30) | identical (14) |
| verifier B vs `reconcile_damage.py` | identical (3) | identical (1) |
| verifier B vs `fixed_eval.gold_silent_corruption_report` | identical (3) | identical (1) |
| `fixed_eval.expected_n_damaged()` | 30 | 14 |

No blocker: every id set agrees across the verifier's literal/identifier comparison, the captain's textual
comparison and `dev_tools`' AST comparison.

### 2.4 What changed in ids 4, 35 and 572 (read from the rewritten SQL)

* **id 4** — gold: `T1.\`Charter School (Y/N)\` = 1` → rewritten: `T1."Charter School (Y / NULLIF(N,0))" = 1`.
  The regex fired **inside a quoted identifier**: the column name itself was rewritten. The statement parses, but
  the name no longer exists in `frpm`, so PostgreSQL raises `UndefinedColumn` — it **cannot execute**.
* **id 35** — same change on the same column in a different query (`T1."Charter School (Y / NULLIF(N,0))"`),
  same outcome: parses, **fails at execution** with `UndefinedColumn`.
* **id 572** — gold title literal `'Integration of Weka and/or RapidMiner into Informatica PowerCenter/Developer'`
  → rewritten literal `'Integration of Weka and / NULLIF(or,0) RapidMiner into Informatica PowerCenter / NULLIF(Developer,0)'`.
  The regex fired **inside a string literal**: this one **does execute**, but it searches for a title that does not
  exist, so the honest answer (the true row) can never be returned.

So "the 3 mutated still execute" is not true: **2 of them fail at execution and 1 executes against a different
literal.** The count 3 and the ceiling are unaffected, but the wording matters — see finding F1.

### 2.5 Ceiling: two independent methods agree

| accounting | offline (parse) | live (execution, captain) |
|---|---|---|
| unparseable / execution failure | 30 class A | 32 = 30 class A + ids 4, 35 (`UndefinedColumn`) |
| executes but answers a different query | 1 (id 572, class B) | 1 (id 572) |
| **questions an honest submission cannot win** | **33** | **33** |
| **honest ceiling** | **208/241 = 86.3 %** | **208/241 = 86.3 %** |
| executable gold | 211 (241 − 30) | **209/241 = 86.72 %** |

The live run (`research/advtext2sql/artifacts/gold_smoke_bird_large.json`, re-summarised by the verifier:
`ok=209, failed=32`; 30 × `psycopg.errors.SyntaxError`, 2 × `psycopg.errors.UndefinedColumn`) confirms the
offline parse-based estimate by execution. Phrase it as **"measured offline by parse and confirmed live by
execution"**, not as an estimate.

### 2.6 What EX/211 means, and why 211, 209 and 208 must never be conflated

Three different denominators exist and they answer three different questions:

| number | definition | value here | max for an honest submission |
|---|---|---|---|
| **EX/241** (`ex_all`) | every question in the answers file | 241 | 208 (86.3 %) |
| **EX/211** (`ex_gold_executable`, offline basis) | questions whose gold survives the *parse* check: 241 − 30 | 211 | 208 (**98.6 %**) |
| **EX/209** (same field, live basis) | questions whose gold *executed* on the live server: 241 − 32 | 209 | 208 (**99.5 %**) |

* **EX/211 is not the number of winnable questions.** It is 241 minus the 30 *parse* failures. The 3 class-B
  items sit inside the 211, and 2 of them cannot execute at all — so even EX/211 is slightly capped by the bug.
* **The honest ceiling is 208/241**, because 33 questions are lost (30 unparseable + 2 execution-failing mutated
  + 1 executing-wrong mutated). `211 − 208 = 3` is a coincidence of the two accountings, not a definition.
* **On the live server the report field prints EX/209**, not EX/211: `gold_ok` is set from executing the gold
  (`dev_tools/fixed_eval.py:782-786`), and execution reveals ids 4 and 35. Quote **EX/241 + EX/209** for the
  honest pair, and label any EX/211 figure as the offline parse-based basis with this note.
* The live **209** and the honest **208** differ by id 572: "executable" is not the same as "winnable".

---

## 3. Numbers recomputed independently by the verifier

Recomputation used the verifier's own inline `python -` scripts (commands 12–14 in §1), not the tested functions
from `dev_tools` or `training`. Cross-checks against those functions are labelled as such in §2.3.

1. **30 unparseable gold ids of `bird_large`** — computed by applying the repo's rewrite (verbatim
   `reconcile_damage.py` logic, re-implemented in the probe) and parsing the result as PostgreSQL:
   30 ids listed in §2.1, plus 3 mutated ids → 33 unwinnable → ceiling 208/241.
2. **14 dropped training targets** — same method on `train_queries.json`: 14 unparseable ids listed in §2.2,
   plus 1 mutated (id 2) = **15 dropped**, usable **342**.
3. **Combined training rows** — the acceptance text expects **685**; the branch measures **684**:
   `290 BIRD train + 394 Ambrosia train = 684`. Line counts of the artifacts produced by command 8:

   ```
   sft_all_train.jsonl          684
   sft_bird_train.jsonl         290
   sft_bird_valid.jsonl          52
   sft_ambrosia_train.jsonl     394
   sft_ambrosia_valid.jsonl      44
   dropped.json                  15
   ```

   **685 is superseded.** It came from the pre-reconciliation split 291/52; dropping the mutated training target
   (id 2, `california_schools`) moves that db from 35 to 34 items and, because of the seeded per-db shuffle,
   shifts every later database: **290/52 and 684** are the reconciled numbers, and `training/build_data.py`
   hard-asserts them (`--self-test` exits non-zero if they move). Numbers derived from 685 are stale (`F2`).
4. **Ambrosia constant floor** — `70.83 % (170/240)`: gold ambiguity is `SQL == "ambiguous"`
   (`benchmarks/evaluate_ambrosia.py:21`); 70 of 240 are ambiguous, so a constant "not ambiguous" classifier is
   correct on 170 → 70.83 %; the constant "ambiguous" classifier scores 29.17 %.

---

## 4. Headline numbers to quote in a report

| # | number | value | command that produces it |
|---|---|---|---|
| 1 | **EX/241** | **42.32 % (102/241)** for the team's frozen predictions (`baseline_team`), measured live | `python -m dev_tools.fixed_eval --predictions <predictions.json> --answers data/bird_large.json --out <report.json> --mode local --db-url <host:port>` (needs `DB_USER`/`DB_PASS`); artifact `research/advtext2sql/artifacts/pred_scores.json` |
| 2 | **EX/211** | denominator 211 on the offline parse basis; the same ledger gives **48.34 % (102/211)**; on the live server the field's denominator is 209, so it prints **EX/209 = 48.80 %** | same command; field `ex_gold_executable` (see §2.6 for which denominator applies) |
| 3 | **Ambrosia constant floor** | **70.83 % (170/240)**, constant-ambiguous 29.17 % | `python -c "import json;r=json.load(open('data/ambrosia_large.json'));a=sum(x['SQL']=='ambiguous' for x in r);print(a,len(r),len(r)-a,100*(len(r)-a)/len(r))"` |
| 4 | **training-row count** | **684** = 290 BIRD + 394 Ambrosia (685 is superseded, §3.3) | `python training/build_data.py --self-test` · or `(Get-Content training/self_test_out/sft_all_train.jsonl \| Measure-Object -Line).Lines` |

For any *new* adapter the same CLI in `--mode fixed` on the live DB is the honest headline; report it as
`EX/241 (EX/209)`, never as a single number.

---

## 5. Scope audit

`git status --porcelain --untracked-files=all`, branch `feat/lora-track`:

```
?? dev_tools/dump_schemas.py      <- appeared 13:21 during the captain's live-DB session
?? dev_tools/paths.py             <- appeared 13:23 during the captain's live-DB session
?? tests/test_data.py             <- I3
?? training/.gitignore            <- I3
?? training/README.md             <- I3
?? training/build_data.py         <- I3
?? training/fixtures/teacher_predictions.json  <- I3
?? training/fixtures/teacher_results.json      <- I3
?? training/teacher_filter.py     <- I3
?? training/train_qlora.py        <- I3
```

Every path is inside the five documented write scopes (`dev_tools/`, `training/`, the tool's `src/`, `tests/`,
`docs/`). No path outside them is untracked or modified. `dev_tools/dump_schemas.py` and `dev_tools/paths.py`
are inside `dev_tools/` but are **not** I1/I2/I3 deliverables: they appeared at 13:21–13:23 while the captain's
tunnel was up and are not reviewed here (in scope, not a violation).

Committed vs `main` (`git diff main`): 11 files, `+4608 −613`, all under `dev_tools/`, `docs/`, the tool's `src/`
and `tests/`.

```
git diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py
-> (empty)
```

`data/bird_large.json`, `data/train_queries.json`, all of `benchmarks/` and the three root entrypoints are
byte-identical to `main`. The verifier wrote no file outside `docs/VERIFICATION.md`; `eval/` was never created.

---

## 6. Behaviour audit

* **English prompt surface.** `prompts.py` — 5 084 chars, **0 Cyrillic characters**; `schema_pruning.py`,
  `sql_utils.py`, `text2sql_implementation.py` also 0. The pre-existing `src/utils.py` keeps a Russian docstring,
  but it is unchanged from `main`, is dead code for the v2 pipeline (nothing imports it) and is not a prompt.
* **Never-raise.** A `Text2SQLGenerator` built with an `llm_client` whose `create()` raises on **every** call
  returned `dict`, not an exception: `{'status': 'error', 'query': None}` (keys `['query','status']`), in 5.52 s.
  The probe used the production import path (`adv_text2sql.mcp_servers…`), a stub `fastmcp` (same technique as
  `tests/test_tool.py:32-50`), and an injected schema so no database was touched. 9 transport attempts were made
  (3 ambiguity + 3 generation + 3 repair) — see F3 for the last 3.

---

## 7. Live-DB fact: the team's 44.81 % does not reproduce (42.32 %)

From the captain's live session (2026-10-23, PostgreSQL 15.15, 900 databases, SSH tunnel to
`lnsigo.mipt.ru:2278`), re-summarised by the verifier from the artifacts:

* Scoring the team's own frozen predictions (`ablation_results_small/feat_best_hybrid_large/bird_query_results.json`,
  extracted from `feat_test`) with the repo's exact rule gives **EX/241 = 42.32 % (102/241)**, **EX/209 = 48.80 %**,
  per difficulty **simple 56.04 / moderate 40.45 / challenging 24.59**. Verified from
  `artifacts/pred_scores.json`: `ex_all = 42.32365145228216`, `ex_exec = 48.803827751196174`, 102 of 241 items
  scored 1.
* Their reported **44.81 % (108/241)** is **6 questions / 2.49 pp higher**. The data file is not the explanation:
  `data/bird_large.json` is byte-identical between `main` and `feat_test`, and all 241 prediction ids are present.
  The misses are concentrated in `UndefinedColumn` on columns their run used (`ncesschool`, `cname`,
  `cardKingdomFoilId`, `baseSetSize`, …), i.e. **database-snapshot drift** between their run and today's server,
  plus one 30 s statement timeout.
* **Consequence:** the comparable frozen baseline for every LoRA comparison is the measured **42.32 % /
  EX209 48.80 %**, not the claimed 44.81 %. Which DB snapshot produced 44.81 % is an **open question for the
  team** and must be asked at the meeting.

Artifacts referenced (read-only for the verifier): `research/advtext2sql/captain-verified-numbers.md` §E,
`research/advtext2sql/artifacts/{gold_smoke_bird_large.json, pred_scores.json, db_schemas.json}`.

---

## 8. What remains unverified

The live DB is up, but these were not executed in this verification and are not claimed:

* **A real training run** — no `trl`/`peft`/GPU on this machine; `train_qlora.py --plan` stops at
  `decision=blocked` by design. No adapter exists yet, so no EX number for one exists.
* **End-to-end tool run through the harness** — the v2 tool has not been executed against the benchmark DB, and
  no v2 prediction file exists; the 42.32 % baseline is the *team's* old tool, not this branch's tool.
* **Ambrosia live scoring** — no tool predictions were produced or scored for Ambrosia; only the constant floor
  (70.83 %) is measured.
* **The verifier did not re-run the 241 gold queries** (captain's instruction; ~2.5 min through the tunnel).
  The live gold-executability result is quoted from the captain's run, whose artifact was re-summarised here.
* **`MAX_RETRIES`, the 30 s per-query timeout and the grading environment** — still unknown; offline tests cannot
  model them.
* **`EX` on a real database at scale** — `local`/`fixed` modes were exercised against an injected executor only.

---

## 9. Findings

**Blocking findings: none (empty list).** The three items below are non-blocking; each names the file and the
required fix. They do not invalidate any number or verdict in this report.

* **F1 — `docs/EVAL.md:73` (documentation, medium).** "Those still parse — so the query executes" is false for
  ids 4 and 35: they parse but fail at execution with `UndefinedColumn`, as the live run now shows (§2.4–2.5).
  Only id 572 executes, against a mutated literal. *Required fix:* reword to "2 fail at execution
  (`UndefinedColumn`, quoted identifier renamed) and 1 executes against a different literal; all 3 are
  unwinnable". The count 3 and the 208/241 ceiling are unchanged.
* **F2 — stale 685 (cross-document, low).** `research/advtext2sql/captain-verified-numbers.md:141` still records
  "291 train / 52 valid and 685 combined", and the t10 acceptance text repeats 685. The reconciled, hard-asserted
  numbers are **290/52 and 684** after dropping mutated training id 2 (§3.3). *Required fix:* update the record
  (and any derived cost/step arithmetic) to 290/52 and 684 when the memory-compaction task runs; treat 685 as
  superseded, not as the current count.
* **F3 — `src/adv_text2sql/mcp_servers/text2sql_tool/src/text2sql_implementation.py:527` (code, low).**
  `_generate_valid_sql` issues the one repair call even when the first generation returned `None` (transport
  failure), so an always-failing client spends 9 attempts instead of 6 (~5.5 s of backoff in the probe, doubled
  for a real outage). *Required fix:* return `None` early when `raw is None` (or document a deliberate double
  budget); the repair prompt has nothing to repair.

---

## 10. Reproduction

```bash
cd research/advtext2sql/repo
python -m pytest tests -q                       # 77 passed
python -m compileall -q src dev_tools training  # clean
python -m dev_tools.fixed_eval --answers data/bird_large.json --out "$TEMP/gold_damage_large.json" --gold-damage
python training/build_data.py --self-test
python training/train_qlora.py --plan --data training/self_test_out --out "$TEMP/lora_plan"
cd ../.. && python research/advtext2sql/reconcile_damage.py
git -C research/advtext2sql/repo status --porcelain --untracked-files=all
git -C research/advtext2sql/repo diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py
```

Interpretation rule for any future report: quote **EX/241 and EX/209 together**, state the **208/241 honest
ceiling**, and never compare a 241-denominator number with a published leaderboard figure without saying which
denominator and which database snapshot produced it.

---

## V2 — re-verification after F3 and T1

Date: **2026-10-23** · Verifier: `qa-review` · Branch `feat/lora-track` @ `220991b`, working tree, no commits.
Reason: the V1 verdict no longer covered the bytes after the F3 repair, the T1 training run and the T2
local-bench work.

**Verdict: FAILED — one blocker (the suite crashes), two non-blocking scope/artifact findings, F1 survives.**
The F3 fix itself is correct and fully verified; the training pipeline is intact; the teacher's harness is
untouched. The current bytes are **not** covered by a passing verdict until F6 is fixed.

### V2.1 Bytes verified (SHA-256)

```
9C2072BC548CF24D10F9C7F45E3809573AC8186F2B24072C674E2F7BF4716776  src/.../text2sql_tool/src/text2sql_implementation.py
880B531AD657E922B8B207A36DF9D38367E7E43465B5E6A87F3BD0A0742CDB9F  tests/test_tool.py
292DC03C891C49FAE99A0A263F69AF91004077074A8A299AE08631D7A826C46C  tests/test_serve.py
2DF9A6C66CE2DE702E4E33D6359DD48BBB8F8EC1E7C181762E37E966A39DB348  training/train_qlora.py
C61AD35344D42C9A42983C169D7BFBF31779BBCFCC2D2BCFC24EB7C02AA29890  training/.gitignore
915CE601D50C711AF62C7EF82DA5205F2833B103853918C997F97FAF76077E3A  training/build_data.py
3F33E430FF87A8B286DD6C215EF5555044D718B9C7DB31DF75B119E4A2FE33F1  docs/EVAL.md
54AC1CDA4D4A4A5DEAC509BA2C7291A1074CA7081CB702805619486098A3EC1E  docs/VERIFICATION.md (before this section)
```

### V2.2 Suite on the current bytes — FAILED (blocker F6)

`python -m pytest tests -q` → **process crash, exit −1073741819 (0xC0000005 access violation)**, reproduced
twice, at `tests/test_tool.py::test_harness_contract_is_unchanged`, inside
`psycopg/_conninfo_attempts.py::_resolve_hostnames` → `socket.getaddrinfo`.

Isolation evidence:

| run | result |
|---|---|
| `pytest tests/test_tool.py -q` | 26 passed in 1.79 s (no crash) |
| `pytest tests -q --deselect tests/test_tool.py::test_harness_contract_is_unchanged` | **137 passed, 1 deselected in 34.73 s** |
| minimal repro outside pytest (import torch + tiny transformers GPT-2, then `psycopg.connect("host=localhost …")`) | same exit −1073741819 |
| torch alone + plain `socket.getaddrinfo`, psycopg to `127.0.0.1` | both fine |

Root cause: `tests/test_tool.py:157` builds the tool with the real DSN
`postgresql+psycopg://user:pass@localhost:5444/fixture`, and `test_harness_contract_is_unchanged`
(`tests/test_tool.py:248-256`) passes `config=None` — production defaults, i.e. `execution_feedback=True` — so
a valid answer makes `_execute_for_feedback` open a **real** connection. `tests/test_serve.py` (new in T2)
loads torch/transformers earlier in the same process; in that combination psycopg's hostname resolution crashes
the interpreter. The test is not hermetic and violates the branch's own "no database, no network" invariant.

Required fix (F6): build that tool with `offline_config()` (which sets `execution_feedback=False`) or inject
`engine=_UnreachableEngine()` — the test only asserts the harness constructor/pipeline shape.
`python -m compileall -q src dev_tools training` is clean (exit 0).

### V2.3 F3 call budget — PASS

Probe: production import path, scripted async client, `ambiguous_mode="birdsafe"`,
`execution_feedback=False` (isolates the LLM budget, no DB), `R = max_llm_retries`.

| scenario | R | calls | sequence | status |
|---|---|---|---|---|
| happy path (valid first try) | 2 | **2** | ambiguity, generation | success |
| one repair (invalid → valid) | 2 | **3** | ambiguity, generation, repair | success |
| all-invalid SQL both times | 2 | **3** | ambiguity, generation, repair | error |
| transport fails every call | 2 | **6** | ambiguity×3, generation×3 — **no repair** | error |
| transport fails every call | 0 | **2** | ambiguity, generation — no repair | error |
| empty replies after gate | 2 | **4** | ambiguity, generation×3 — no repair | error |
| empty replies after gate | 0 | **2** | ambiguity, generation — no repair | error |

The pre-fix 9-call case is gone: a no-usable-text path spends exactly the generation budget `R + 1` (plus the
gate's `R + 1`) and never emits a repair prompt; a repair only follows a generation that returned SQL, and
always-invalid SQL keeps its single budgeted repair (3 calls). Every probe returned a dict (never raised).

### V2.4 Training pipeline — PASS

* `python training/train_qlora.py --plan` → exit 0; `[trl] decision=blocked`, `[gpu] not probed: --plan never imports torch`, provenance written.
* import-only probe with a `sys.meta_path` blocker for torch/trl/transformers/peft/datasets/bitsandbytes/accelerate → module imports, **0** heavy modules loaded.
* `python training/build_data.py --self-test` → exit 0: `dropped=14 mutated=1 usable=342 train/valid=290/52`, `ambrosia built=438 (394/44) combined=684` — identical to V1; `build_data.py` behaviour did not move.

### V2.5 Scope invariants — PASS with two deviations

`git diff main --stat` → 12 files, +4791 −613, all under `.gitignore`, `dev_tools/`, `docs/`, the tool's `src/`
and `tests/`. Harness paths (`benchmarks/`, `data/`, the three root entrypoints) → **empty diff**.

`git status --porcelain` → every tracked modification and untracked path is inside the five documented scopes
**except**:

* `M .gitignore` (repo root) — one added line `runs/` (F4);
* `?? training/out-real/` — 1.6 MB of generated SFT data, **not** gitignored (F5). Every other artifact
  (`runs/`, `self_test_out/`, `.venv*`) is ignored.

### V2.6 Findings

* **F6 (blocker, new): `tests/test_tool.py:157,248-256`** — the harness-contract test opens a real psycopg
  connection with production defaults; in a full-suite run after `tests/test_serve.py` loads the ML stack this
  crashes the interpreter (exit −1073741819). *Required fix:* `offline_config()` or an injected `engine` so the
  test never dials.
* **F4 (non-blocking, new): `.gitignore:12`** — the root `.gitignore` is outside the five documented write
  scopes. *Required fix:* name it in the integration commit (it keeps 161 MB of adapters out of git) or move the
  rule into `training/.gitignore`.
* **F5 (non-blocking, new): `training/.gitignore:2`** — no `out-real/` rule, so `training/out-real/` would be
  swept into `git add -A`. *Required fix:* ignore or delete it before the final commit.
* **F1 (survives, non-blocking): `docs/EVAL.md:73`** — "Those still parse — so the query executes" is still
  wrong for ids 4/35 (live `UndefinedColumn`).
* **F2 (still open, outside the repo): `research/advtext2sql/captain-verified-numbers.md:141`** — 685 is
  superseded by 684.
* **F3 (resolved and verified):** see V2.3.

### V2.7 Round 2 — after the F6 repair (2026-10-23) — supersedes the FAILED verdict above

**Verdict: PASS.** The current bytes pass the suite, keep the F3 budget, keep the training pipeline intact and
leave the teacher's harness untouched. Integration (I4) may seal this tree; F6 is fixed and regression-guarded.

Bytes verified (SHA-256, this round):

```
9C2072BC548CF24D10F9C7F45E3809573AC8186F2B24072C674E2F7BF4716776  src/.../text2sql_tool/src/text2sql_implementation.py  (unchanged since V2.1)
1BBA1DFC9E321F88E3BB8723A121705FA7781102BE658033BC074754DB1F270B  tests/test_tool.py
59FB9D53C206D683D948A1A577E16D1418B6E9064A3A2694B32739D1CE7FF319  tests/conftest.py
2DF9A6C66CE2DE702E4E33D6359DD48BBB8F8EC1E7C181762E37E966A39DB348  training/train_qlora.py  (unchanged since V2.1)
27DE50FFCCC4D0B50942B6A5EDF22F84CA6F50DD3EB3083A7B7CE87FE91A0B02  training/.gitignore
915CE601D50C711AF62C7EF82DA5205F2833B103853918C997F97FAF76077E3A  training/build_data.py  (unchanged since V2.1)
FC1C57AF193053EAC8E0CD370BC1C176EEDAD23DC5B098983BD06228FA9DEDEE  docs/EVAL.md
2CF2AD20F96A7510A271AA95A03D868FCF5C9A5BEBB4975C210E642A3D961CD5  .gitignore
8A8B63EAC458E278952BB4F05A286757EC5F65232C125F69023F759A824E2331  docs/VERIFICATION.md (before this addendum)
```

Product code is byte-identical to V2.1 (`text2sql_implementation.py`, `train_qlora.py`, `build_data.py`); only
test, documentation and ignore bytes moved.

* **Suite — PASS.** `python -m pytest tests -q` → **138 passed in 33.64 s, exit 0** (the V2.2 crash is gone).
  `python -m pytest tests/test_tool.py -q` → **26 passed in 1.08 s, exit 0**. `compileall src dev_tools training` → exit 0.
* **F6 fix — PASS.** An autouse fixture `_forbid_database_engines` (`tests/test_tool.py:242-261`) patches
  `text2sql_implementation.create_engine`, records every attempt and asserts at teardown that none happened
  (it fails, it does not skip). `test_harness_contract_is_unchanged` now builds with `offline_config()` plus
  `_UnreachableEngine()` and asserts `engine.connections == 0` (`tests/test_tool.py:284-310`); `make_tool()`
  defaults to `offline_config()` (`tests/test_tool.py:167`), and the tests that exercise the real defaults ask
  for `Text2SQLConfig()` explicitly. 138 passing tests therefore prove no test created a database engine.
* **F3 call budget — PASS (unchanged):** happy path **2** (gate+generation); one repair **3**; all-invalid SQL
  **3** (gate+generation+one budgeted repair); transport fails R=2 → **6** (gate×3 + generation×3, **no repair**),
  R=0 → **2**; empty replies R=2 → **4**, R=0 → **2**, **no repair**. Every probe returned a dict.
* **Training — PASS.** `train_qlora.py --plan` exit 0 (`decision=blocked`, `--plan never imports torch`);
  import-only probe with a heavy-module blocker loads **0** heavy modules; `build_data.py --self-test` exit 0:
  `dropped=14 mutated=1 usable=342 train/valid=290/52`, combined 684.
* **Scopes — PASS with one recorded extension.** Harness paths (`benchmarks/`, `data/`, three entrypoints) →
  **empty diff**; 12 files changed vs `main`, +4851 −613, all under `dev_tools/`, `docs/`, the tool's `src/`,
  `tests/` and the root `.gitignore`. The root `.gitignore` is the single path outside the five original scopes;
  it now carries an in-file comment declaring the intentional extension (D-001, relative `--out runs/…`) and the
  `runs/` rule at line 18 — recorded here, so the invariant holds as "five scopes + one declared extension".
  `training/out-real/` is now matched by `training/.gitignore:9` (`git check-ignore` confirms), so no generated
  data would enter the commit.

Findings status: **F6 resolved; F4 resolved by record; F5 resolved; F1 resolved** (`docs/EVAL.md` now splits
30 unparseable + 2 `UndefinedColumn` + 1 mutated-but-executing). **F2 remains open outside the repo**
(`captain-verified-numbers.md:141`, 685 vs 684) and is a documentation-only item for the memory-compaction pass.
No new blocking findings; no new non-blocking findings.

---

## V3 — final delivered state

Date: **2026-10-23** · Verifier: `qa-review` · Reason: final audit stamp before handover, after I4c
(`b051560`, `f280ca0`) landed the last measurement and changed bytes that the V2.7 verdict had hashed.

**Verdict: the delivered branch is covered by this verdict.** Final commit
**`f280ca0947b26bc085763596edec45cc4687b9f4`** (`docs(results): evidence-enabled baseline and per-run VRAM`),
19 commits above `main`; patch bundle **761,305 bytes / 19 patches**; suite **138 passed in 37.18 s** on this tree.

### V3.1 Config invariants

| check | result |
|---|---|
| branch / HEAD | `feat/lora-track` @ `f280ca0` |
| `git status --porcelain` | **empty when this check ran on `f280ca0`**; after this section was appended, the only modified path is `docs/VERIFICATION.md` itself — integration must commit that one doc, nothing else is dirty |
| harness diff (`git diff main -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py`) | **empty** |
| teacher repo (`origin`, deeppavlov) | `origin/main` = local `main` = `e812451`; **no `origin/feat/lora-track`** — nothing pushed to the teacher's repository |
| owner's fork (`fork` = `github.com/dadgrand/AdvText2SQL`) | delivery fact, not a byte risk: `fork/feat/lora-track` = `f280ca0`, remote reflog **`update by push` 2026-10-23 18:29:43 +0300** (dadgrand). This post-dates D-001's "nothing pushed" wording (which meant the teacher repo / no credentials); D-001 and the INDEX should record the owner's fork as the delivery channel. |

### V3.2 Patch proof on a fresh clone of `main`

```
clone repo; checkout main (e812451); tag base-main
git apply --check lora-track.patch    -> exit 0
git am --keep-cr lora-track.patch     -> exit 0 (all patches applied)
git rev-list --count base-main..HEAD  -> 19   (= git log --oneline main..HEAD = 19)
```

### V3.3 Hash reconciliation with the V2.7 verified-bytes block

Pre-V3 SHA-256 of this file: `E5834F27D632CF28EB3BA5E303DDEC11E8F183D47B21861152C50A152A700B03`.

| file | V2.7 → final | reason | judgement |
|---|---|---|---|
| `src/.../text2sql_implementation.py` | `9C2072BC…` unchanged | — | no change |
| `tests/conftest.py` | `59FB9D53…` unchanged | — | no change |
| `training/train_qlora.py` | `2DF9A6C6…` unchanged | — | no change |
| `docs/EVAL.md` | `FC1C57AF…` unchanged | — | no change |
| `tests/test_tool.py` | `1BBA1DFC…` → `14D8B699…` | +15 lines in `test_harness_contract_is_unchanged` (exact-bind + no-extra-required assertions), commit `36a1c4d` | **test-only**, no product behaviour |
| `.gitignore` | `2CF2AD20…` → `38AE7CC7…` | +5 lines adding `eval/` and its comment, commit `5bb4f97` | **config-only**; `git check-ignore eval/…` passes |
| `training/.gitignore` | `27DE50FF…` → `97E94AD4…` | ignore-rule content only; the file was **untracked at V2.7**, so the exact pre-commit bytes are not recoverable from history | **config-only**; current rules verified with `git check-ignore` (`out/`, `self_test_out/`, `runs/`, `out-real/`, `.venv*`, `*.log` all match) |
| `training/build_data.py` | `915CE601…` → `3471CBA7…` | 4-line module-**docstring** change: stale `usable=343, 291/52, 685` corrected to `342, 290/52, 684`, commit `b051560` | **cosmetic**; no behaviour change (the executable self-test still asserts 14/1/342 and 290/52) |
| `docs/VERIFICATION.md` | `8A8B63EA…` → `E5834F27…` | V2.7 addendum appended (this section changes it again) | **docs-only** |

Four of the nine hashed files changed; every change is comment/docstring/ignore/test/doc-only. No product
behaviour changed under a V2.7 hash, so no re-verification gap exists.

### V3.4 Drift outside the V2.7 block (for completeness)

`dev_tools/fixed_eval.py` (+21 lines: retry once when a dropped tunnel raises `OperationalError`, `c25e20b`)
and `dev_tools/run_benchmark.py` (+17 lines: `--benchmark {bird,ambrosia}`, `4db5d23`) changed behaviourally
after the V2.7 document. Both are covered by the t24 review verdict (suite + claims audit at `e4256ed`) and by
this V3 run; the suite is green. Low observation: the retry-once path has no dedicated regression test (it is
defensive error handling around a transport failure); not a blocker.

### V3.5 CHANGELOG final-state claims

* **Suite count** — `CHANGELOG.md:25` says the working repository gives `138 passed`; measured **138 passed**
  (37.18 s) on the final tree. ✓
* **"Inconclusive at n = 241"** — the table row at `CHANGELOG.md:208` reports **+4.15 pp, CI [−0.83, +9.54],
  inconclusive at n = 241**, and the pre-registered rule (`b − c ≥ 6`, `p < 0.05`, CI excludes 0) is stated
  immediately below at `CHANGELOG.md:212` — visible next to the number, not paraphrased away. ✓ The
  E3-vs-E1-fair comparison is explicitly labelled "not an adapter measurement" (`CHANGELOG.md:237`). ✓

**V3 verdict: every byte that matters is unchanged or cosmetically changed since V2.7; the delivered branch at
`f280ca0` is covered by this verdict.** No blocker. One delivery note (owner's fork push) and one low
observation (no dedicated retry test) are recorded above. This section is uncommitted by design (no commits were
allowed in this task); integration must commit `docs/VERIFICATION.md` so the delivered tip carries the verdict.

---

# V4 — round-2 pre-seal artifacts audit (branch `feat/round2`, 2026-10-29)

Scope: the round-2 commits on `feat/round2` (created from the sealed round-1 tip `f4d3d4a`), verified before the
seal's final push. This section is written by the integration pass; it cites the four code commits that precede
the docs commit carrying it, and will be extended with the final seal commit sha when t7 lands.

## V4.1 Commands executed

| command | result |
|---|---|
| `python research/advtext2sql/audit_round2.py` | **161/161 checks reproduce, 0 findings** (exit 0); report `research/advtext2sql/AUDIT_ROUND2.json` |
| `python -m pytest tests -q` | **236 passed in 60.88 s** (offline: no DB, no network, no GPU) |
| `git diff main --stat -- benchmarks data bird_benchmark.py ambrosia_benchmark.py bird_evaluate_only.py` | empty — the teacher-owned harness, data and root entrypoints are untouched |
| `git rev-parse --short feat/lora-track` | `f4d3d4a` — round 1's branch is untouched |

Pinned audit identity: `audit_round2.py` sha256 `775b7df07352db784c8195a8dfdb2203d2791b1935715b91e935789353e9b0cd`;
`AUDIT_ROUND2.json` sha256 `052577003816a39f3b8819f0fe3cf68ca96b4e74eb459ec5e7f545fb814a67e7`.

## V4.2 What the audit covers

Independent re-implementation (no `dev_tools` import) of: the four 2×2 cells and their EX/209 denominators
(A 51/241, B 84/241, C 61/241, D 93/241), the five paired blocks plus D vs the frozen 42.32 % file, the
difference-in-differences interaction, the 208/241 ceiling derivation (qid 4/35 unexecutable, qid 572
executable-but-mutated), the Ambrosia floors/operating points and the regenerated `cells.base_strict` block,
the feature × model matrix cross-check (`findings: []`), the natural-Ambrosia-arm arithmetic (480 = 146 + 334,
432/48, 722, 138 steps, ~40 min), and the decoder arm (N 39/241 / G 44/241 with both EX/209 denominators,
unexecutable 62 → 57, fenced completions 241/241 → 0/241, grammar acceptance 241/241, 55 of 62 schema-grounding
errors, paired b=7, c=2, +2.07 pp, p=0.1797).

The one finding this audit raised pre-seal — a stale `cells.base_strict` block in
`runs/t2/scores/ambrosia-calibration.json` caused by a loop-variable leak in the artifact generator — was fixed
at source (generator + pre-write self-check), the artifact was regenerated CPU-only, and the audit is green
(0 findings). The run artifacts stay out of the patch (`runs/` is ignored); this document and `docs/REPORT.md`
§11 carry the numbers.

## V4.3 Commits in this round (before the final scale commit)

| bucket | commit | subject |
|---|---|---|
| tool | `4ea7d4f` | `feat(tool): round-2 recorder, grammar proxy, procedure arms and thinking mode` |
| eval | `98e6a22` | `feat(eval): feature x model matrix, grammar checker and decoder report` |
| training | `70c2fe6` | `feat(training): scale-up build with group split and natural Ambrosia recipe` |
| tests | `1689af2` | `test: pin round-2 recorder, matrix, grammar, procedure and scale-up behaviour` |

The docs commit carrying this section follows them. Commit now / push later is the captain's 2026-10-29
decision: nine hours of uncommitted multi-agent work was the largest avoidable risk; nothing is pushed until t7
lands and the patch is regenerated on a fresh `main` clone.

## V4.4 Open item at this checkpoint

**t7 (scale-adapter measurement) is not complete.** No scale number is invented or implied. Placeholder policy if
t7 cannot be measured before the deadline: ship with the explicit sentence "scale-up measured dataset 5,612 rows /
71 DBs, adapter trained 3 epochs, measurement not completed in this round", and keep the dataset work in the
patch. The final seal commits the scale results, the refreshed `ROUND2_RESULTS.ru.md`, the regenerated patch and
the refreshed suite/audit counts, and records the final commit sha here.
