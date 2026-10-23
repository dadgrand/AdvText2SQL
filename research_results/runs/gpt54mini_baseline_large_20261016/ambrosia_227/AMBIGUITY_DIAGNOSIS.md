# Baseline ambiguity diagnosis

Checked against the completed 227-question GPT-5.4 mini run. No new provider
requests were made and no prompts or prediction artifacts were changed.

## Confirmed

- All 227 questions reached the classic ambiguity checker.
- The log contains 227 `Ambiguity check: OK` entries, zero ambiguous entries,
  and zero ambiguity-check exceptions.
- FEAT_12, FEAT_13, and FEAT_32 were false. The simple prompt was selected;
  optimistic fallback and taxonomy detection were disabled.
- Offline mocked-client checks confirm that the question is present in the
  system prompt, `OK` maps to unambiguous, and a clarification response maps to
  ambiguous. The outer query method propagates that ambiguous response.
- The checker logs the normalized decision, not the full raw response for OK
  decisions. Therefore the original 227 response bodies cannot be recovered
  from these logs.

## Prompt concern

`AMBIGUITY_PROMPT_SIMPLE` tells the model to assume questions are unambiguous
by default and treats ambiguity as an exception. It additionally requires two
fundamentally different meanings and results. Ambrosia includes subtler scope
and attachment ambiguities, such as:

- "List all banquet halls and conference rooms with a 200 person capacity."
- "Give me restaurants and cafes that serve Italian cuisine."
- "Show me chefs and servers who speak English."

The prompt's conservative framing is a plausible cause of the all-unambiguous
behavior. It is not experimentally established as the sole cause: no controlled
LLM comparison with a neutral prompt has been performed.

## Separate parser issue

The current parser handles `OK`, but classifies `**OK**.` as ambiguous because
it strips asterisks before removing trailing punctuation. It also accepts an
OK first line regardless of later lines. These are parser limitations; the
formatting issue would cause extra ambiguous predictions, so it does not
explain this run's zero ambiguous predictions.

## Interpretation

The run has 160 unambiguous and 67 ambiguous examples. Predicting every example
as unambiguous gives 160/227 = 70.48% accuracy, 0% ambiguity recall, and 50%
balanced accuracy. Thus the current headline accuracy alone is insufficient
evidence of better ambiguity detection than the historical baseline.

Keep this completed run as the original baseline. A separate small diagnostic
experiment with raw-response capture and a neutral prompt is needed to test
the prompt hypothesis before changing the baseline configuration.
