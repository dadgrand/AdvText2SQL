# Hard-structure error slice for B_real (t49)

Purpose: decide whether arm D (verified synthetic) has a real gap to target, independently of arm C. Error = B_real's execution result differs from native gold, batch-1 decoding, over the gold-executable ids. A question can belong to several structure classes.

Overall error: OOD-876 **162/811 = 20.0 %**; dev-216 **113/216 = 52.3 %** (dev is the selection shard, so its rate is optimistic by construction).

## Error rate per structure class (OOD-876, gold SQL features)

| class | denominator | B_real wrong | error rate | lift vs overall |
|---|---:|---:|---:|---:|
| `joins_3plus` | 30 | 17 | 56.7 % | 2.84× |
| `having` | 2 | 1 | 50.0 % | 2.50× |
| `subquery` | 57 | 21 | 36.8 % | 1.84× |
| `group_by` | 72 | 21 | 29.2 % | 1.46× |
| `joins_2plus` | 214 | 61 | 28.5 % | 1.43× |
| `joins_any` | 609 | 128 | 21.0 % | 1.05× |
| `cte` | 0 | 0 | — | — |
| `correlated_subquery` | 0 | 0 | — | — |
| `window` | 0 | 0 | — | — |
| `filter_clause` | 0 | 0 | — | — |
| `lateral` | 0 | 0 | — | — |
| `distinct_on` | 0 | 0 | — | — |
| `jsonb` | 0 | 0 | — | — |
| `set_ops` | 0 | 0 | — | — |

Classes with denominator 0 (`cte`, `correlated_subquery`, `window`, `filter_clause`, `lateral`, `distinct_on`, `jsonb`, `set_ops`) do not occur in the OOD-876 gold SQL at all, so no error rate exists for them on this shard — they are not '0 % error', they are untestable here.

## Same classes on dev-216 (selection shard)

| class | denominator | wrong | error rate | lift |
|---|---:|---:|---:|---:|
| `having` | 5 | 5 | 100.0 % | 1.91× |
| `window` | 2 | 2 | 100.0 % | 1.91× |
| `set_ops` | 1 | 1 | 100.0 % | 1.91× |
| `group_by` | 24 | 17 | 70.8 % | 1.35× |
| `joins_2plus` | 61 | 33 | 54.1 % | 1.03× |
| `joins_any` | 179 | 96 | 53.6 % | 1.03× |
| `subquery` | 21 | 11 | 52.4 % | 1.00× |
| `joins_3plus` | 12 | 6 | 50.0 % | 0.96× |
| `cte` | 2 | 1 | 50.0 % | 0.96× |
| `correlated_subquery` | 0 | 0 | — | — |
| `filter_clause` | 0 | 0 | — | — |
| `lateral` | 0 | 0 | — | — |
| `distinct_on` | 0 | 0 | — | — |
| `jsonb` | 0 | 0 | — | — |

## Join-depth buckets (OOD-876)

| joins in gold | denominator | wrong | error rate |
|---|---:|---:|---:|
| 0 | 202 | 34 | 16.8 % |
| 1 | 395 | 67 | 17.0 % |
| 2 | 184 | 44 | 23.9 % |
| 3+ | 30 | 17 | 56.7 % |

## Training coverage for the same classes (train_main, 6,499 rows)

| class | train rows | share of corpus | error rate (OOD) | ratio error/share |
|---|---:|---:|---:|---:|
| `cte` | 0 | 0.0 % | — | — |
| `correlated_subquery` | 0 | 0.0 % | — | — |
| `window` | 0 | 0.0 % | — | — |
| `filter_clause` | 0 | 0.0 % | — | — |
| `lateral` | 0 | 0.0 % | — | — |
| `distinct_on` | 0 | 0.0 % | — | — |
| `jsonb` | 0 | 0.0 % | — | — |
| `set_ops` | 1 | 0.0 % | — | — |
| `having` | 102 | 1.6 % | 50.0 % | 31.86 |
| `joins_3plus` | 332 | 5.1 % | 56.7 % | 11.09 |
| `group_by` | 438 | 6.7 % | 29.2 % | 4.33 |
| `subquery` | 766 | 11.8 % | 36.8 % | 3.13 |
| `joins_2plus` | 1613 | 24.8 % | 28.5 % | 1.15 |
| `joins_any` | 4858 | 74.8 % | 21.0 % | 0.28 |

## Decision note for arm D

**D has a target, but it is narrow.** The classes that combine a high B_real error rate with thin training coverage are `joins_3plus` (56.7 % error on 30 OOD questions; 332 training rows = 5.1 %), `group_by` (29.2 % error on 72 OOD questions; 438 training rows = 6.7 %).

The obvious suspects — window functions, CTE, FILTER, LATERAL, DISTINCT ON, jsonb, correlated subqueries — are absent from the OOD-876 gold (denominator 0) and from train_main (0 rows), so D cannot be tested on them with the current shard at all: that would need a new evaluation slice first, not a synthetic batch.

Sizing against the token budget:
- 5 % of the 352,421-token budget = 325 rows at the corpus average of 54.2 supervised tokens/row;
- 10 % of the budget = 650 rows (targeted rows are longer than average, so 30–40 % fewer rows are needed for the same token share — order 300–500 rows).

HAVING is the same aggregation story but too rare to measure on its own (2 OOD questions, 1 error), so it is folded into the GROUP BY target rather than counted as a separate class.

Caveat for the decision: the measurable denominator is small — 30 OOD questions with 3+ joins and 72 with GROUP BY (12 and 24 on dev-216). A D slice can move the error rate on those classes, but the OOD-level effect will be measured on a handful of questions, so the D result should be read as a class-level signal and, if it justifies it, the evaluation slice for 3+-join aggregation should be extended before a full run.

Reading rules: denominators are the gold-executable questions that contain the feature; a question can count in several classes; dev-216 is the selection shard (optimistic), OOD-876 is confirmation only; every number is batch-1 decoding. Machine-readable source: `cluster/results/hard_slice.json`.

