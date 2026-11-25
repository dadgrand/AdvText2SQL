# t12 controlled generation pilot (roles A/B/C + external screen)

Artifacts: `pilot.jsonl` (every candidate with its gate trail) and `pilot_report.json`.
Nothing in this directory modifies the corpus; the corpus and the frozen split manifest are
read-only inputs.

## Gate order (hardened by the t6 audit, fixed before the first candidate)

| Step | Check | Where |
|---|---|---|
| 1 | mechanical LIMIT: every `LIMIT n` must be named in the question (digits, a spelled-out number, or single-answer wording when `n = 1`) | `gate.limit_check` |
| 2 | bidirectional literals: SQL literals must appear in the question AND numbers / quoted strings / dates stated in the question must appear in the SQL | `gate.literal_check` |
| 3 | one read-only PostgreSQL SELECT, parsed with sqlglot, executed on the live server, 1-200 rows, byte-identical result on a second run | `gate.parse_select`, `gate.execute` |
| 4 | G1-G8 semantic judge, verdicts accept / repair / reject only | `judge_pass.py` |

A candidate that fails step n never reaches step n+1. Reject counts per step are in the report.

### Equivalence rules used by the literal check

These are the only allowances; anything else counts as a missing literal.

- numbers: `88,000` == `88000`; a spelled-out number (`three`, `twenty`) matches its value; a cap
  number is exempt here because step 1 already polices it;
- strings: matched case-insensitively after whitespace folding; `LIKE 'New Haven%'` matches a
  question that says "in New Haven" (wildcards are stripped);
- dates: `March 1, 2020` matches `'2020-03-01'`, and `March 2020` matches any
  `'2020-03-dd'` literal or a `2020-03` prefix in the SQL text; date components are not
  double-counted as bare numbers;
- a SQL string literal the student can already read in the schema view shown with the task (a JSON
  key, a code visible in the sample values) does not have to be repeated in the question.

## Roles (playbook sections 4A-4C, called separately)

- **A - case author**: sees the schema view (tables, types, nullability, keys, row counts, two
  sample rows), the glossary, the target skill and difficulty. Returns `intent`, `public_facts`,
  `evidence`, `expected_columns`, `comparison`, `reference_sql`, `confounders`.
- **B - question author**: sees only `intent`, `public_facts`, glossary, evidence and style. Never
  sees SQL or results. Returns `needs_facts` instead of inventing a fact.
- **C - independent solver**: sees only the schema, the question and the evidence. Returns
  `sql` plus `ok | ambiguous | unsupported`.

A/C disagreement (different result bags, or C says ambiguous/unsupported) is stored as
`adjudication_required` with both SQLs and both result fingerprints. It is never resolved
silently and never counted as a hard reject.

## Families

- 52 train families of `datasets/splits/schema_family_split.json`, executed against their live
  databases on the benchmark server (PostgreSQL 15.15 through the SSH tunnel).
- 4 pilot-local PostgreSQL-native families (`pilot_billing`, `pilot_staff`, `pilot_orders`,
  `pilot_catalog`) with seeded fixtures that contain the confounders the playbook asks for
  (tie at the cut, NULL parent, empty JSON array, JSON null value, a product attached to a branch
  node, timestamps that cross a local date boundary). They exist because the train schemas are
  SQLite-translated (`integer` / `real` / `text`) and cannot express as-of state, timestamptz
  periods, JSONB or arrays. Their cases run on throw-away `CREATE TEMP TABLE` fixtures.

## Reproduce

```
python3 datasets/pilot/test_gate.py                 # gate regression, 9 cases
python3 datasets/pilot/pilot_families.py            # rebuild family_context.json
python3 datasets/pilot/run_pilot.py --internal 300 --workers 4
python3 datasets/pilot/screen_external.py --workers 6
python3 datasets/pilot/judge_pass.py --workers 3 --batch 8
python3 datasets/pilot/build_pilot.py
python3 datasets/pilot/verify_t12.py                # acceptance checks
```

`progress.json` is rewritten every 25 candidates: counts by skill, hard share, evidence share,
rejects by gate step. It counts the **generation stage before judging** (`pending_judge` =
mechanically clean and queued for the G1-G8 pass); the final verdicts are in
`judge_verdicts.jsonl` and `pilot_report.json`. All runs are idempotent by `sample_id`.
