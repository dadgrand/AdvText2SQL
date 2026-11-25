# Main results table — arms A, B_mixed, B_real, C (D pending)

Generated from the batch-1 evaluation reports; every number carries its denominator, its decoding mode and its selection label. Machine-readable twin: `main_table.json`; error ledger: `errors_by_schema.jsonl`.

## Contract

- **Decoding**: batch 1, no padding, no truncation, greedy, `max_new_tokens=256`, stop `<|im_end|>`, the training renderer, no repair (`cluster/preds/preds_manifest.json`; calibration in `cluster/eval_reports/padding_sensitivity.json`).
- **Selection rule v2**: dev-216 selects the checkpoint (**selected on this shard**, optimistic by construction), OOD-876 is confirmation only, PG-hard stays closed (`cluster/runs/selection_rule.md`).
- **Denominators**: dev-216 = 216 gold-executable (question 309 excluded by the native-gold drift policy); OOD-876 = 811 of 876 gold-executable (65 unexecutable gold); historical 241 = 209 of 241, continuity only.
- **Raw pass@1** = execution accuracy of the single greedy prediction, no repair, over the gold-executable denominator. **EX + repair** = the same predictions after one deterministic repair pass applied only to predictions whose first execution failed (fences/prose/first statement, then qualifying a bare column that exactly one in-query table can resolve). No model calls, no retries (`cluster/repair_pass.py`).

## Arm table

| arm | dev-216 pass@1 (no repair) — **selected on this shard** | dev-216 EX + repair | OOD-876 pass@1 (no repair) — **confirmation only** | OOD-876 EX + repair | historical 241 (continuity only) |
|---|---:|---:|---:|---:|---:|
| **A** | 87/216 = 40.28 % [34.0–46.9] | 87/216 = 40.28 % | 574/811 = 70.78 % [67.6–73.8] | 576/811 = 71.02 % | 83/209 = 39.71 % |
| **B_mixed** | 101/216 = 46.76 % [40.2–53.4] | 101/216 = 46.76 % | 638/811 = 78.67 % [75.7–81.3] | 638/811 = 78.67 % | 99/209 = 47.37 % |
| **B_real** | 103/216 = 47.69 % [41.1–54.3] | 103/216 = 47.69 % | 649/811 = 80.02 % [77.1–82.6] | 649/811 = 80.02 % | not run |
| **C** | 102/216 = 47.22 % [40.7–53.9] | 102/216 = 47.22 % | 655/811 = 80.76 % [77.9–83.3] | 655/811 = 80.76 % | not run |
| **D** | pending | pending | pending | pending | pending |

**Arm D is pending**: arm D (real + the new synthetic pool) has not run: it is gated behind t30, which needs the verified new-synthetic mix (t21/t37) and the frozen selection rule; no D training job was submitted and no D number exists on disk. It is listed as pending here rather than omitted.

**PG-hard is closed** (96 questions / 12 schemas, predictions sealed, not scored): it is unsealed only after the winning configuration is frozen (t27), so it contributes no row here.


Reading a cell: correct/denominator = percent, with the Wilson 95 % interval in the raw columns; every number is batch-1 decoding; every dev-216 number is **selected on this shard** (the checkpoint was chosen by dev-216, so it is optimistic by construction); every OOD-876 number is **confirmation only**; the historical 241 column uses its own evaluator, is continuity only, and is never compared in percent with 216.

## Paired comparisons (bootstrap intervals, exact McNemar)

| comparison | shard | Δ | 95 % CI | b / c | McNemar p | n | label |
|---|---|---:|---|---:|---:|---:|---|
| B_real vs A | dev216 | +7.41 pp | [+0.93, +13.89] | 35 / 19 | 0.0402 | 216 | selected on this shard |
| B_real vs A | ood876 | +9.25 pp | [+6.41, +12.21] | 112 / 37 | 5.79e-10 | 811 | confirmation only |
| B_mixed vs A | dev216 | +6.48 pp | [+0.00, +12.96] | 31 / 17 | 0.0595 | 216 | selected on this shard |
| B_mixed vs A | ood876 | +7.89 pp | [+5.06, +10.85] | 105 / 41 | 1.17e-07 | 811 | confirmation only |
| C vs B_real | dev216 | -0.46 pp | [-6.02, +5.09] | 19 / 20 | 1 | 216 | selected on this shard |
| C vs B_real | ood876 | +0.74 pp | [-1.11, +2.59] | 32 / 26 | 0.512 | 811 | confirmation only |
| C vs B_mixed | dev216 | +0.46 pp | [-5.09, +6.02] | 18 / 17 | 1 | 216 | selected on this shard |
| C vs B_mixed | ood876 | +2.10 pp | [+0.00, +4.32] | 47 / 30 | 0.0675 | 811 | confirmation only |
| C vs A | dev216 | +6.94 pp | [+0.93, +12.96] | 30 / 15 | 0.0357 | 216 | selected on this shard |
| C vs A | ood876 | +9.99 pp | [+7.15, +12.82] | 114 / 33 | 1.22e-11 | 811 | confirmation only |

## What the synthetic result means

- **At a 39.4 % synthetic share the synthetic half does not pay**: B_real − B_mixed on OOD-876 is +1.36 pp [-0.74, +3.33], b/c 40/29, McNemar p = 0.228 (n = 811) — real-only is slightly ahead and the interval includes zero. On dev-216 (selected on this shard) the same comparison is +0.93 pp [-4.63, +6.48], p = 0.871.
- **At a documented 13.77 % share (arm C) the answer does not change**: C − B_real on OOD-876 is +0.74 pp [-1.11, +2.59], p = 0.512; C − B_mixed is +2.10 pp [+0.00, +4.32], p = 0.0675.
- **Plain statement for the meeting**: *at 39.4 % and at 13.77 % the current synthetic pool gave no measurable advantage*; the adapter effect itself is large and significant (B_real − A +9.25 pp, p = 5.79e-10; C − A +9.99 pp, p = 1.22e-11).

## Errors by class — dev-216 (selected on this shard)

| arm | wrong | identifier | aggregate | grain/fan-out | period | tie/ordering | syntax | other |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **A** | 129 | 37 | 28 | 12 | 6 | 0 | 4 | 42 |
| **B_mixed** | 115 | 26 | 23 | 17 | 2 | 0 | 3 | 44 |
| **B_real** | 113 | 30 | 17 | 15 | 4 | 0 | 1 | 46 |
| **C** | 114 | 28 | 22 | 19 | 4 | 0 | 1 | 40 |

## Errors by class — OOD-876 (confirmation only)

| arm | wrong | identifier | aggregate | grain/fan-out | period | tie/ordering | syntax | other |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **A** | 237 | 69 | 43 | 24 | 7 | 2 | 18 | 74 |
| **B_mixed** | 173 | 38 | 37 | 16 | 5 | 4 | 1 | 72 |
| **B_real** | 162 | 34 | 38 | 14 | 4 | 2 | 0 | 70 |
| **C** | 156 | 29 | 40 | 15 | 5 | 3 | 1 | 63 |


The fixed repair budget could only touch the execution failures (`identifier`/`syntax`), which is why it adds at most +2 questions (A on OOD-876); the remaining errors execute but return different rows — they are semantic (`aggregate`, `grain/fan-out`, `period`, `tie/ordering`). Per-database tables and quoted examples: `errors_by_schema.jsonl`.

## Errors by database — dev-216 (selected on this shard)

| database | scored | A wrong | B_mixed wrong | B_real wrong | C wrong |
|---|---:|---:|---:|---:|---:|
| `california_schools` | 23 | 20 | 21 | 19 | 19 |
| `card_games` | 28 | 15 | 16 | 17 | 18 |
| `codebase_community` | 26 | 11 | 10 | 11 | 12 |
| `debit_card_specializing` | 19 | 15 | 13 | 14 | 12 |
| `financial` | 27 | 25 | 18 | 18 | 19 |
| `student_club` | 30 | 13 | 10 | 10 | 9 |
| `superhero` | 36 | 13 | 11 | 7 | 8 |
| `toxicology` | 27 | 17 | 16 | 17 | 17 |

## Errors by database — OOD-876 (confirmation only)

