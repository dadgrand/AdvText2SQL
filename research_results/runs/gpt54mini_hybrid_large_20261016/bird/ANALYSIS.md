# GPT hybrid BIRD result

All 241 question IDs match both the GPT baseline and historical best hybrid.
The run completed successfully with no warning/error entries in the log.

| Run | Correct | Accuracy | Ambiguous predictions |
| --- | ---: | ---: | ---: |
| GPT baseline | 99/241 | 41.08% | 0 |
| GPT fixed hybrid | 93/241 | 38.59% | 50 |
| Historical best hybrid | 108/241 | 44.81% | 3 |

The GPT hybrid fixes 29 baseline failures and regresses on 35 baseline successes.
Of these 35 regressions, 21 are caused by returning ambiguous instead of a
baseline SQL prediction that had scored correctly. The remaining 14 regressions
are SQL-result failures. Thus ambiguity gating is a substantial source of lost
baseline successes, but it is not the only regression mechanism.

There are 16 SQL execution errors among the 191 non-ambiguous SQL predictions:
10 SyntaxError, 3 UndefinedFunction, 2 UndefinedColumn, 1 FeatureNotSupported.
The baseline had 33 execution errors across 241 SQL predictions; the denominators
differ, so counts alone are insufficient to claim an execution-quality gain.

Usage: 1,261,461 input + 58,839 output = 1,320,300 tokens. This is approximately
2.99 times the baseline's 441,456 tokens. There are 596 recorded successful LLM
calls, excluding the short preflight. Duration: 49 minutes 44 seconds. Key usage
delta during the run: $0.6893367, versus $0.35795325 during baseline; snapshot
accounting can lag or include concurrent requests.

## Next experiments

1. Diagnose the 50 ambiguity-gated questions with FEAT_32/33 disabled, keeping
   other features, model, and data fixed. Treat this targeted rerun as diagnostic,
   not an independent full ablation. Use a separate output and preserve all
   original predictions. If useful, validate the change on a complete run.
2. Evaluate the unchanged fixed hybrid on the same 227-question Ambrosia subset.
   Do not assume that a feature harmful for BIRD is harmful for ambiguity detection.
3. Use per-question gains/losses and costs as evidence for feature selection.
   Train/tune the feature selector on a separate split, and evaluate on held-out
   questions rather than claiming performance on questions used for selection.

No new provider calls were made during this analysis, and the completed run
was not modified.
