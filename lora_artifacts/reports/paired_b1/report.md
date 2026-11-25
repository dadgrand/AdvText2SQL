# Batch-1 paired ledger: fixes and regressions by id

Serving contract: batch size 1, no padding, no truncation, greedy, `max_new_tokens=256`, stop `<|im_end|>`, the training renderer in serve mode, no repair. Arm A = base Qwen2.5-Coder-7B-Instruct; arm B = 7B + LoRA (adapter sha256 `d9dca06e…`, step 200). Predictions: `cluster/preds/{shard}_{A,B}.json`.

This report supersedes the earlier batch-8 paired report. Batch-8 decoding was left-padded and shape-sensitive (t38); those files stay in `cluster/eval_reports/t4/b8_*` for the record and are not a serving number. Only the batch-1 numbers below may be quoted.

## Headline

| shard | scored ids | EX A | EX B | Δ | 95 % CI | fixes | regressions | net | McNemar p |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| `ood876` | 811 | 574/811 | 638/811 | +7.89 pp | [5.06, 10.85] | 105 | 41 | +64 | 1.17e-07 |
| `dev216` | 216 | 87/216 | 101/216 | +6.48 pp | [0.00, 12.96] | 31 | 17 | +14 | 0.0595 |
| `ex241` | 209 | 83/209 | 99/209 | +7.66 pp | [0.96, 13.88] | 32 | 16 | +16 | 0.0293 |

## Arithmetic check (from the per-id ledger, not from the aggregate)

| shard | fixes − regressions | net (= EX B − EX A) | equal | ledger ids = pair n | b/c match harness |
|---|---:|---:|---|---|---|
| `ood876` | 105 − 41 = 64 | 638 − 574 = 64 | yes | yes | yes |
| `dev216` | 31 − 17 = 14 | 101 − 87 = 14 | yes | yes | yes |
| `ex241` | 32 − 16 = 16 | 99 − 83 = 16 | yes | yes | yes |

- OOD-876: fixes − regressions **105 − 41 = 64**; EX B − EX A **638 − 574 = 64**.
- dev-216: fixes − regressions **31 − 17 = 14**; EX B − EX A **101 − 87 = 14**.
- historical 241: fixes − regressions **32 − 16 = 16**; EX B − EX A **99 − 83 = 16**.

Every scored id appears in the matching `paired_*.jsonl` with its question, db_id, gold SQL and both predictions; the fix and regression lists below are generated from those files. To check a number by hand: `jq -r 'select(.change=="fix") | .question_id' paired_ood876.jsonl | wc -l` (105), the same with `regression` (41), and `wc -l` (811).

## By database — OOD-876

| database | scored | share | fixes | regressions | net | EX A | EX B |
|---|---:|---:|---:|---:|---:|---:|---:|
| `movie_3` | 189 | 23.3 % | 21 | 11 | +10 | 73.0 % | 78.3 % |
| `movies_4` | 134 | 16.5 % | 13 | 5 | +8 | 78.4 % | 84.3 % |
| `talkingdata` | 109 | 13.4 % | 14 | 2 | +12 | 83.5 % | 94.5 % |
| `law_episode` | 89 | 11.0 % | 19 | 2 | +17 | 56.2 % | 75.3 % |
| `disney` | 74 | 9.1 % | 17 | 10 | +7 | 52.7 % | 62.2 % |
| `menu` | 68 | 8.4 % | 4 | 3 | +1 | 66.2 % | 67.6 % |
| `sales_in_weather` | 42 | 5.2 % | 6 | 2 | +4 | 57.1 % | 66.7 % |
| `social_media` | 39 | 4.8 % | 5 | 2 | +3 | 82.1 % | 89.7 % |
| `european_football_1` ⚠ | 36 | 4.4 % | 3 | 4 | -1 | 72.2 % | 69.4 % |
| `music_tracker` | 31 | 3.8 % | 3 | 0 | +3 | 77.4 % | 87.1 % |

**Net negative databases:** `european_football_1` — the adapter loses more there than it fixes.