| database | scored | A wrong | B_mixed wrong | B_real wrong | C wrong |
|---|---:|---:|---:|---:|---:|
| `disney` | 74 | 35 | 28 | 27 | 20 |
| `european_football_1` | 36 | 10 | 11 | 8 | 8 |
| `law_episode` | 89 | 39 | 22 | 24 | 23 |
| `menu` | 68 | 23 | 22 | 22 | 23 |
| `movie_3` | 189 | 51 | 41 | 38 | 37 |
| `movies_4` | 134 | 29 | 21 | 15 | 14 |
| `music_tracker` | 31 | 7 | 4 | 7 | 7 |
| `sales_in_weather` | 42 | 18 | 14 | 13 | 14 |
| `social_media` | 39 | 7 | 4 | 4 | 4 |
| `talkingdata` | 109 | 18 | 6 | 4 | 6 |


## Quoted examples per arm (≥30 each; from `errors_by_schema.jsonl`)

### Arm A — first 30 wrong predictions

- `10` (california_schools, identifier): For the school with the highest average score in Reading in the SAT test, what is its FRPM count for students aged 5-17?
  - gold: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY T1.AvgScrRead DESC`
  - pred: `SELECT frpm."FRPM Count (Ages 5-17)" FROM frpm JOIN satscores ON frpm."CDSCode" = satscores.cds WHERE satscores.rtype = 'Reading' ORDER BY satscores.avgscrread `
- `14` (california_schools, identifier): List the top five schools, by descending order, from the highest to the lowest, the most number of Enrollment (Ages 5-17
  - gold: `SELECT T1.NCESSchool FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode ORDER BY T2."Enrollment (Ages 5-17)" DESC NULLS LAST LIMIT 5`
  - pred: `SELECT ncesschool FROM schools ORDER BY "Enrollment (Ages 5-17)" DESC LIMIT 5`
- `28` (california_schools, identifier): Consider the average difference between K-12 enrollment and 15-17 enrollment of schools that are locally funded, list th
  - gold: `SELECT T2.School, T2.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.FundingType = 'Locally funded' AND (T1."Enrollment (K-12)"`
  - pred: `SELECT T1.school, T1.doc, (T1."Enrollment (K-12)" - T1."Enrollment (Ages 5-17)") AS diff FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode WHERE T1.`
- `49` (california_schools, identifier): Which different county has the most number of closed schools? Please provide the name of each school as well as the clos
  - gold: `SELECT DISTINCT County, School, ClosedDate FROM schools WHERE County = (SELECT County FROM schools WHERE StatusType = 'Closed' GROUP BY County ORDER BY COUNT(Sc`
  - pred: `SELECT DISTINCT T1.county, T1.school, T1.closeddate FROM schools AS T1 WHERE T1.StatusType = 'Closed' GROUP BY T1.county, T1.school, T1.closeddate ORDER BY COUN`
- `62` (california_schools, identifier): What is the total number of non-chartered schools in the county of Los Angeles with a percent (%) of eligible free meals
  - gold: `SELECT COUNT(T2.School) FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.County = 'Los Angeles' AND T2.Charter = 0 AND CAST(T1."Free`
  - pred: `SELECT COUNT(*) FROM frpm WHERE "County Name" = 'Los Angeles' AND "Charter School" = 0 AND ("Free Meal Count (K-12)" * 100 / NULLIF("Enrollment (K-12)", 0)) < 0`
- `17` (california_schools, aggregate): Rank schools by their average score in Writing where the score is greater than 499, showing their charter numbers.
  - gold: `SELECT CharterNum, AvgScrWrite, RANK() OVER (ORDER BY AvgScrWrite DESC NULLS LAST) AS WritingScoreRank FROM schools AS T1 INNER JOIN satscores AS T2 ON CAST(T1.`
  - pred: `SELECT s.cdscode, s.charternum, AVG(ss.avgscrwrite) AS avg_write_score FROM frpm f JOIN satscores ss ON f.cdscode = ss.cds JOIN schools s ON f.cdscode = s.cdsco`
- `27` (california_schools, aggregate): What is the average score in writing for the schools that were opened after 1991 or closed before 2000? List the school 
  - gold: `SELECT T2.School, T1.AvgScrWrite, T2.Phone FROM schools AS T2 LEFT JOIN satscores AS T1 ON CAST(T2.CDSCode AS BIGINT) = CAST(T1.cds AS BIGINT) WHERE TO_CHAR(CAS`
  - pred: `SELECT s.school, s.phone, AVG(ss.avgscrwrite) AS avg_write_score FROM schools s JOIN satscores ss ON s.cdscode = ss.cds WHERE s.opendate > '1991-12-31' OR s.clo`
- `43` (california_schools, aggregate): What is the average math score of the school with the lowest average score for all subjects, and in which county is it l
  - gold: `SELECT T1.AvgScrMath, T2.County FROM satscores AS T1 INNER JOIN schools AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) WHERE NOT T1.AvgScrMath IS `
  - pred: `SELECT AVG(avgscrmath) AS avg_math_score, T1.county FROM schools AS T1 JOIN satscores AS T2 ON T1.cdscode = T2.cds GROUP BY T1.county ORDER BY AVG(avgscrmath) +`
- `91` (financial, aggregate): The average unemployment ratio of 1995 and 1996, which one has higher percentage?
  - gold: `SELECT DISTINCT CASE WHEN AVG(A13) > AVG(A12) THEN '1996' ELSE '1995' END FROM district`
  - pred: `SELECT a12 AS "1995", a13 AS "1996", CASE WHEN a12 > a13 THEN '1995' ELSE '1996' END AS "Higher" FROM district WHERE a8 BETWEEN 1995 AND 1996`
- `116` (financial, aggregate): For the client whose loan was approved first in 1993/7/5, what is the increase rate of his/her account balance from 1993
  - gold: `SELECT CAST((SUM(CASE WHEN T3.date = '1998-12-27' THEN T3.balance ELSE 0 END) - SUM(CASE WHEN T3.date = '1993-03-22' THEN T3.balance ELSE 0 END)) AS REAL) * 100`
  - pred: `SELECT      ((t1.balance - t2.balance) / t2.balance) * 100 AS increase_rate FROM      trans t1 JOIN      trans t2 ON t1.account_id = t2.account_id WHERE      t1`
- `84` (california_schools, grain_fanout): What are the two most common first names among the school administrators? Indicate the district to which they administer
  - gold: `SELECT DISTINCT T1.AdmFName1, T1.District FROM schools AS T1 INNER JOIN (SELECT admfname1 FROM schools GROUP BY admfname1 ORDER BY COUNT(admfname1) DESC NULLS L`
  - pred: `SELECT T1.admfname1, T1.admlname1, T1.district FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode GROUP BY T1.admfname1, T1.admlname1, T1.district OR`
- `89` (financial, grain_fanout): How many accounts who choose issuance after transaction are staying in East Bohemia region?
  - gold: `SELECT COUNT(T2.account_id) FROM district AS T1 INNER JOIN account AS T2 ON T1.district_id = T2.district_id WHERE T1.A3 = 'east Bohemia' AND T2.frequency = 'POP`
  - pred: `SELECT COUNT(DISTINCT a.account_id) FROM account a JOIN disp d ON a.account_id = d.account_id JOIN card c ON d.disp_id = c.disp_id JOIN district dt ON a.distric`
- `135` (financial, grain_fanout): After making a credit card withdrawal, how many account/s with monthly issuance has a negative balance?
  - gold: `SELECT COUNT(T1.account_id) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T1.balance < 0 AND T1.operation = 'VYBER KARTOU' AN`
  - pred: `SELECT COUNT(DISTINCT t1.account_id) FROM trans AS t1 JOIN account AS t2 ON t1.account_id = t2.account_id WHERE t1.operation = 'VYBER KARTOU' AND t2.frequency =`
- `136` (financial, grain_fanout): Between 1/1/1995 and 12/31/1997, how many loans in the amount of at least 250,000 per account that chose monthly stateme
  - gold: `SELECT COUNT(T1.account_id) FROM account AS T1 INNER JOIN loan AS T2 ON T1.account_id = T2.account_id WHERE T2.date BETWEEN '1995-01-01' AND '1997-12-31' AND T1`
  - pred: `SELECT COUNT(DISTINCT l.account_id) FROM loan l JOIN account a ON l.account_id = a.account_id WHERE l.date BETWEEN '1995-01-01' AND '1997-12-31' AND l.amount >=`
- `145` (financial, grain_fanout): Who are the account holder identification numbers whose who have transactions on the credit card with the amount is less
  - gold: `SELECT T1.account_id FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE TO_CHAR(CAST(T1.date AS TIMESTAMP), 'YYYY') = '1998' AND T`
  - pred: `SELECT DISTINCT d.client_id FROM disp d JOIN account a ON d.account_id = a.account_id JOIN trans t ON a.account_id = t.account_id WHERE t.date BETWEEN '1998-01-`
- `169` (financial, period): What was the growth rate of the total amount of loans across all accounts for a male client between 1996 and 1997?
  - gold: `SELECT CAST((SUM(CASE WHEN TO_CHAR(CAST(T1.date AS TIMESTAMP), 'YYYY') = '1997' THEN T1.amount ELSE 0 END) - SUM(CASE WHEN TO_CHAR(CAST(T1.date AS TIMESTAMP), '`
  - pred: `SELECT ((SUM(CASE WHEN EXTRACT(YEAR FROM l.date) = 1997 THEN l.amount ELSE 0 END) - SUM(CASE WHEN EXTRACT(YEAR FROM l.date) = 1996 THEN l.amount ELSE 0 END)) / `
- `708` (codebase_community, period): List the creation date and age of the user that commented with webiste.
  - gold: `SELECT T2.CreationDate, T2.Age FROM comments AS T1 INNER JOIN users AS T2 ON T1.UserId = T2.Id WHERE T1.text LIKE '%http://%'`
  - pred: `SELECT u.creationdate, EXTRACT(YEAR FROM AGE(u.creationdate)) AS age FROM users u JOIN comments c ON u.id = c.userid WHERE c.text LIKE '%http://%';`
- `1437` (student_club, period): Which members who were approved from 1/10/2019 to 11/19/2019? Please identify the member who attended the event and the 
  - gold: `SELECT DISTINCT T1.link_to_member, T3.link_to_event FROM expense AS T1 INNER JOIN member AS T2 ON T1.link_to_member = T2.member_id INNER JOIN attendance AS T3 O`
  - pred: `SELECT DISTINCT m.first_name, m.last_name, a.link_to_event FROM member m JOIN expense e ON m.member_id = e.link_to_member JOIN attendance a ON m.member_id = a.l`
- `1473` (debit_card_specializing, period): What was the average monthly consumption of customers in SME for the year 2013?
  - gold: `SELECT AVG(T2.Consumption) / NULLIF(12, 0) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE SUBSTRING(T2.Date FROM 1 FOR 4`
  - pred: `SELECT AVG(consumption) / 12 AS avg_monthly_consumption FROM yearmonth WHERE customerid IN ( SELECT customerid FROM customers WHERE segment = 'SME' ) AND date B`
- `1496` (debit_card_specializing, period): Which client segment consumed the least in September 2013?
  - gold: `SELECT c.Segment FROM customers AS c JOIN yearmonth AS y ON c.CustomerID = y.CustomerID WHERE y.Date = '201309' GROUP BY c.Segment ORDER BY SUM(y.Consumption) A`
  - pred: `SELECT c.segment, SUM(t.amount) AS total_spent FROM customers c JOIN transactions_1k t ON c.customerid = t.customerid JOIN yearmonth ym ON t.customerid = ym.cus`
- `4` (california_schools, syntax): Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.
  - gold: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter Funding Type" = 'Directly funded' AND T1."Charter School (`
  - pred: `SELECT T1.phone FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode WHERE T2.`Charter School (Y/N)` = 1 AND T1.opendate > '2000-01-01'`
- `35` (california_schools, syntax): What is the administrator's email address of the chartered school with the fewest students enrolled in grades 1 through 
  - gold: `SELECT T2.AdmEmail1 FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter School (Y/N)" = 1 ORDER BY T1."Enrollment (K-12)" ASC `
  - pred: `SELECT T1.admemail1 FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode WHERE T2.`Charter School (Y/N)` = 1 ORDER BY T2.`Enrollment (K-12)` ASC LIMIT `
- `83` (california_schools, syntax): Of the schools that offers a magnet program serving a grade span of Kindergarten to 8th grade, how many offers Multiple 
  - gold: `SELECT T2.City, COUNT(T2.CDSCode) FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.Magnet = 1 AND T2.GSoffered = 'K-8' AND T1."NSLP `
  - pred: `SELECT COUNT(DISTINCT T1.city) AS num_cities, T1.city, COUNT(T1.school) AS num_schools FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode WHERE T1.ma`
- `158` (financial, syntax): What is the district Id of the account that placed the order with the id 33333?
  - gold: `SELECT T3.district_id FROM "order" AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id INNER JOIN district AS T3 ON T2.district_id = T3.district_id `
  - pred: `SELECT T1.district_id FROM account AS T1 JOIN order AS T2 ON T1.account_id = T2.account_id WHERE T2.order_id = 33333`
- `13` (california_schools, other): Please list the phone numbers of the schools with the top 3 SAT excellence rate.
  - gold: `SELECT T1.Phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.cds ORDER BY CAST(T2.NumGE1500 AS REAL) / NULLIF(T2.NumTstTakr, 0) DESC NULLS L`
  - pred: `SELECT T1.phone FROM schools AS T1 JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE T2.rtype = 'excellence' ORDER BY T2.numge1500 / NULLIF(T2.numtsttakr, 0) DE`
- `34` (california_schools, other): What is the free rate for students between the ages of 5 and 17 at the school run by Kacey Gibson?
  - gold: `SELECT CAST(T2."Free Meal Count (Ages 5-17)" AS REAL) / NULLIF(T2."Enrollment (Ages 5-17)", 0) FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCo`
  - pred: `SELECT "Percent (%) Eligible Free (Ages 5-17)" FROM frpm WHERE "School Name" = 'Kacey Gibson'`
- `37` (california_schools, other): What is the complete address of the school with the lowest excellence rate? Indicate the Street, City, Zip and State.
  - gold: `SELECT T2.Street, T2.City, T2.State, T2.Zip FROM satscores AS T1 INNER JOIN schools AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY CAST(T`
  - pred: `SELECT T2.street, T2.city, T2.state, T2.zip FROM satscores AS T1 JOIN schools AS T2 ON T1.cds = T2.cdscode WHERE T1.rtype = 'Excellence Rate' ORDER BY T1.numge1`
- `50` (california_schools, other): What is the postal street address for the school with the 7th highest Math average? Indicate the school's name.
  - gold: `SELECT T2.MailStreet, T2.School FROM satscores AS T1 INNER JOIN schools AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY T1.AvgScrMath DESC`
  - pred: `SELECT T2.street, T2.school FROM satscores AS T1 JOIN schools AS T2 ON T1.cds = T2.cdscode ORDER BY T1.avgscrmath DESC LIMIT 1 OFFSET 6`
- `82` (california_schools, other): What is the grade span offered in the school with the highest longitude?
  - gold: `SELECT GSoffered FROM schools ORDER BY ABS(longitude) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT "High Grade" - "Low Grade" AS grade_span FROM frpm WHERE cdscode = (SELECT cdscode FROM schools ORDER BY ABS(longitude) DESC LIMIT 1)`
- `ood:disney:1` (disney, identifier): The song "Once Upon a Dream" is associated with the movie directed by whom?
  - gold: `SELECT T2.director FROM characters AS T1 INNER JOIN director AS T2 ON T1.movie_title = T2.name WHERE T1.song = 'Once Upon a Dream'`
  - pred: `SELECT T2.director FROM characters AS T1 JOIN director AS T2 ON T1.movie_title = T2.movie WHERE T1.song = 'Once Upon a Dream'`

### Arm B_mixed — first 30 wrong predictions

- `17` (california_schools, identifier): Rank schools by their average score in Writing where the score is greater than 499, showing their charter numbers.
  - gold: `SELECT CharterNum, AvgScrWrite, RANK() OVER (ORDER BY AvgScrWrite DESC NULLS LAST) AS WritingScoreRank FROM schools AS T1 INNER JOIN satscores AS T2 ON CAST(T1.`
  - pred: `SELECT T1.CDSCode, T1.CharterNumber FROM frpm AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.AvgScrWrite > 499 AND NOT T1.CharterNumber IS NUL`
- `27` (california_schools, identifier): What is the average score in writing for the schools that were opened after 1991 or closed before 2000? List the school 
  - gold: `SELECT T2.School, T1.AvgScrWrite, T2.Phone FROM schools AS T2 LEFT JOIN satscores AS T1 ON CAST(T2.CDSCode AS BIGINT) = CAST(T1.cds AS BIGINT) WHERE TO_CHAR(CAS`
  - pred: `SELECT T2.School, T2.avgscrwrite, T2.cds FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE (TO_CHAR(CAST(T1.opendate AS TIMESTAMP), 'YY`
- `28` (california_schools, identifier): Consider the average difference between K-12 enrollment and 15-17 enrollment of schools that are locally funded, list th
  - gold: `SELECT T2.School, T2.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.FundingType = 'Locally funded' AND (T1."Enrollment (K-12)"`
  - pred: `SELECT T1.School, T1.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.FundingType = 'Local' AND T1."Enrollment (K-12)" - T1."Enr`
- `34` (california_schools, identifier): What is the free rate for students between the ages of 5 and 17 at the school run by Kacey Gibson?
  - gold: `SELECT CAST(T2."Free Meal Count (Ages 5-17)" AS REAL) / NULLIF(T2."Enrollment (Ages 5-17)", 0) FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCo`
  - pred: `SELECT T1."Percent (%) Eligible Free (Ages 5-17)" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1."CDSCode" = T2."CDSCode" WHERE T2."School" = 'Kacey Gibson'`
- `73` (california_schools, identifier): What is the free or reduced price meal count for ages 5 to 17 in the Youth Authority School with a mailing street addres
  - gold: `SELECT T1."FRPM Count (Ages 5-17)" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.MailStreet = 'PO Box 1040' AND T2.SOCType = 'You`
  - pred: `SELECT "Free Meal Count (Ages 5-17)" FROM "frpm" WHERE "School Name" = 'Youth Authority School' AND "mailstreet" = 'PO Box 1040'`
- `36` (california_schools, aggregate): Under whose administration is the school with the highest number of students scoring 1500 or more on the SAT? Indicate t
  - gold: `SELECT T2.AdmFName1, T2.AdmLName1, T2.AdmFName2, T2.AdmLName2, T2.AdmFName3, T2.AdmLName3 FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode O`
  - pred: `SELECT T1.admfname1, T1.admlname1 FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE T2.NumGE1500 = (SELECT MAX(NumGE1500) FROM satscore`
- `39` (california_schools, aggregate): What is the average number of test takers from Fresno schools that opened between 1/1/1980 and 12/31/1980?
  - gold: `SELECT AVG(T1.NumTstTakr) FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode WHERE TO_CHAR(CAST(T2.OpenDate AS TIMESTAMP), 'YYYY') = '1980' AN`
  - pred: `SELECT CAST(SUM(T2.numtsttakr) AS REAL) / NULLIF(COUNT(T2.numtsttakr), 0) FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE TO_CHAR(CAS`
- `82` (california_schools, aggregate): What is the grade span offered in the school with the highest longitude?
  - gold: `SELECT GSoffered FROM schools ORDER BY ABS(longitude) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT DISTINCT "gsoffered" FROM "schools" WHERE "longitude" = (SELECT MAX("longitude") FROM "schools") ORDER BY "gsoffered"`
- `91` (financial, aggregate): The average unemployment ratio of 1995 and 1996, which one has higher percentage?
  - gold: `SELECT DISTINCT CASE WHEN AVG(A13) > AVG(A12) THEN '1996' ELSE '1995' END FROM district`
  - pred: `SELECT CASE WHEN A12 > A13 THEN '1995' WHEN A12 < A13 THEN '1996' ELSE 'equal' END FROM district`
- `116` (financial, aggregate): For the client whose loan was approved first in 1993/7/5, what is the increase rate of his/her account balance from 1993
  - gold: `SELECT CAST((SUM(CASE WHEN T3.date = '1998-12-27' THEN T3.balance ELSE 0 END) - SUM(CASE WHEN T3.date = '1993-03-22' THEN T3.balance ELSE 0 END)) AS REAL) * 100`
  - pred: `SELECT T1.balance / NULLIF(T2.balance, 0) FROM trans AS T1 INNER JOIN trans AS T2 ON T1.account_id = T2.account_id WHERE T1.date = '1998-12-27' AND T2.date = '1`
- `49` (california_schools, grain_fanout): Which different county has the most number of closed schools? Please provide the name of each school as well as the clos
  - gold: `SELECT DISTINCT County, School, ClosedDate FROM schools WHERE County = (SELECT County FROM schools WHERE StatusType = 'Closed' GROUP BY County ORDER BY COUNT(Sc`
  - pred: `SELECT T1.School, T1.ClosedDate FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1.StatusType = 'Closed' GROUP BY T1.School, T1.Closed`
- `84` (california_schools, grain_fanout): What are the two most common first names among the school administrators? Indicate the district to which they administer
  - gold: `SELECT DISTINCT T1.AdmFName1, T1.District FROM schools AS T1 INNER JOIN (SELECT admfname1 FROM schools GROUP BY admfname1 ORDER BY COUNT(admfname1) DESC NULLS L`
  - pred: `SELECT "admfname1", "district" FROM "schools" GROUP BY "admfname1", "district" ORDER BY COUNT(*) DESC NULLS LAST LIMIT 2`
- `149` (financial, grain_fanout): Please list the account types that are not eligible for loans, and the average income of residents in the district where
  - gold: `SELECT T3.type FROM district AS T1 INNER JOIN account AS T2 ON T1.district_id = T2.district_id INNER JOIN disp AS T3 ON T2.account_id = T3.account_id WHERE T3.t`
  - pred: `SELECT DISTINCT "disp"."type", "district"."a11" FROM "disp" JOIN "account" ON "disp"."account_id" = "account"."account_id" JOIN "district" ON "account"."distric`
- `199` (toxicology, grain_fanout): In the molecule containing sodium atoms, how many are non-carcinogenic?
  - gold: `SELECT COUNT(DISTINCT T2.molecule_id) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'na' AND T2.label = '-'`
  - pred: `SELECT COUNT(T1.molecule_id) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'na' AND T2.label = '-'`
- `230` (toxicology, grain_fanout): What are the elements of the toxicology and label of molecule TR060?
  - gold: `SELECT DISTINCT T1.element, T2.label FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.molecule_id = 'TR060'`
  - pred: `SELECT T1.element, T2.label FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.label = 'toxicology' AND T2.molecule_id = 'TR0`
- `102` (financial, period): State different accounts who have account opening date before 1997 and own an amount of money greater than 3000USD
  - gold: `SELECT DISTINCT T2.account_id FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE TO_CHAR(CAST(T2.date AS TIMESTAMP), 'YYYY') < '19`
  - pred: `SELECT DISTINCT "account_id" FROM "account" WHERE "date" < '1997-01-01' UNION SELECT "account_id" FROM "trans" WHERE "amount" > 3000`
- `1340` (student_club, period): Calculate the difference of the total amount spent in all events by the Student_Club in year 2019 and 2020.
  - gold: `SELECT SUM(CASE WHEN SUBSTRING(T1.event_date FROM 1 FOR 4) = '2019' THEN T2.spent ELSE 0 END) - SUM(CASE WHEN SUBSTRING(T1.event_date FROM 1 FOR 4) = '2020' THE`
  - pred: `SELECT SUM(CASE WHEN TO_CHAR(CAST(T1.event_date AS TIMESTAMP), 'YYYY') = '2019' THEN T2.spent ELSE 0 END) - SUM(CASE WHEN TO_CHAR(CAST(T1.event_date AS TIMESTAM`
- `62` (california_schools, syntax): What is the total number of non-chartered schools in the county of Los Angeles with a percent (%) of eligible free meals
  - gold: `SELECT COUNT(T2.School) FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.County = 'Los Angeles' AND T2.Charter = 0 AND CAST(T1."Free`
  - pred: `SELECT COUNT(*) FROM frpm WHERE County Name = 'Los Angeles' AND "Percent (%) Eligible Free (K-12)" < 0.18 AND "Charter School (Y/N)" = 0`
- `158` (financial, syntax): What is the district Id of the account that placed the order with the id 33333?
  - gold: `SELECT T3.district_id FROM "order" AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id INNER JOIN district AS T3 ON T2.district_id = T3.district_id `
  - pred: `SELECT T1.district_id FROM account AS T1 INNER JOIN order AS T2 ON T1.account_id = T2.account_id WHERE T2.order_id = 33333`
- `708` (codebase_community, syntax): List the creation date and age of the user that commented with webiste.
  - gold: `SELECT T2.CreationDate, T2.Age FROM comments AS T1 INNER JOIN users AS T2 ON T1.UserId = T2.Id WHERE T1.text LIKE '%http://%'`
  - pred: `SELECT T1.CreationDate, T1.Age FROM Users AS T1 INNER JOIN Comments AS T2 ON T1.Id = T2.UserId WHERE T2.Text LIKE '%http://%'  **Created Question**: Find the us`
- `4` (california_schools, other): Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.
  - gold: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter Funding Type" = 'Directly funded' AND T1."Charter School (`
  - pred: `SELECT T1.Phone FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1.OpenDate > '2000-01-01' AND T2."Charter School (Y/N)" = 1`
- `9` (california_schools, other): Among the schools with the average score in Math over 560 in the SAT test, how many schools are directly charter-funded?
  - gold: `SELECT COUNT(T2."School Code") FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) WHERE T1.AvgScrMath > 560 AND T`
  - pred: `SELECT COUNT(*) FROM "schools" JOIN "satscores" ON "schools"."cdscode" = "satscores"."cds" WHERE "satscores"."avgscrmath" > 560 AND "schools"."fundingtype" = 'D`
- `10` (california_schools, other): For the school with the highest average score in Reading in the SAT test, what is its FRPM count for students aged 5-17?
  - gold: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY T1.AvgScrRead DESC`
  - pred: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON T2."School Name" = T1.sname WHERE T1.rtype = 'Reading' ORDER BY T1.avgscrread D`
- `13` (california_schools, other): Please list the phone numbers of the schools with the top 3 SAT excellence rate.
  - gold: `SELECT T1.Phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.cds ORDER BY CAST(T2.NumGE1500 AS REAL) / NULLIF(T2.NumTstTakr, 0) DESC NULLS L`
  - pred: `SELECT T2.Phone FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.CDS = T2.CDSCode WHERE T1.RType = 'Excellence' ORDER BY T1.NumGE1500 / NULLIF(T1.NumTstTakr,`
- `14` (california_schools, other): List the top five schools, by descending order, from the highest to the lowest, the most number of Enrollment (Ages 5-17
  - gold: `SELECT T1.NCESSchool FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode ORDER BY T2."Enrollment (Ages 5-17)" DESC NULLS LAST LIMIT 5`
  - pred: `SELECT T1.NCESDIST, T1.NCESSCHOOL FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode ORDER BY T2."Enrollment (Ages 5-17)" DESC NULLS LAST LIMIT`
- `ood:disney:16` (disney, identifier): List all of Wolfgang Reitherman's movies and their voice actors.
  - gold: `SELECT T1.name, T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON T1.name = T2.movie WHERE T1.director = 'Wolfgang Reitherman'`
  - pred: `SELECT T1.movie, T2."voice-actor" FROM characters AS T1 INNER JOIN "voice-actors" AS T2 ON T2.movie = T1.movie_title INNER JOIN director AS T3 ON T3.name = T1.m`
- `ood:disney:51` (disney, identifier): List all the voice actors in the movie directed by Ben Sharpsteen which was released on February 9, 1940.
  - gold: `SELECT T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON TRUE INNER JOIN movies_total_gross AS T3 ON T1.name = T2.movie AND T2.movie = T3.`
  - pred: `SELECT T1."voice-actor" FROM "voice-actors" AS T1 INNER JOIN characters AS T2 ON T1.movie = T2.movie_title WHERE T2.release_date = 'Feb 9, 1940' AND T2.director`
- `ood:disney:71` (disney, identifier): What is Disney's highest grossing action movie?
  - gold: `SELECT movie_title FROM movies_total_gross WHERE genre = 'Action' ORDER BY CAST(REPLACE(TRIM('$' FROM total_gross), ',', '') AS REAL) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT T1.movie_title FROM movies_total_gross AS T1 INNER JOIN revenue AS T2 ON T1.movie_title = T2.Studio_Entertainment_NI_1 WHERE T1.genre = 'Action' ORDER BY`
- `ood:european_football_1:85` (european_football_1, identifier): In which division was the match between Hibernian, the away team, and Hearts, the home team, played? To which country do
  - gold: `SELECT DISTINCT T2.division, T2.country FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.division WHERE T1.HomeTeam = 'Hearts' AND T1.AwayTeam = 'Hib`
  - pred: `SELECT T1.div, T1.country FROM divisions AS T1 INNER JOIN matchs AS T2 ON T1.div = T2.div WHERE T2.awayteam = 'Hibernian' AND T2.hometeam = 'Hearts'`
- `ood:european_football_1:111` (european_football_1, identifier): Which team had the most final-time home-team goals in the 2021 season's matches of the Bundesliga division?
  - gold: `SELECT T1.HomeTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.division WHERE T2.name = 'Bundesliga' AND T1.season = 2021 ORDER BY T1.FTHG DESC `
  - pred: `SELECT T2.HomeTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.Division WHERE T2.Name = 'Bundesliga' AND T1.Season = 2021 ORDER BY T1.FTHG DESC `

### Arm B_real — first 30 wrong predictions

- `17` (california_schools, identifier): Rank schools by their average score in Writing where the score is greater than 499, showing their charter numbers.
  - gold: `SELECT CharterNum, AvgScrWrite, RANK() OVER (ORDER BY AvgScrWrite DESC NULLS LAST) AS WritingScoreRank FROM schools AS T1 INNER JOIN satscores AS T2 ON CAST(T1.`
  - pred: `SELECT T1.CDSCode, T1.CharterNumber FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.AvgScrWrite > 499 AND NOT T1.CharterNumber IS `
- `49` (california_schools, identifier): Which different county has the most number of closed schools? Please provide the name of each school as well as the clos
  - gold: `SELECT DISTINCT County, School, ClosedDate FROM schools WHERE County = (SELECT County FROM schools WHERE StatusType = 'Closed' GROUP BY County ORDER BY COUNT(Sc`
  - pred: `SELECT T2.County, T1.School, T1.ClosedDate FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1.StatusType = 'Closed' GROUP BY T2.County`
- `73` (california_schools, identifier): What is the free or reduced price meal count for ages 5 to 17 in the Youth Authority School with a mailing street addres
  - gold: `SELECT T1."FRPM Count (Ages 5-17)" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.MailStreet = 'PO Box 1040' AND T2.SOCType = 'You`
  - pred: `SELECT "Free Meal Count (Ages 5-17)" FROM schools WHERE School = 'Youth Authority School' AND MailStreet = 'PO Box 1040'`
- `81` (california_schools, identifier): In which city can you find the school in the state of California with the lowest latitude coordinates and what is its lo
  - gold: `SELECT T2.City, T1."Low Grade", T1."School Name" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.State = 'CA' ORDER BY T2.Latitude `
  - pred: `SELECT city, low_grade, school FROM schools WHERE state = 'CA' ORDER BY latitude NULLS FIRST LIMIT 1`
- `83` (california_schools, identifier): Of the schools that offers a magnet program serving a grade span of Kindergarten to 8th grade, how many offers Multiple 
  - gold: `SELECT T2.City, COUNT(T2.CDSCode) FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.Magnet = 1 AND T2.GSoffered = 'K-8' AND T1."NSLP `
  - pred: `SELECT COUNT(T1.CDSCode), T1.City FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1.GradeSpan = 'K-8' AND T1.Magnet = 1 AND T2."NSLP `
- `82` (california_schools, aggregate): What is the grade span offered in the school with the highest longitude?
  - gold: `SELECT GSoffered FROM schools ORDER BY ABS(longitude) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT GSOffered FROM schools WHERE ABS(Longitude) = (SELECT MAX(ABS(Longitude)) FROM schools)`
- `116` (financial, aggregate): For the client whose loan was approved first in 1993/7/5, what is the increase rate of his/her account balance from 1993
  - gold: `SELECT CAST((SUM(CASE WHEN T3.date = '1998-12-27' THEN T3.balance ELSE 0 END) - SUM(CASE WHEN T3.date = '1993-03-22' THEN T3.balance ELSE 0 END)) AS REAL) * 100`
  - pred: `SELECT (T1.balance - T2.balance) * 100 / NULLIF(T2.balance, 0) FROM trans AS T1 INNER JOIN (SELECT account_id, MAX(balance) AS balance FROM trans WHERE date = '`
- `219` (toxicology, aggregate): What is the percentage of carcinogenic molecules in triple type bonds?
  - gold: `SELECT CAST(COUNT(DISTINCT CASE WHEN T2.label = '+' THEN T2.molecule_id ELSE NULL END) AS REAL) * 100 / NULLIF(COUNT(DISTINCT T2.molecule_id), 0) FROM atom AS T`
  - pred: `SELECT CAST(SUM(CASE WHEN T2.label = '+' THEN 1 ELSE 0 END) AS REAL) * 100 / NULLIF(COUNT(T1.bond_id), 0) FROM bond AS T1 INNER JOIN molecule AS T2 ON T1.molecu`
- `298` (toxicology, aggregate): Calculate the percentage of molecules containing carcinogenic compounds that element is hydrogen.
  - gold: `SELECT CAST(COUNT(CASE WHEN T1.element = 'h' AND T2.label = '+' THEN T2.molecule_id ELSE NULL END) AS REAL) * 100 / NULLIF(COUNT(T2.molecule_id), 0) FROM atom A`
  - pred: `SELECT CAST(SUM(CASE WHEN T1.element = 'h' THEN 1 ELSE 0 END) AS REAL) * 100 / NULLIF(COUNT(T1.element), 0) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.mole`
- `317` (toxicology, aggregate): Calculate the percentage of carcinogenic molecules which contain the Chlorine element.
  - gold: `SELECT COUNT(CASE WHEN T2.label = '+' AND T1.element = 'cl' THEN T2.molecule_id ELSE NULL END) * 100 / NULLIF(COUNT(T2.molecule_id), 0) FROM atom AS T1 INNER JO`
  - pred: `SELECT CAST(SUM(CASE WHEN T1.element = 'Cl' THEN 1 ELSE 0 END) AS REAL) * 100 / NULLIF(COUNT(T1.element), 0) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.mol`
- `135` (financial, grain_fanout): After making a credit card withdrawal, how many account/s with monthly issuance has a negative balance?
  - gold: `SELECT COUNT(T1.account_id) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T1.balance < 0 AND T1.operation = 'VYBER KARTOU' AN`
  - pred: `SELECT COUNT(DISTINCT T1.account_id) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T1.balance < 0 AND T1.operation = 'VYBER K`
- `199` (toxicology, grain_fanout): In the molecule containing sodium atoms, how many are non-carcinogenic?
  - gold: `SELECT COUNT(DISTINCT T2.molecule_id) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'na' AND T2.label = '-'`
  - pred: `SELECT COUNT(T1.molecule_id) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'na' AND T2.label = '-'`
- `230` (toxicology, grain_fanout): What are the elements of the toxicology and label of molecule TR060?
  - gold: `SELECT DISTINCT T1.element, T2.label FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.molecule_id = 'TR060'`
  - pred: `SELECT T1.element, T2.label FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.label = 'toxicology' AND T2.molecule_id = 'TR0`
- `232` (toxicology, grain_fanout): Please list top three molecules that have single bonds between two atoms and are not carcinogenic in alphabetical order.
  - gold: `SELECT DISTINCT T2.molecule_id FROM bond AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.bond_type = '-' AND T2.label = '-' ORDER BY`
  - pred: `SELECT T1.molecule_id FROM molecule AS T1 INNER JOIN bond AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.bond_type = '-' ORDER BY T1.molecule_id NULLS FIRST `
- `290` (toxicology, grain_fanout): Which toxic element can be found in the molecule TR151?
  - gold: `SELECT DISTINCT T.element FROM atom AS T WHERE T.molecule_id = 'TR151'`
  - pred: `SELECT T1.element FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.label = 'TR151' AND T1.element IN ('cl', 'br', 'i', 'pb'`
- `27` (california_schools, period): What is the average score in writing for the schools that were opened after 1991 or closed before 2000? List the school 
  - gold: `SELECT T2.School, T1.AvgScrWrite, T2.Phone FROM schools AS T2 LEFT JOIN satscores AS T1 ON CAST(T2.CDSCode AS BIGINT) = CAST(T1.cds AS BIGINT) WHERE TO_CHAR(CAS`
  - pred: `SELECT T2.avgscrwrite, T1.school, T1.phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE T1.opendate > '1991-12-31' OR T1.closeddat`
- `102` (financial, period): State different accounts who have account opening date before 1997 and own an amount of money greater than 3000USD
  - gold: `SELECT DISTINCT T2.account_id FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE TO_CHAR(CAST(T2.date AS TIMESTAMP), 'YYYY') < '19`
  - pred: `SELECT DISTINCT T1.account_id FROM trans AS T1 INNER JOIN loan AS T2 ON T1.account_id = T2.account_id WHERE T1.date < '1997-01-01' AND T2.amount > 3000`
- `180` (financial, period): Please provide a list of clients who were born between 1983 and 1987 and whose account branch is in East Bohemia, along 
  - gold: `SELECT T1.client_id, T3.account_id FROM client AS T1 INNER JOIN district AS T2 ON T1.district_id = T2.district_id INNER JOIN disp AS T4 ON T1.client_id = T4.cli`
  - pred: `SELECT T1.client_id, T1.gender FROM client AS T1 INNER JOIN disp AS T2 ON T1.client_id = T2.client_id INNER JOIN account AS T3 ON T2.account_id = T3.account_id `
- `1529` (debit_card_specializing, period): What is the amount spent by customer "38508" at the gas stations? How much had the customer spent in January 2012?
  - gold: `SELECT SUM(Price), SUM(CASE WHEN Date >= '2012-01-01' AND Date < '2012-02-01' THEN Price ELSE 0 END) FROM transactions_1k WHERE CustomerID = 38508`
  - pred: `SELECT SUM(T1.Amount), SUM(CASE WHEN T1.Date = '201201' THEN T1.Amount ELSE 0 END) FROM transactions_1k AS T1 INNER JOIN customers AS T2 ON T1.CustomerID = T2.C`
- `158` (financial, syntax): What is the district Id of the account that placed the order with the id 33333?
  - gold: `SELECT T3.district_id FROM "order" AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id INNER JOIN district AS T3 ON T2.district_id = T3.district_id `
  - pred: `SELECT T1.district_id FROM account AS T1 INNER JOIN order AS T2 ON T1.account_id = T2.account_id WHERE T2.order_id = 33333`
- `4` (california_schools, other): Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.
  - gold: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter Funding Type" = 'Directly funded' AND T1."Charter School (`
  - pred: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter School (Y/N)" = 1 AND T2.OpenDate > '2000-01-01'`
- `9` (california_schools, other): Among the schools with the average score in Math over 560 in the SAT test, how many schools are directly charter-funded?
  - gold: `SELECT COUNT(T2."School Code") FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) WHERE T1.AvgScrMath > 560 AND T`
  - pred: `SELECT COUNT(T1.CDSCode) FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.avgscrmath > 560 AND T1.FundingType = 'Directly Charter-F`
- `10` (california_schools, other): For the school with the highest average score in Reading in the SAT test, what is its FRPM count for students aged 5-17?
  - gold: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY T1.AvgScrRead DESC`
  - pred: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode WHERE T1.rtype = 'S' ORDER BY T1.avgscrread DESC NULLS LAST`
- `13` (california_schools, other): Please list the phone numbers of the schools with the top 3 SAT excellence rate.
  - gold: `SELECT T1.Phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.cds ORDER BY CAST(T2.NumGE1500 AS REAL) / NULLIF(T2.NumTstTakr, 0) DESC NULLS L`
  - pred: `SELECT T1.Phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.RType = 'Excellence' ORDER BY T2.NumGE1500 / NULLIF(T2.NumTstTakr,`
- `28` (california_schools, other): Consider the average difference between K-12 enrollment and 15-17 enrollment of schools that are locally funded, list th
  - gold: `SELECT T2.School, T2.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.FundingType = 'Locally funded' AND (T1."Enrollment (K-12)"`
  - pred: `SELECT T2.School, T2.DOC, T2.DocType FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Percent (%) Eligible Free (K-12)" > (SELECT A`
- `ood:disney:16` (disney, identifier): List all of Wolfgang Reitherman's movies and their voice actors.
  - gold: `SELECT T1.name, T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON T1.name = T2.movie WHERE T1.director = 'Wolfgang Reitherman'`
  - pred: `SELECT T1.movie, T2."voice-actor" FROM characters AS T1 INNER JOIN "voice-actors" AS T2 ON T2.movie = T1.movie_title INNER JOIN director AS T3 ON T3.name = T1.m`
- `ood:disney:22` (disney, identifier): What are the total grosses for the movies with Jim Cummings as the voice actor?
  - gold: `SELECT T2.movie_title FROM "voice-actors" AS T1 INNER JOIN movies_total_gross AS T2 ON T2.movie_title = T1.movie WHERE T1."voice-actor" = 'Jim Cummings' ORDER B`
  - pred: `SELECT SUM(T1."total gross") FROM movies_total_gross AS T1 INNER JOIN "voice-actors" AS T2 ON T1.movie_title = T2.movie WHERE T2."voice-actor" = 'Jim Cummings'`
- `ood:disney:51` (disney, identifier): List all the voice actors in the movie directed by Ben Sharpsteen which was released on February 9, 1940.
  - gold: `SELECT T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON TRUE INNER JOIN movies_total_gross AS T3 ON T1.name = T2.movie AND T2.movie = T3.`
  - pred: `SELECT T1."voice-actor" FROM "voice-actors" AS T1 INNER JOIN characters AS T2 ON T1.movie = T2.movie_title WHERE T2.release_date = 'Feb 9, 1940' AND T2.director`
- `ood:european_football_1:78` (european_football_1, identifier): Of all the teams that played as a team away against Caen in the 2010 season, which one has the highest winning percentag
  - gold: `SELECT AwayTeam FROM matchs WHERE HomeTeam = 'Caen' AND season = 2010 AND FTR = 'A' GROUP BY AwayTeam ORDER BY COUNT(AwayTeam) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT T2.AwayTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.Division WHERE T1.HomeTeam = 'Caen' AND T1.Season = 2010 AND T1.FTR = 'H' GROUP B`
- `ood:european_football_1:111` (european_football_1, identifier): Which team had the most final-time home-team goals in the 2021 season's matches of the Bundesliga division?
  - gold: `SELECT T1.HomeTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.division WHERE T2.name = 'Bundesliga' AND T1.season = 2021 ORDER BY T1.FTHG DESC `
  - pred: `SELECT T2.HomeTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.Division WHERE T2.Name = 'Bundesliga' AND T1.Season = 2021 ORDER BY T1.FTHG DESC `

### Arm C — first 30 wrong predictions

- `9` (california_schools, identifier): Among the schools with the average score in Math over 560 in the SAT test, how many schools are directly charter-funded?
  - gold: `SELECT COUNT(T2."School Code") FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) WHERE T1.AvgScrMath > 560 AND T`
  - pred: `SELECT COUNT(T1.CDS) FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.avgscrmath > 560 AND T1.FundingType = 'Directly Charter-Funde`
- `14` (california_schools, identifier): List the top five schools, by descending order, from the highest to the lowest, the most number of Enrollment (Ages 5-17
  - gold: `SELECT T1.NCESSchool FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode ORDER BY T2."Enrollment (Ages 5-17)" DESC NULLS LAST LIMIT 5`
  - pred: `SELECT "ncesschool", "Enrollment (Ages 5-17)" FROM "schools" ORDER BY "Enrollment (Ages 5-17)" DESC NULLS LAST LIMIT 5`
- `73` (california_schools, identifier): What is the free or reduced price meal count for ages 5 to 17 in the Youth Authority School with a mailing street addres
  - gold: `SELECT T1."FRPM Count (Ages 5-17)" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.MailStreet = 'PO Box 1040' AND T2.SOCType = 'You`
  - pred: `SELECT "Free Meal Count (Ages 5-17)" FROM frpm WHERE "School Name" = 'Youth Authority School' AND "Mail Street" = 'PO Box 1040'`
- `81` (california_schools, identifier): In which city can you find the school in the state of California with the lowest latitude coordinates and what is its lo
  - gold: `SELECT T2.City, T1."Low Grade", T1."School Name" FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.State = 'CA' ORDER BY T2.Latitude `
  - pred: `SELECT city, "Low Grade", school FROM schools WHERE state = 'CA' ORDER BY latitude NULLS FIRST LIMIT 1`
- `82` (california_schools, identifier): What is the grade span offered in the school with the highest longitude?
  - gold: `SELECT GSoffered FROM schools ORDER BY ABS(longitude) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT DISTINCT "High Grade", "Low Grade" FROM "frpm" WHERE "CDSCode" = (SELECT "CDSCode" FROM "schools" ORDER BY "longitude" DESC NULLS LAST LIMIT 1) ORDER BY `
- `17` (california_schools, aggregate): Rank schools by their average score in Writing where the score is greater than 499, showing their charter numbers.
  - gold: `SELECT CharterNum, AvgScrWrite, RANK() OVER (ORDER BY AvgScrWrite DESC NULLS LAST) AS WritingScoreRank FROM schools AS T1 INNER JOIN satscores AS T2 ON CAST(T1.`
  - pred: `SELECT T1.CDSCode, T1.CharterNum FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.avgscrwrite > 499 AND NOT T1.CharterNum IS NULL O`
- `28` (california_schools, aggregate): Consider the average difference between K-12 enrollment and 15-17 enrollment of schools that are locally funded, list th
  - gold: `SELECT T2.School, T2.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.FundingType = 'Locally funded' AND (T1."Enrollment (K-12)"`
  - pred: `SELECT T2.School, T2.DOC FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."2013-14 CALPADS Fall 1 Certification Status" = 1 AND T2.F`
- `43` (california_schools, aggregate): What is the average math score of the school with the lowest average score for all subjects, and in which county is it l
  - gold: `SELECT T1.AvgScrMath, T2.County FROM satscores AS T1 INNER JOIN schools AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) WHERE NOT T1.AvgScrMath IS `
  - pred: `SELECT T2.AvgScrMath, T1.County FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.CDS WHERE T2.AvgScrMath + T2.AvgScrRead + T2.AvgScrWrite = (SEL`
- `49` (california_schools, aggregate): Which different county has the most number of closed schools? Please provide the name of each school as well as the clos
  - gold: `SELECT DISTINCT County, School, ClosedDate FROM schools WHERE County = (SELECT County FROM schools WHERE StatusType = 'Closed' GROUP BY County ORDER BY COUNT(Sc`
  - pred: `SELECT DISTINCT T1.County, T1.School, T1.ClosedDate FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1.StatusType = 'Closed' ORDER BY `
- `84` (california_schools, aggregate): What are the two most common first names among the school administrators? Indicate the district to which they administer
  - gold: `SELECT DISTINCT T1.AdmFName1, T1.District FROM schools AS T1 INNER JOIN (SELECT admfname1 FROM schools GROUP BY admfname1 ORDER BY COUNT(admfname1) DESC NULLS L`
  - pred: `SELECT DISTINCT "admfname1", "district" FROM "schools" ORDER BY "admfname1"`
- `91` (financial, grain_fanout): The average unemployment ratio of 1995 and 1996, which one has higher percentage?
  - gold: `SELECT DISTINCT CASE WHEN AVG(A13) > AVG(A12) THEN '1996' ELSE '1995' END FROM district`
  - pred: `SELECT CASE WHEN AVG(A12) > AVG(A13) THEN '1995' ELSE '1996' END FROM district GROUP BY A12, A13`
- `135` (financial, grain_fanout): After making a credit card withdrawal, how many account/s with monthly issuance has a negative balance?
  - gold: `SELECT COUNT(T1.account_id) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T1.balance < 0 AND T1.operation = 'VYBER KARTOU' AN`
  - pred: `SELECT COUNT(DISTINCT T1.account_id) FROM account AS T1 INNER JOIN trans AS T2 ON T1.account_id = T2.account_id WHERE T2.balance < 0 AND T2.operation = 'VYBER K`
- `145` (financial, grain_fanout): Who are the account holder identification numbers whose who have transactions on the credit card with the amount is less
  - gold: `SELECT T1.account_id FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE TO_CHAR(CAST(T1.date AS TIMESTAMP), 'YYYY') = '1998' AND T`
  - pred: `SELECT DISTINCT "account_id" FROM "trans" WHERE "account_id" IN (SELECT "account_id" FROM "card" WHERE "type" = 'credit') AND "amount" < (SELECT AVG("amount") F`
- `150` (financial, grain_fanout): How many accounts in North Bohemia has made a transaction with the partner's bank being AB?
  - gold: `SELECT COUNT(T2.account_id) FROM district AS T1 INNER JOIN account AS T2 ON T1.district_id = T2.district_id INNER JOIN trans AS T3 ON T2.account_id = T3.account`
  - pred: `SELECT COUNT(DISTINCT T1.account_id) FROM trans AS T1 INNER JOIN district AS T2 ON T1.account_id = T2.district_id WHERE T2.a3 = 'Nord Bohemia' AND T1.bank = 'AB`
- `199` (toxicology, grain_fanout): In the molecule containing sodium atoms, how many are non-carcinogenic?
  - gold: `SELECT COUNT(DISTINCT T2.molecule_id) FROM atom AS T1 INNER JOIN molecule AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'na' AND T2.label = '-'`
  - pred: `SELECT COUNT(T1.molecule_id) FROM molecule AS T1 INNER JOIN atom AS T2 ON T1.molecule_id = T2.molecule_id WHERE T2.element = 'na' AND T1.label = '-'`
- `27` (california_schools, period): What is the average score in writing for the schools that were opened after 1991 or closed before 2000? List the school 
  - gold: `SELECT T2.School, T1.AvgScrWrite, T2.Phone FROM schools AS T2 LEFT JOIN satscores AS T1 ON CAST(T2.CDSCode AS BIGINT) = CAST(T1.cds AS BIGINT) WHERE TO_CHAR(CAS`
  - pred: `SELECT T2.avgscrwrite, T1.school, T1.phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE (T1.opendate > '1991-12-31' OR T1.closedda`
- `169` (financial, period): What was the growth rate of the total amount of loans across all accounts for a male client between 1996 and 1997?
  - gold: `SELECT CAST((SUM(CASE WHEN TO_CHAR(CAST(T1.date AS TIMESTAMP), 'YYYY') = '1997' THEN T1.amount ELSE 0 END) - SUM(CASE WHEN TO_CHAR(CAST(T1.date AS TIMESTAMP), '`
  - pred: `SELECT CAST(SUM(CASE WHEN TO_CHAR(CAST(T1.birth_date AS TIMESTAMP), 'YYYY') = '1997' THEN T3.amount ELSE 0 END) - SUM(CASE WHEN TO_CHAR(CAST(T1.birth_date AS TI`
- `1340` (student_club, period): Calculate the difference of the total amount spent in all events by the Student_Club in year 2019 and 2020.
  - gold: `SELECT SUM(CASE WHEN SUBSTRING(T1.event_date FROM 1 FOR 4) = '2019' THEN T2.spent ELSE 0 END) - SUM(CASE WHEN SUBSTRING(T1.event_date FROM 1 FOR 4) = '2020' THE`
  - pred: `SELECT SUM(CASE WHEN TO_CHAR(CAST(event_date AS TIMESTAMP), 'YYYY') = '2019' THEN T2.spent ELSE 0 END) - SUM(CASE WHEN TO_CHAR(CAST(event_date AS TIMESTAMP), 'Y`
- `1529` (debit_card_specializing, period): What is the amount spent by customer "38508" at the gas stations? How much had the customer spent in January 2012?
  - gold: `SELECT SUM(Price), SUM(CASE WHEN Date >= '2012-01-01' AND Date < '2012-02-01' THEN Price ELSE 0 END) FROM transactions_1k WHERE CustomerID = 38508`
  - pred: `SELECT SUM(Amount), SUM(CASE WHEN Date = '201201' THEN Amount ELSE 0 END) FROM transactions_1k WHERE CustomerID = 38508`
- `708` (codebase_community, syntax): List the creation date and age of the user that commented with webiste.
  - gold: `SELECT T2.CreationDate, T2.Age FROM comments AS T1 INNER JOIN users AS T2 ON T1.UserId = T2.Id WHERE T1.text LIKE '%http://%'`
  - pred: `SELECT T1.CreationDate, T1.Age FROM Users AS T1 INNER JOIN Comments AS T2 ON T1.Id = T2.UserId WHERE T2.Text LIKE '%http://%'  **Created Question**: How many us`
- `4` (california_schools, other): Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.
  - gold: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter Funding Type" = 'Directly funded' AND T1."Charter School (`
  - pred: `SELECT T2.Phone FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter School (Y/N)" = 1 AND T2.OpenDate > '2000-01-01'`
- `10` (california_schools, other): For the school with the highest average score in Reading in the SAT test, what is its FRPM count for students aged 5-17?
  - gold: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY T1.AvgScrRead DESC`
  - pred: `SELECT T2."FRPM Count (Ages 5-17)" FROM satscores AS T1 INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode WHERE T1.rtype = 'Reading' ORDER BY T1.avgscrread DESC NULL`
- `13` (california_schools, other): Please list the phone numbers of the schools with the top 3 SAT excellence rate.
  - gold: `SELECT T1.Phone FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.CDSCode = T2.cds ORDER BY CAST(T2.NumGE1500 AS REAL) / NULLIF(T2.NumTstTakr, 0) DESC NULLS L`
  - pred: `SELECT T2.Phone FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.CDS = T2.CDSCode WHERE T1.RType = 'Excellence' ORDER BY T1.NumGE1500 / NULLIF(T1.NumTstTakr,`
- `34` (california_schools, other): What is the free rate for students between the ages of 5 and 17 at the school run by Kacey Gibson?
  - gold: `SELECT CAST(T2."Free Meal Count (Ages 5-17)" AS REAL) / NULLIF(T2."Enrollment (Ages 5-17)", 0) FROM schools AS T1 INNER JOIN frpm AS T2 ON T1.CDSCode = T2.CDSCo`
  - pred: `SELECT "Percent (%) Eligible Free (Ages 5-17)" FROM frpm WHERE "School Name" = 'Kacey Gibson'`
- `37` (california_schools, other): What is the complete address of the school with the lowest excellence rate? Indicate the Street, City, Zip and State.
  - gold: `SELECT T2.Street, T2.City, T2.State, T2.Zip FROM satscores AS T1 INNER JOIN schools AS T2 ON CAST(T1.cds AS BIGINT) = CAST(T2.CDSCode AS BIGINT) ORDER BY CAST(T`
  - pred: `SELECT T2.Street, T2.City, T2.Zip, T2.State FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.CDS = T2.CDSCode WHERE T1.NumTstTakr <> 0 ORDER BY CAST(T1.NumGE`
- `ood:disney:16` (disney, identifier): List all of Wolfgang Reitherman's movies and their voice actors.
  - gold: `SELECT T1.name, T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON T1.name = T2.movie WHERE T1.director = 'Wolfgang Reitherman'`
  - pred: `SELECT T1.movie, T2."voice-actor" FROM characters AS T1 INNER JOIN "voice-actors" AS T2 ON T1.movie_title = T2.movie INNER JOIN director AS T3 ON T3.name = T1.m`
- `ood:disney:22` (disney, identifier): What are the total grosses for the movies with Jim Cummings as the voice actor?
  - gold: `SELECT T2.movie_title FROM "voice-actors" AS T1 INNER JOIN movies_total_gross AS T2 ON T2.movie_title = T1.movie WHERE T1."voice-actor" = 'Jim Cummings' ORDER B`
  - pred: `SELECT SUM(T1."total gross") FROM movies_total_gross AS T1 INNER JOIN "voice-actors" AS T2 ON T1."movie_title" = T2.movie WHERE T2."voice-actor" = 'Jim Cummings`
- `ood:disney:51` (disney, identifier): List all the voice actors in the movie directed by Ben Sharpsteen which was released on February 9, 1940.
  - gold: `SELECT T2."voice-actor" FROM director AS T1 INNER JOIN "voice-actors" AS T2 ON TRUE INNER JOIN movies_total_gross AS T3 ON T1.name = T2.movie AND T2.movie = T3.`
  - pred: `SELECT T1."voice-actor" FROM "voice-actors" AS T1 INNER JOIN characters AS T2 ON T1.movie = T2.movie_title WHERE T2.release_date = 'Feb 9, 1940' AND T2.director`
- `ood:european_football_1:78` (european_football_1, identifier): Of all the teams that played as a team away against Caen in the 2010 season, which one has the highest winning percentag
  - gold: `SELECT AwayTeam FROM matchs WHERE HomeTeam = 'Caen' AND season = 2010 AND FTR = 'A' GROUP BY AwayTeam ORDER BY COUNT(AwayTeam) DESC NULLS LAST LIMIT 1`
  - pred: `SELECT T2.AwayTeam FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.Division WHERE T1.HomeTeam = 'Caen' AND T1.Season = 2010 AND T1.FTR = 'H' GROUP B`
- `ood:european_football_1:85` (european_football_1, identifier): In which division was the match between Hibernian, the away team, and Hearts, the home team, played? To which country do
  - gold: `SELECT DISTINCT T2.division, T2.country FROM matchs AS T1 INNER JOIN divisions AS T2 ON T1.Div = T2.division WHERE T1.HomeTeam = 'Hearts' AND T1.AwayTeam = 'Hib`
  - pred: `SELECT T1.div, T1.country FROM divisions AS T1 INNER JOIN matchs AS T2 ON T1.div = T2.div WHERE T2.awayteam = 'Hibernian' AND T2.hometeam = 'Hearts'`

## Provenance

- Raw pass@1: `cluster/eval_reports/arms/t47_{A,B_mixed,B_real,C}_{dev217,ood876}_b1/report.json`.
- EX + repair: `cluster/eval_reports/main_table/*_repair/report.json` (repaired predictions in `/tmp` at build time; the repair pass is deterministic and re-runnable with `cluster/repair_pass.py`).
- Paired: `cluster/eval_reports/arms/t36_paired_real_vs_{A,mixed}_*` and `t47_paired_C_vs_{A,B_real,B_mixed}_*`.
- Historical 241: `cluster/eval_reports/t4/ex241_{A,B}/report.json`.

