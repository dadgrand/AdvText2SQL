# Exact main prompt diagnostic

Tested the unmodified `AMBIGUITY_PROMPT_TEMPLATE` extracted from commit
`e812451ab30dbeb9aa2eb5c3a819b53604b6487e` (local main).

Reused all 12 questions and six exact schema strings from the previous A/B
diagnostic. Model: GPT-5.4 mini via OpenRouter; temperature 0, reasoning none,
max output 4096. No new database reads or SQL generation were performed.
Only the main-prompt variant made new calls; earlier results were reused.
This isolates the prompt rather than reproducing the entire main pipeline.

| Metric | Current baseline prompt | Neutral prompt | Main prompt |
| --- | ---: | ---: | ---: |
| Correct / 12 | 6 | 6 | 7 |
| Accuracy | 50% | 50% | 58.33% |
| Ambiguous detected / 6 | 0 | 4 | 3 |
| False positives / 6 unambiguous | 0 | 4 | 2 |

The main and current parsers agree on all 12 main-prompt responses. Thus parser
differences do not explain the observed results. Main receives raw `OK` for
seven questions and clarification text for five.

Qualitative limitations remain. Q40 is labeled ambiguous but its explanation
focuses on absent value descriptions rather than the intended attachment
ambiguity. Q266 is labeled ambiguous because the model questions "open
position", instead of distinguishing minimum/preferred experience. Q262 does
identify salary_range vs hourly_rate. Q38 and Q263 are false positives.
Both scope-ambiguity examples, Q165 and Q172, are missed.

Main has one more correct classification on this small diagnostic sample. This
is insufficient evidence of a reliable general improvement. No full 227-question
main-prompt run was launched, and the completed baseline remains unchanged.

New usage: 10,278 prompt + 268 completion = 10,546 tokens, 12 successful calls.
Key usage delta at immediate snapshot: $0.00033; it should not be interpreted as
settled per-run cost because accounting may update with delay.

See `prompt.json` for the exact source prompt, `responses.json` for full
messages/responses and both parser decisions, and `report.json` for all three
variants' metrics.
