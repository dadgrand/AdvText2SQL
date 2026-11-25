# Provenance audit — one-epoch runs and t1 balance (t35)

Corpus: `cluster/sft_all.jsonl`, 6,866 rows, sha256 `64077299e8bc3f2518a16b2534a878348d93cfed9d1904953fff8306140449ac`.
Split manifest sha256 `65e6ca1c47060b5c08d7fb7999ff88ee4d848d659e24dafa08f7c7ad2a6cbb02`.

## One-epoch runs (both trained on sft_all)

| run | base | epochs | steps | rows | contaminated rows | affected metrics |
|---|---|---:|---:|---:|---|---|
| 7b-2ep | Qwen2.5-Coder-7B-Instruct | 2 | 3,432 | 6,866 | 1098 ood_reserved / 0 internal_valid / 0 external_test | dev-241 102/241 and 102/209 unaffected; OOD-120 83/120 contaminated |
| 14b-1ep | Qwen2.5-Coder-14B-Instruct | 1 | 1,717 | 6,866 | 1098 ood_reserved / 0 internal_valid / 0 external_test | dev-241 130/241 and 130/209 unaffected; OOD-120 88/120 contaminated |

Dev-217: 0 training rows on any of the eight internal_valid dev databases and no dev question id in the corpus — unaffected.
OOD-120: 120/120 questions use a database with training rows — contaminated. OOD-876 has the same status (876/876).

## Second, independent caveat: accumulation

Both runs predate the t20 gradient-accumulation fix. They averaged per-microbatch means, while the fixed loop uses sum(NLL) / total valid targets. t20 measured naive-vs-fused relative L2 **6.06e-01** (token-weighted 8.20e-02; `numerics_report.json`). Their training dynamics/curves are therefore not comparable to the new arms even where the data is clean.

## What may still be quoted

- **Quotable:** dev-241 in-domain evaluations of these adapters (102/241=42.32%, 102/209=48.80%; 130/241=53.94%, 130/209=62.20%) as historical adapter results — no dev schema was trained.
- **Mark contaminated:** every OOD-120 number from these runs (83/120=69.17%, 88/120=73.33%), and any OOD-876 result from the same adapters.
- **Mark uncomparable:** training dynamics, step counts and supervised-token budgets of the one-epoch runs versus the new token-weighted arms (accumulation caveat above).

## t1 balance (`cluster/sft_all_v2.jsonl`)

- 5,110 real + 2,815 synthetic = 7,925; matches the source pools (5,110 real / 2,815 synthetic).
- Real rows: no duplicates, no missing, no extra ids versus the unified train source.
- Synthetic: 1,179 ids appear twice (second phrasings restored; no new ids introduced) and 457 ids appear once. The literal expectation "every synthetic id appears twice" is therefore not met; the 457 ids are legitimate single-phrasing source pairs, listed in the JSON, not silently dropped rows.
- Restored rows: 1,179 second rows, all ids already present in the old corpus; pair integrity problems: 0.