## By database — dev-216

| database | scored | share | fixes | regressions | net | EX A | EX B |
|---|---:|---:|---:|---:|---:|---:|---:|
| `superhero` | 36 | 16.7 % | 6 | 4 | +2 | 63.9 % | 69.4 % |
| `student_club` | 30 | 13.9 % | 4 | 1 | +3 | 56.7 % | 66.7 % |
| `card_games` ⚠ | 28 | 13.0 % | 1 | 2 | -1 | 46.4 % | 42.9 % |
| `financial` | 27 | 12.5 % | 8 | 1 | +7 | 7.4 % | 33.3 % |
| `toxicology` | 27 | 12.5 % | 5 | 4 | +1 | 37.0 % | 40.7 % |
| `codebase_community` | 26 | 12.0 % | 3 | 2 | +1 | 57.7 % | 61.5 % |
| `california_schools` ⚠ | 23 | 10.6 % | 2 | 3 | -1 | 13.0 % | 8.7 % |
| `debit_card_specializing` | 19 | 8.8 % | 2 | 0 | +2 | 21.1 % | 31.6 % |

**Net negative databases:** `california_schools`, `card_games` — the adapter loses more there than it fixes.

## By database — historical 241

| database | scored | share | fixes | regressions | net | EX A | EX B |
|---|---:|---:|---:|---:|---:|---:|---:|
| `student_club` | 33 | 15.8 % | 5 | 1 | +4 | 51.5 % | 63.6 % |
| `toxicology` | 32 | 15.3 % | 6 | 5 | +1 | 37.5 % | 40.6 % |
| `superhero` ⚠ | 30 | 14.4 % | 3 | 4 | -1 | 76.7 % | 73.3 % |
| `financial` | 26 | 12.4 % | 8 | 1 | +7 | 7.7 % | 34.6 % |
| `card_games` ⚠ | 24 | 11.5 % | 1 | 2 | -1 | 50.0 % | 45.8 % |
| `codebase_community` | 23 | 11.0 % | 3 | 1 | +2 | 43.5 % | 52.2 % |
| `debit_card_specializing` | 21 | 10.0 % | 4 | 0 | +4 | 23.8 % | 42.9 % |
| `california_schools` | 20 | 9.6 % | 2 | 2 | +0 | 10.0 % | 10.0 % |

**Net negative databases:** `card_games`, `superhero` — the adapter loses more there than it fixes.

## Examples

### OOD-876 fixes (first rows of the ledger)

- `ood:disney:1` (disney): A `SELECT T2.director FROM characters AS T1 JOIN director AS T2 ON T1.movie_title = T2.movie WHERE T1.song = 'Once Upon a Dream'` → B `SELECT T1.director FROM director AS T1 INNER JOIN characters AS T2 ON T1.name = T2.movie_title WHERE T2.song = 'Once Upon a Dream'`
- `ood:european_football_1:101` (european_football_1): A `SELECT hometeam FROM matchs WHERE season = 2021 AND div = 'Bundesliga' AND (hometeam = 'Augsburg' OR hometeam = 'Mainz') AND ftr = 'H' GROUP` → B `SELECT CASE WHEN SUM(CASE WHEN T2.HomeTeam = 'Augsburg' THEN 1 ELSE 0 END) > SUM(CASE WHEN T2.HomeTeam = 'Mainz 05' THEN 1 ELSE 0 END) THEN `
- `ood:law_episode:118` (law_episode): A `SELECT SUM(votes) FROM episode AS e JOIN vote AS v ON e.episode_id = v.episode_id WHERE e.title = 'Cherished'` → B `SELECT SUM(T2.votes) FROM episode AS T1 INNER JOIN vote AS T2 ON T2.episode_id = T1.episode_id WHERE T1.title = 'Cherished'`
- `ood:menu:215` (menu): A `SELECT CASE WHEN SUM(CASE WHEN menu.name = 'Zentral Theater Terrace' THEN menupage.full_width ELSE 0 END) > SUM(CASE WHEN menu.name = 'Young` → B `SELECT CASE WHEN SUM(CASE WHEN T2.name = 'Zentral Theater Terrace' THEN T1.full_width ELSE 0 END) > SUM(CASE WHEN T2.name = 'Young''s Hotel'`

