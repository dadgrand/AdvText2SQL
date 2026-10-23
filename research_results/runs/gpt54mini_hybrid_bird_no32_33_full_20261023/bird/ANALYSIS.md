# Full BIRD hybrid without FEAT_32/33

Completed successfully on all 241 questions and eight databases. The prediction
IDs match the GPT baseline, original GPT hybrid, and historical best hybrid.
Only FEAT_32/33 differ from the original fixed-hybrid feature configuration;
the model and other runner settings were retained.

| Configuration | Correct | Accuracy | Ambiguous |
| --- | ---: | ---: | ---: |
| GPT baseline | 99/241 | 41.08% | 0 |
| GPT original fixed hybrid | 93/241 | 38.59% | 50 |
| GPT hybrid without FEAT_32/33, full run | 113/241 | 46.89% | 0 |
| Historical best hybrid, different model | 108/241 | 44.81% | 3 |

The full run gains 5.81 percentage points over the GPT baseline, 8.30 over the
original GPT hybrid, and 2.08 over historical best hybrid. The first two are
within-model comparisons; the last also changes the model and does not isolate
a feature effect. These are single-run observations on this subset, not a
claim of superiority on unseen databases.

Compared with GPT baseline, 31 failures become correct and 17 successes become
incorrect, leaving 193 decisions unchanged. Compared with the original GPT
hybrid, 38 failures become correct and 18 successes become incorrect.

## Difficulty

| Difficulty | GPT baseline | Hybrid without FEAT_32/33 |
| --- | ---: | ---: |
| Simple | 57.14% | 63.74% |
| Moderate | 33.71% | 33.71% |
| Challenging | 27.87% | 40.98% |

The former 50 ambiguity-gated questions now score 27/50. The earlier targeted
rerun scored 22/50; seven examples improve and two regress between those runs.
The full-run result is 46.89%, versus the diagnostic composite's 47.72%.
The other 191 predictions were regenerated in this full run, so the composite
must not be substituted for the measured full-run result. Temperature 0 does
not establish exact repeatability; the cause of per-question variation has
not been isolated.

## Errors and resources

- Zero generation errors and no warning/error entries in the generation log.
- 19 SQL evaluation exceptions: 9 SyntaxError, 5 UndefinedColumn,
  3 UndefinedFunction, 1 FeatureNotSupported, 1 QueryCanceled. The current
  evaluator catches both reference and predicted execution errors in one block,
  so all exceptions cannot automatically be attributed to model SQL.
- 1,833,660 input + 54,293 output = 1,887,953 tokens, approximately 4.28 times
  GPT baseline's token use.
- 634 recorded successful LLM calls (excluding short preflight).
- Elapsed: 50 minutes 5 seconds, including schema preparation and evaluation.
- Key usage delta: $0.87240705, versus $0.35795325 during baseline. These are
  snapshot-based amounts and can be affected by delayed/concurrent accounting.

## Interpretation and next work

Feature selection should depend on the task: the unchanged fixed hybrid on
Ambrosia scored 73.13% with ambiguity F1 57.93%, while suppressing hard
ambiguity gating improves this BIRD experiment. Remaining baseline/hybrid
regressions support exploring a per-question selector.

The existing paired results can label a selector between whole baseline and
hybrid configurations. They do not by themselves provide independent labels
for each of the 12/14 individual features. A multilabel feature selector needs
appropriate ablation/training data, model provenance, and separate held-out
questions/databases for evaluation.
