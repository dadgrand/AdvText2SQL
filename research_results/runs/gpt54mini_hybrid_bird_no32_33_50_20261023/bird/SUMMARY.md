# Targeted BIRD rerun without FEAT_32/33

Completed all 50 selected questions. Exactly FEAT_32 and FEAT_33 were disabled;
the other fixed-hybrid features, GPT-5.4 mini, temperature 0, reasoning none,
4096 output-token cap, evidence, and five generation attempts were retained.

| Same 50 question IDs | Correct | Accuracy | Ambiguous |
| --- | ---: | ---: | ---: |
| Original GPT hybrid | 0 | 0% | 50 |
| GPT baseline | 21 | 42% | 0 |
| Hybrid without FEAT_32/33 | 22 | 44% | 0 |

Relative to baseline on this subset, the rerun fixes three questions and
regresses on two. Five SQL execution errors remain; zero generation errors
and no warnings/errors in the generation log were recorded.

## Diagnostic composite

Reusing the original hybrid's 191 other answers (93 correct) and replacing its
50 blocked answers with this rerun produces 115/241 = 47.72%. For context,
GPT baseline scored 99/241 = 41.08%, the original GPT hybrid scored 93/241 =
38.59%, and historical best hybrid scored 108/241 = 44.81%.

47.72% is a composite diagnostic, not the measured result of a full
241-question run with FEAT_32/33 disabled. It must be labeled as such; a full
run is needed to measure that configuration directly. The selected subset
was chosen after examining the original run, not independently sampled.

The evidence supports avoiding hard ambiguity gating for these BIRD questions.
It does not show that the detector should be disabled for Ambrosia, whose
target includes detecting genuine ambiguity.

## Usage and resume

The manual attempt resumed four predictions from the interrupted background
attempt. The resulting report covers all 50 questions. Latest-attempt usage:
431,733 input + 11,736 output = 443,469 tokens; key usage delta $0.1998681;
elapsed 12 minutes 43 seconds including schema preparation. These figures
exclude unrecorded usage from the interrupted attempt and may be affected by
delayed key accounting. Do not treat them as exact total cost for all 50.

All source predictions and completed baseline/hybrid reports are unchanged.