### OOD-876 regressions

- `ood:disney:23` (disney): A `SELECT T1.movie_title FROM movies_total_gross AS T1 JOIN director AS T2 ON T1.movie_title = T2.name WHERE T2.director = 'Ron Clements' ORDER` → B `SELECT T1.movie_title FROM movies_total_gross AS T1 INNER JOIN director AS T2 ON T1.movie_title = T2.name WHERE T2.director = 'Ron Clements'`
- `ood:european_football_1:107` (european_football_1): A `SELECT T1.awayteam FROM matchs AS T1 JOIN divisions AS T2 ON T1.div = T2.division WHERE T2.name = 'Ligue 2' AND T1.ftr = 'A' ORDER BY T1.fth` → B `SELECT T2.HOMEteam FROM divisions AS T1 INNER JOIN matchs AS T2 ON T1.division = T2.DIV WHERE T1.Name = 'Ligue 2' ORDER BY T2.FTHG + T2.FTAG`
- `ood:law_episode:143` (law_episode): A `SELECT T1.name FROM person AS T1 JOIN credit AS T2 ON T1.person_id = T2.person_id JOIN episode AS T3 ON T2.episode_id = T3.episode_id WHERE ` → B `SELECT T2.name FROM episode AS T1 INNER JOIN credit AS T2 ON T1.episode_id = T2.episode_id INNER JOIN person AS T3 ON T3.person_id = T2.pers`
- `ood:menu:207` (menu): A `SELECT dish.name FROM dish JOIN menuitem ON dish.id = menuitem.dish_id JOIN menupage ON menuitem.menu_page_id = menupage.id JOIN menu ON men` → B `SELECT T3.name FROM menu AS T1 INNER JOIN menupage AS T2 ON T1.id = T2.menu_id INNER JOIN dish AS T3 ON T3.id = T2.dish_id WHERE T1.name = '`

### dev-216 fixes

- `35` (california_schools): A `SELECT T1.admemail1 FROM schools AS T1 JOIN frpm AS T2 ON T1.cdscode = T2.cdscode WHERE T2.`Charter School (Y/N)` = 1 ORDER BY T2.`Enrollmen` → B `SELECT T2.admemail1 FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T1."Charter School (Y/N)" = 1 ORDER BY T1."Enr`
- `498` (card_games): A `SELECT T2.translation FROM cards AS T1 JOIN set_translations AS T2 ON T1.setcode = T2.setcode WHERE T2.language = 'French' AND T2.translatio` → B `SELECT T1.Translation FROM set_translations AS T1 INNER JOIN cards AS T2 ON T1.setcode = T2.setcode WHERE T2.name = 'Tendo Ice Bridge' AND T`
- `592` (codebase_community): A `SELECT COUNT(UserId) FROM badges GROUP BY UserId HAVING COUNT(Name) > 5` → B `SELECT COUNT(UserId) FROM (SELECT UserId FROM Badges GROUP BY UserId HAVING COUNT(Name) > 5) AS T1`
- `1471` (debit_card_specializing): A `SELECT count(CASE WHEN currency = 'EUR' THEN 1 ELSE NULL END) / NULLIF(count(CASE WHEN currency = 'CZK' THEN 1 ELSE NULL END), 0) AS ratio F` → B `SELECT CAST(SUM(CASE WHEN Currency = 'EUR' THEN 1 ELSE 0 END) AS REAL) / NULLIF(SUM(CASE WHEN Currency = 'CZK' THEN 1 ELSE 0 END), 0) FROM C`

### dev-216 regressions

