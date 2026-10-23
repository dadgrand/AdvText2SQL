# Hybrid comparison

Historical results use a different model; the GPT baseline isolates the feature effect.


## bird — 241 questions

| Run | Accuracy |
| --- | ---: |
| GPT baseline | 41.08% |
| GPT hybrid | 38.59% |
| Historical best hybrid | 44.81% |

## ambrosia — 227 questions

| Run | Accuracy | Ambiguity F1 | Detected / 67 | False positives |
| --- | ---: | ---: | ---: | ---: |
| GPT baseline | 70.48% | 0.00% | 0 | 0 |
| GPT hybrid | 73.13% | 57.93% | 42 | 36 |
| Historical best hybrid | 84.14% | 70.00% | 42 | 11 |
