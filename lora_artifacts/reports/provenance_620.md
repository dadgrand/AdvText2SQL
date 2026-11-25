# Provenance of 620/811 = 76.45 %: it is the 7B scale adapter, not 14B

Decision (from artifacts, not memory): **620/811 = 76.45 % belongs to the 7B scale adapter**, run
`ood-scale7b-v2` (adapter sha256 `71abcde86a45a351ebf3150b3d4daa510ec74d43c7f9b88c8e9ec90e4b0a8fed`,
base model `qwen2.5-coder-7b`, 4-bit, evidence on, 876 OOD questions). It is **not** a 14B QLoRA
result. The 14B number on the paired OOD subset is **88/120 = 73.33 %** (EX over executable gold
88/107 = 82.24 %), and **135/216 = 62.50 %** stays the historical 14B dev reference.

## The two files, side by side

| artifact | run id | base model | adapter sha256 | questions | OOD ids | what it can measure |
|---|---|---|---|---|---|---|
| `repo/runs/ood/ood-scale7b-v2/query_results.json` | `ood-scale7b-v2` | `qwen2.5-coder-7b` | **`71abcde8…`** | 876 (`ood_heldout.json`) | 876/876 | OOD-876 → **620/811** |
| `repo/runs/t2/14b-ev/query_results.json` | `r2-14b-ev` | `qwen2.5-coder-14b` | `5833a1cc…` | 241 (`data/bird_large.json`) | **0** | dev only → 130/241 historical |

Evidence for the table:

* `repo/runs/ood/ood-scale7b-v2/run.json` — `run_id=ood-scale7b-v2`, `n_questions=876`,
  `llm_model=scale7b-v2`, `adapter_id=/root/work/repo/runs/a100-scale/adapter`,
  `adapter_sha256=71abcde86a45a351ebf3150b3d4daa510ec74d43c7f9b88c8e9ec90e4b0a8fed`,
  `model_id=/root/models/qwen2.5-coder-7b`, `load_in_4bit=true`, `use_evidence=true`.
* `repo/runs/t2/14b-ev/run.json` — `run_id=r2-14b-ev`, `n_questions=241`,
  `query_file=data/bird_large.json`, `llm_model=qwen14b+scale`,
  `adapter_id=/root/work/repo/runs/a100-14b/adapter`,
  `adapter_sha256=5833a1cce065f1865173265b1713a14ba5aad2a66e23c88d994de6c83780e0e1`,
  `model_id=/root/models/qwen2.5-coder-14b`.
* The 14B file cannot produce an OOD number: its 241 ids are the dev ids (`4…1533`), while the OOD
  ids are `ood:<db>:<n>`; the intersection is empty, and scoring that file on OOD-876 yields
  **0/876 predictions present** (measured in t11 and again in t32's report).
* The frozen OOD ledger `repo/runs/ood/scores/ood-scale7b-v2.json` records `ex_all = 620/876`,
  `ex_gold_executable = 620/811`, `n_gold_unexecutable = 65`, `n_pred_unexecutable = 50`, with the
  same 65 unexecutable ids the t11 harness reproduced id-by-id.

## What `docs/REPORT.md` section 19 says

`repo/docs/REPORT.md` §19 ("Out-of-distribution check …") lists exactly these arms:

| arm | rows | EX | EX over executable |
|---|---:|---:|---:|
| 7B scale adapter, full held-out set (`71abcde8…`) | 876 | 620/876 = 70.78 % | **620/811 = 76.45 %** |
| 7B scale adapter, same 120 as the base arm | 120 | 83/120 = 69.17 % | 83/107 = 77.57 % |
| 7B base, no adapter | 120 | 86/120 = 71.67 % | 86/107 = 80.37 % |
| **14B scale adapter** (`5833a1cc…`, same 120) | 120 | **88/120 = 73.33 %** | 88/107 = 82.24 % |

So the report itself has always attributed 620/811 to the 7B adapter; the interim summary lost that
attribution. The reviewer's "14B was 88/120" is confirmed by artifact: rescoring
`repo/runs/t2/ood-14b-120-query_results.json` against the 120-question gold with the historical
evaluator reproduces **88/120 = 73.33 % (EX/107 = 82.24 %)**.

## The other 14B number that is sometimes nearby

`cluster/preds/ood_14b.json` — a **different** 14B run (the project's 1-epoch night run) — scores
**95/120 = 79.17 %** (EX/107 = 95/107), and only 34 of its 120 predictions are byte-identical to the
historical 14B file. That 79.17 % is the number the interim report already marks as contaminated
(old corpus, 1 098 OOD rows in training). It is not the historical 14B reference and must not be
quoted as one.

## Correction applied to `INTERIM_REPORT.md`

The OOD row of §2 now reads:

> `отложенные базы | 876 (исполнимых 811) | 620/811 = 76,45 % — **7B scale adapter** (run ood-scale7b-v2, adapter sha256 71abcde8…), не 14B; 14B на тех же 120 вопросах — 88/120 = 73,33 % (EX/107 = 82,24 %) | только как исторический 7B-результат; OOD-числа контаминированы`

`135/216 = 62,50 %` in §1 and §2 is unchanged and remains the historical 14B dev reference
(drift-clean denominator; its training corpus contained 1 018 OOD rows per t9, which contaminates
its OOD numbers, not its dev gold).

## Standing rules (so this cannot recur)

1. An OOD number is quoted with the run id **and** the adapter hash; 620/811 → `ood-scale7b-v2` /
   `71abcde8…`, never "14B".
2. The historical 14B is `5833a1cc…`: dev 135/216 (62.50 %, historical reference) and OOD-120
   88/120 (73.33 %) — both contaminated for OOD by the old corpus, dev by nothing.
3. The only clean OOD numbers in this project are the t4 batch-1 predictions of arms A and B:
   A 574/811 = 70.78 %, B 638/811 = 78.67 % (paired +7.89 pp, p = 1.17e-07), trained on the t9
   clean split with the split-manifest hash recorded in their reports.