- `36` (california_schools): A `SELECT T1.admfname1, T1.admlname1, T1.admfname2, T1.admlname2, T1.admfname3, T1.admlname3 FROM schools AS T1 JOIN satscores AS T2 ON T1.cdsc` → B `SELECT T1.admfname1, T1.admlname1 FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE T2.NumGE1500 = (SELECT MAX(NumG`
- `409` (card_games): A `SELECT COUNT(*) FROM cards c JOIN legalities l ON c.uuid = l.uuid JOIN rulings r ON c.uuid = r.uuid WHERE l.format = 'premodern' AND r.text ` → B `SELECT COUNT(*) FROM cards AS T1 INNER JOIN legalities AS T2 ON T1.uuid = T2.uuid WHERE T2.format = 'premodern' AND T1.text = 'This is a tri`
- `563` (codebase_community): A `SELECT T2.favoritecount FROM comments AS T1 INNER JOIN posts AS T2 ON T1.postid = T2.id WHERE T1.userid = '3025' AND T1.creationdate = '2014` → B `SELECT T2.FavoriteCount FROM PostHistory AS T1 INNER JOIN Posts AS T2 ON T1.PostId = T2.Id WHERE T1.UserId = 3025 AND T1.CreationDate = '201`
- `179` (financial): A `SELECT SUM(amount) FROM trans WHERE account_id IN (SELECT account_id FROM disp WHERE client_id = 617) AND date BETWEEN '1998-01-01' AND '199` → B `SELECT SUM(T1.amount) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T2.client_id = 617 AND TO_CHAR(CAST(T`

### historical 241 fixes

- `50` (california_schools): A `SELECT T2.street, T2.school FROM satscores AS T1 JOIN schools AS T2 ON T1.cds = T2.cdscode ORDER BY T1.avgscrmath DESC LIMIT 1 OFFSET 6` → B `SELECT T2.Street, T2.School FROM satscores AS T1 INNER JOIN schools AS T2 ON T1.CDS = T2.CDSCode ORDER BY T1.AvgScrMath DESC NULLS LAST LIMI`
- `498` (card_games): A `SELECT T2.translation FROM cards AS T1 JOIN set_translations AS T2 ON T1.setcode = T2.setcode WHERE T2.language = 'French' AND T2.translatio` → B `SELECT T1.Translation FROM set_translations AS T1 INNER JOIN cards AS T2 ON T1.setcode = T2.setcode WHERE T2.name = 'Tendo Ice Bridge' AND T`
- `592` (codebase_community): A `SELECT COUNT(UserId) FROM badges GROUP BY UserId HAVING COUNT(Name) > 5` → B `SELECT COUNT(UserId) FROM (SELECT UserId FROM Badges GROUP BY UserId HAVING COUNT(Name) > 5) AS T1`
- `1471` (debit_card_specializing): A `SELECT count(CASE WHEN currency = 'EUR' THEN 1 ELSE NULL END) / NULLIF(count(CASE WHEN currency = 'CZK' THEN 1 ELSE NULL END), 0) AS ratio F` → B `SELECT CAST(SUM(CASE WHEN Currency = 'EUR' THEN 1 ELSE 0 END) AS REAL) / NULLIF(SUM(CASE WHEN Currency = 'CZK' THEN 1 ELSE 0 END), 0) FROM C`

### historical 241 regressions

- `36` (california_schools): A `SELECT T1.admfname1, T1.admlname1, T1.admfname2, T1.admlname2, T1.admfname3, T1.admlname3 FROM schools AS T1 JOIN satscores AS T2 ON T1.cdsc` → B `SELECT T1.admfname1, T1.admlname1 FROM schools AS T1 INNER JOIN satscores AS T2 ON T1.cdscode = T2.cds WHERE T2.NumGE1500 = (SELECT MAX(NumG`
- `409` (card_games): A `SELECT COUNT(*) FROM cards c JOIN legalities l ON c.uuid = l.uuid JOIN rulings r ON c.uuid = r.uuid WHERE l.format = 'premodern' AND r.text ` → B `SELECT COUNT(*) FROM cards AS T1 INNER JOIN legalities AS T2 ON T1.uuid = T2.uuid WHERE T2.format = 'premodern' AND T1.text = 'This is a tri`
- `563` (codebase_community): A `SELECT T2.favoritecount FROM comments AS T1 INNER JOIN posts AS T2 ON T1.postid = T2.id WHERE T1.userid = '3025' AND T1.creationdate = '2014` → B `SELECT T2.FavoriteCount FROM PostHistory AS T1 INNER JOIN Posts AS T2 ON T1.PostId = T2.Id WHERE T1.UserId = 3025 AND T1.CreationDate = '201`
- `179` (financial): A `SELECT SUM(amount) FROM trans WHERE account_id IN (SELECT account_id FROM disp WHERE client_id = 617) AND date BETWEEN '1998-01-01' AND '199` → B `SELECT SUM(T1.amount) FROM trans AS T1 INNER JOIN account AS T2 ON T1.account_id = T2.account_id WHERE T2.client_id = 617 AND TO_CHAR(CAST(T`

