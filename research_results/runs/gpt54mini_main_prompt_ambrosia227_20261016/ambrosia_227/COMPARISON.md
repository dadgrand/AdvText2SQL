# Ambrosia: full 227-question prompt comparison

All three prediction files cover exactly the same 227 question IDs: 67
ambiguous and 160 unambiguous. Metrics below were recomputed from the saved
predictions and dataset labels. Ambrosia's evaluator checks ambiguity labels,
not SQL execution correctness.

| Metric | Previous GPT / current prompt | GPT / main prompt | Historical baseline |
| --- | ---: | ---: | ---: |
| Correct | 160 | 106 | 127 |
| Accuracy | 70.48% | 46.70% | 55.95% |
| True positives | 0 | 31 | 24 |
| False negatives | 67 | 36 | 43 |
| True negatives | 160 | 75 | 103 |
| False positives | 0 | 85 | 57 |
| Ambiguity recall | 0% | 46.27% | 35.82% |
| Ambiguity precision | Undefined | 26.72% | 29.63% |
| Ambiguity F1 | 0% | 33.88% | 32.43% |
| Balanced accuracy | 50% | 46.57% | 50.10% |

Relative to the previous GPT run, the main prompt fixes 31 decisions and
introduces 85 errors: accuracy changes by -23.79 percentage points. Relative
to the historical baseline it fixes 22 decisions and introduces 43 errors:
accuracy changes by -9.25 percentage points. The main prompt detects more
ambiguous questions but raises too many false alarms. It is not a successful
overall improvement, despite its slightly higher ambiguity F1 than history.

Raw false-positive examples illustrate the issue:

- Q3 asks about restaurants serving Italian cuisine and cafes serving the same
  cuisine. The model unnecessarily asks what "the same cuisine" means.
- Q4 explicitly requests all restaurants and cafes serving Italian cuisine.
  The model asks how to identify cafes from the schema.
- Q6 explicitly scopes English-speaking to servers. The model asks which
  column distinguishes chefs/servers instead of accepting the question.

This full result also shows that the one-question advantage seen in the
12-question diagnostic did not generalize to the full subset.

## Run details

- Completed successfully: all 52 databases; no warning/error entries in the
  benchmark log; no error predictions.
- Main prompt from commit e812451ab30dbeb9aa2eb5c3a819b53604b6487e.
- Only the ambiguity prompt changed from the previous GPT run. The rest of
  the current pipeline was retained, so this is not an entire-main-branch run.
- 277,300 input + 18,099 output = 295,399 tokens; 338 recorded calls.
- Elapsed: approximately 29 minutes 10 seconds, including schema preparation.
- Key usage delta during run: $0.1571775. This is a snapshot-based estimate;
  delayed accounting or concurrent usage can affect attribution.

Keep the three configurations separate. Any future classifier/prompt work
should report precision, recall, F1, and balanced accuracy alongside accuracy;
the majority-class accuracy reference on this subset is 70.48%.
