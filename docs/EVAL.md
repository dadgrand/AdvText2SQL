# Evaluation layer — `dev_tools/fixed_eval.py`

Offline, non-invasive measurement for the BIRD benchmark used in this repo.
Nothing here is imported by the shipped harness: `benchmarks/`, `data/` and the
root entrypoints are untouched, and every unit test runs without a database,
without network access and without an LLM key.

Measured against **`sqlglot 30.23.0`** (record the version next to every number;
`sqlglot` is unpinned in `pyproject.toml`, and all statement counts below depend
on its transpile behaviour).

---

## 1. The four numbers, and which one to report where

Every run is described by **four** numbers that belong together. Quoting one of
them alone is how a 12 % ceiling gets mistaken for a weak model.

| # | number | field in the JSON report | what it is for |
|---|---|---|---|
| 1 | **EX/241** (`mode=local`) | `ex_all` | The number the *shipped* evaluator prints. Quote it **only** next to the fixed numbers, or when comparing against a previous run of `benchmarks/evaluate_bird.py`. Never present it as "how good the model is". |
| 2 | **EX/211** (`mode=local`) | `ex_gold_executable` | The same run over the questions whose gold SQL actually executes. This is the only number from the shipped harness that is not capped by its own bug. |
| 3 | **EX/241 + per-difficulty** (`mode=fixed`) | `ex_all`, `per_difficulty` (each with a Wilson 95 % CI) | The honest headline for the model, with the harness bug removed. Use it for the LoRA track and for any comparison with published BIRD numbers (after the PostgreSQL correction). |
| 4 | **paired block vs the frozen baseline** | `paired` (`b`, `c`, `mcnemar_exact_p`, `bootstrap95`) | The only statistically valid way to say "LoRA is better than the baseline". |

`soft_f1_mean` (mini_dev cell-level F1) is the secondary metric: continuous, so
it has more power than EX for the same number of questions.

### Why EX/241 and EX/211 must always be quoted together

The shipped `benchmarks/evaluate_bird.py` destroys **30 of the 241**
`bird_large` gold queries before executing them (and **2 of 22** in
`bird_small`). Those questions score 0 for **every** submission, so:

* a *perfect* PostgreSQL submission scores **208/241 = 86.3 %**, not 100 %
  (30 unexecutable + 3 silently mutated, see §2);
* therefore any EX measured over 241 under-reports: for a submission that does
  not accidentally emit the corrupted text, `EX/211 = EX/241 × 241/211`, i.e.
  a model at 70.0 % on 241 is at 80.0 % on the 211 questions that can actually
  be answered;
* a 2-point difference between two runs on 241 is 2.3 points of the real signal,
  and the *absolute* number is not comparable with a leaderboard figure at all;
* the 30 dead ids are **not random**: they are the questions with a division in
  the gold SQL, and they cluster in `superhero`, `card_games`,
  `codebase_community`, `toxicology` and `california_schools`. Per-database and
  per-difficulty figures are skewed, which is why the report also carries
  per-difficulty intervals and per-database counts.

Report both, always, e.g.:

> LoRA v1: **EX/241 = 43.2 % (104/241, 95 % CI 37.2–49.4)**;
> **EX/211 = 49.3 % (104/211)**. Baseline: EX/241 = 39.4 %, EX/211 = 45.0 %.
> Paired: b = 14, c = 5, exact McNemar p = 0.041, ΔEX 95 % CI [0.4 pp, 7.9 pp].

---

## 2. What is wrong with the shipped evaluator (measured)

`benchmarks/evaluate_bird.py`:

1. **Line 28** applies `re.sub(r"(\b\w+\b)\s*/\s*(\b\w+\b)", r"\1 / NULLIF(\2,0)", …)`
   *after* `sqlglot.transpile(…, read="sqlite", write="postgres")`. sqlglot
   already emits `NULLIF(<denominator>, 0)` for every division (the SQLite parser
   marks divisions as "safe"; the PostgreSQL generator guards them). The regex
   then eats the `NULLIF` token and produces
   `… / NULLIF(NULLIF,0)(COUNT(T1.id), 0)`, which PostgreSQL cannot parse. The
   `SQLAlchemyError` branch (lines 102–115) records `score = 0` and moves on, so
   the failure is silent. **30/241** `bird_large` (12.4 %) and **2/22**
   `bird_small` gold queries are dead this way; 14/357 training targets carry the
   same damage.
2. The same regex also fires **inside string literals and quoted identifiers**
   that contain a slash: ids 4 and 35 (`"Charter School (Y/N)"`) and id 572
   (`'… Weka and/or RapidMiner …'`). Those still parse — so the query executes —
   but it no longer means the same thing, and every submission is compared
   against the mutated version. **3/241**; reproduced by
   `gold_silent_corruption_report`.