<!-- breal-section:start -->
## B_real comparisons (batch 1)

Per-id ledgers: `paired_ood876_breal.jsonl` (B_real vs A), `paired_ood876_breal_vs_bmixed.jsonl` (B_real vs B_mixed), `paired_dev216_breal.jsonl`, `paired_dev216_breal_vs_bmixed.jsonl`; per-database aggregates in `by_db_breal.json`.

| comparison | shard | EX baseline → current | Δ | 95 % CI | fixes / regressions | net | McNemar p | ledger |
|---|---|---|---|---|---:|---:|---:|---|
| A → B_mixed | ood876 | 574/811 → 638/811 | +7.89 п.п. | [+5.06; +10.85] | 105 / 41 | +64 | 1.17e-07 | `paired_ood876.jsonl` |
| A → B_real | ood876 | 574/811 → 649/811 | +9.25 п.п. | [+6.41; +12.21] | 112 / 37 | +75 | 5.79e-10 | `paired_ood876_breal.jsonl` |
| B_mixed → B_real | ood876 | 638/811 → 649/811 | +1.36 п.п. | [-0.74; +3.33] | 40 / 29 | +11 | 0.228 | `paired_ood876_breal_vs_bmixed.jsonl` |
| A → B_real | dev216 | 87/216 → 103/216 | +7.41 п.п. | [+0.93; +13.89] | 35 / 19 | +16 | 0.0402 | `paired_dev216_breal.jsonl` |
| B_mixed → B_real | dev216 | 101/216 → 103/216 | +0.93 п.п. | [-4.63; +6.48] | 20 / 18 | +2 | 0.871 | `paired_dev216_breal_vs_bmixed.jsonl` |

**The +1.36 pp of B_real over B_mixed (OOD-876)** is the balance of 40 fixes and 29 regressions over 69 discordant questions. The 69 discordant questions (net +11) are **concentrated**: `movies_4` alone carries +6 of the net +11 (55 %), and `law_episode` -2, `music_tracker` -3 offset it on the other side — so the +1.36 pp is **not a general effect**.

Per database (net of fixes − regressions): `movie_3` +3; `menu` +0; `movies_4` +6; `talkingdata` +2; `disney` +1; `law_episode` -2; `european_football_1` +3; `sales_in_weather` +1; `music_tracker` -3; `social_media` +0.

Reading rules: dev-216 is the selection shard (all dev numbers are optimistic by construction); OOD-876 is confirmation only; every number is batch-1 decoding; the historical 241 shard was not run for B_real or B_mixed, so no 241 cell exists here (not a zero).

<!-- breal-section:end -->



## Provenance

- Predictions: `cluster/preds/{shard}_{A,B}.json` (batch-1 contract, sidecars with model/adapter hashes).
- Execution ledgers: `cluster/eval_reports/t4/{shard}_{A,B}/per_question_*.jsonl`.
- Pairing and intervals: `cluster/eval_reports/paired_b1/{shard}/report.json` (harness, 10 000 bootstrap resamples, seed 0).
- This ledger: `cluster/build_paired_ledger.py`; every row is checksummed by the arithmetic block above.