3. **Line 78** (prediction transpilation) is commented out, so a model that
   imitates BIRD's SQLite-style gold (backticked identifiers, `IIF`,
   `STRFTIME`, `IFNULL`) is punished for being stylistically correct: 15/241 gold
   queries are not valid PostgreSQL verbatim and 24 use SQLite-only functions.
4. It reports one number: no CI, no per-difficulty interval, no secondary
   metric, no paired test. At n = 241 a 2-point difference is noise.

Net effect for an honest, perfect PostgreSQL submission in `mode=local`:
**208/241 = 86.3 %** — 30 questions cannot execute, 3 more are mutated. The
`fixed` mode removes all of it: the same submission scores 241/241.

---

## 3. The three modes

| mode | gold rewrite | prediction rewrite | purpose |
|---|---|---|---|
| `local` | `repo_rewrite` (verbatim copy of the shipped regex) | none | exact comparability with `benchmarks/evaluate_bird.py` |
| `fixed` | `sqlite_to_postgres_safe` (AST) | none | removes the harness ceiling; isolate model quality |
| `normalized` | `sqlite_to_postgres_safe` | `sqlite_to_postgres_safe` | removes the dialect penalty too; the style-neutral number |

`sqlite_to_postgres_safe` parses with `read="sqlite"`, walks `exp.Div` nodes and
wraps **only** column/expression denominators in `NULLIF(<denominator>, 0)`, then
generates PostgreSQL:

```text
SELECT T1.Price / T2.Amount                  -> SELECT T1.Price / NULLIF(T2.Amount, 0)
… * 100 / COUNT(T1.id)                       -> … * 100 / NULLIF(COUNT(T1.id), 0)
T2.`School Code`                             -> T2."School Code"   (case preserved, backticks -> ANSI)
T1.Price / NULLIF(T2.Amount, 0)              -> unchanged (idempotent, exactly one guard)
```

It never uses a regex, never splits a qualified name, and every one of the 30
damaged gold queries becomes parseable again (asserted by the tests).

Execution is always **injected**: `score_predictions(..., execute=fn)` where
`fn(sql, db_id) -> list[tuple]`. That is what lets the whole test suite run
in-process. The CLI builds a PostgreSQL executor from `--db-url`
(`$BENCHMARK_DB_URL`) plus `DB_USER` / `DB_PASS`.

---

## 4. Statistics (definitions and hand checks)

* **Wilson 95 % CI** for a proportion, `z = 1.959964`, no continuity correction:
  `wilson_ci(11, 22) = (0.30718, 0.69282)`. Used for overall and
  per-difficulty EX. Preferred over the normal approximation because EX at 241
  questions sits near the middle but per-difficulty subsets get small.
* **Exact McNemar** on the paired ledger: `b` = current right / baseline wrong,
  `c` = current wrong / baseline right,
  `p = min(1, 2 · Σ_{k≤min(b,c)} C(b+c, k) · 0.5^(b+c))`.
  `mcnemar_exact_p(6, 0) = 0.03125`. Exact (binomial), not the χ² approximation,
  because `b + c` is often < 25.
* **Paired bootstrap**: resample **question ids** with replacement, 10 000
  resamples, `seed = 0` by default; percentile 95 % CI of the mean difference.
  Deterministic for a fixed seed, so a reported interval can be reproduced
  exactly.
* **soft-F1** (mini_dev cell-level): rows are aligned by position; inside a row
  the cells form a set (NULL cells ignored), `tp` = cells in both, `fp` = only in
  the prediction, `fn` = only in gold; `F1 = 2PR/(P+R)`. The documented worked
  example is reproduced exactly: the 3×2 tables from the mini_dev README give
  `tp=4, fp=1, fn=1 → F1 = 0.8`.

Pre-registered decision rule (from the plan, §7 P5): claim "LoRA is better"
**only if** `b − c ≥ 6` **and** exact McNemar `p < 0.05` **and** the paired
bootstrap CI for ΔEX excludes 0. Claim "not worse" only if the CI excludes
−5 pp. Otherwise report "inconclusive at n = 241".

---

## 5. Commands

All commands run from `research/advtext2sql/repo` on branch `feat/lora-track`.

### 5.1 Gold-damage audit — no database needed

```bash
python -m dev_tools.fixed_eval \
  --answers data/bird_large.json \
  --out eval/gold_damage_large.json \
  --gold-damage
```

Prints and stores: `n_damaged` (30 for `bird_large`, 2 for `bird_small`), the
expected count for the installed sqlglot version, the per-kind breakdown, every
damaged `question_id` with `gold`, `rewritten`, `parse_error`, the regex evidence
and the `sqlite_to_postgres_safe` output that reparses, plus
`silently_corrupted_ids` (3 for `bird_large`). Exit code 0 even when damage is
found — this is an audit, not a gate.

### 5.2 Scoring a predictions file

```bash
export DB_USER=... DB_PASS=...
export BENCHMARK_DB_URL=host:port          # or pass --db-url

python -m dev_tools.fixed_eval \
  --predictions query_results.json \
  --answers data/bird_large.json \
  --out eval/report_fixed.json \
  --mode fixed \
  --baseline eval/baseline_results.json
```

* `--mode local` — reproduce the shipped evaluator (for the "before" column);
* `--mode fixed` — the honest headline;
* `--mode normalized` — style-neutral (safe rewrite on predictions too);
* `--baseline` — adds the paired block (`b`, `c`, exact McNemar p, bootstrap CI);
* `--n-boot` (default 10 000) and `--seed` (default 0) control the bootstrap;
* `--quiet` suppresses the text summary, the JSON is always written.

The JSON report contains: `ex_all` (EX/241 with Wilson CI), `ex_gold_executable`
(EX/211), `n_gold_unexecutable` + ids + reasons, `n_pred_unexecutable`,
`n_missing_predictions`, `per_difficulty` (n, correct, EX, Wilson CI),
`per_database` (counts only — do not compute significance per database),
`soft_f1_mean`, `soft_f1_mean_all`, `paired`, and a `per_question` ledger.

`soft_f1_mean` is the mean over the questions where *both* sides executed;
`soft_f1_mean_all` counts every failure as 0 as well (ambiguous items are
excluded from both), so it can be compared directly with EX.

### 5.3 No database at all (what the tests do)

```python
from dev_tools.fixed_eval import score_predictions

def execute(sql, db_id):
    ...                       # returns list[tuple]; raise to mean "this query failed"
    return rows

report = score_predictions(predictions, "data/bird_large.json", mode="fixed", execute=execute)
```

### 5.4 Tests and syntax check

```bash
python -m pytest tests/test_eval.py -q
python -m compileall -q dev_tools
```

36 tests, all in-process: the 30/241 and 2/22 damage reproduction, the 3 silent
mutations, the Wilson and McNemar hand checks, the soft-F1 worked example, the
three scoring modes against a fake executor, the paired block (b=6, c=0 →
p=0.031), the CLI (`--gold-damage` as a module, the full scoring path with an
injected executor, `--help`, the "no database" usage error), and the
deterministic stub LLM (`dev_tools/stub_llm.py`).

---

## 6. Honest limitations

* **`parse-failure ⇒ execution-failure`, but not the converse.** The audit
  decides "gold cannot execute" by *failing to parse* the rewritten SQL as
  PostgreSQL; that direction is sound. The reverse is not guaranteed: sqlglot
  accepts a few constructs PostgreSQL rejects (e.g. `f(x).field`), so the
  executable set must be confirmed once against the real database. The 30/241
  and 2/22 figures are the *damaged* set; the exact executable denominator can
  only go down, never up.
* **No per-query timeout.** The official harness runs each query under a 30 s
  `func_timeout`; this layer does not (a timeout needs the real driver).
  Exceptions still score 0.
* **Missing predictions are scored 0** and counted in `n_missing_predictions`;
  the shipped code raises `KeyError` mid-run instead. That is a deliberate
  deviation: a broken predictions file must not destroy the report.
* **EX is set equality of row tuples**: order-blind *and* duplicate-blind, which
  is exactly BIRD's `set(predicted_res) == set(ground_truth_res)`.
* **soft-F1 aligns rows by position** (mini_dev's table is positional);
  `order_insensitive=True` sorts both tables first if a run-order-free variant is
  wanted. It is a secondary metric, not the official leaderboard one.
* **Bootstrap** is a percentile interval over question ids, seeded; it is not a
  BCa interval and it is not adjusted for the 30 dead questions (use `mode=fixed`
  for that).
* **`sqlglot` version sensitivity**: the 30 / 2 / 3 counts are tied to
  `30.23.0` and are re-measured on every audit run; `n_damaged_expected` in the
  JSON tells you whether the installed version matches the recorded measurement.
* The `local` mode's rewrite is a **copy**, not an import, of
  `benchmarks/evaluate_bird.py:15-30`; if the shipped evaluator is ever fixed,
  `repo_rewrite` must be updated in the same commit (the test
  `test_repo_rewrite_is_the_shipped_defect` pins the current behaviour).
