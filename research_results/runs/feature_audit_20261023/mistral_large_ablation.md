# Historical large-set single-feature ablations

Recomputed from saved results, not copied from small-set recommendations.
Scores are observations, not causal estimates: runs were not repeated and dependencies matter.

Baseline BIRD: 92/241 = 38.17%. Baseline Ambrosia: 127/227 = 55.95%.

Ambrosia columns use the common 227-question baseline coverage; FEAT_6 originally reported 154/240.

| Flag | Description | BIRD correct / 241 | Gains / losses | Ambrosia correct / 227 | Gains / losses |
| --- | --- | ---: | ---: | ---: | ---: |
| FEAT_1 | FK / PK relationships в системном промпте (**E3**) | 93 | +9 / -8 | 136 | +16 / -7 |
| FEAT_2 | Column statistics (min/max/top-K/диапазон дат) в обоих промптах (**E4**) | 94 | +15 / -13 | 146 | +30 / -11 |
| FEAT_3 | Regex-детекция реального типа TEXT-колонок (**E5**) | 95 | +5 / -2 | 129 | +7 / -5 |
| FEAT_4 | `pg_stat_user_tables.n_live_tup` + fallback на `COUNT(*)` для row-count | 97 | +7 / -2 | 125 | +3 / -5 |
| FEAT_5 | Light-режим извлечения схемы — имена + типы (**E1**) | 95 | +5 / -2 | 126 | +6 / -7 |
| FEAT_6 | Heavy-режим — sample rows + low-cardinality enums (**E2**) | 89 | +11 / -14 | 145 | +29 / -11 |
| FEAT_7 | Компактное форматирование статистик (ID → `unique (id)`, top-K только для high-cardinality) | 94 | +3 / -1 | 127 | +4 / -4 |
| FEAT_8 | Rollback abort-нутых PostgreSQL-транзакций внутри exploration | 95 | +4 / -1 | 126 | +5 / -6 |
| FEAT_9 | Дамп расширенной схемы (schema + relationships + stats) в `db_schemas.json` | 94 | +5 / -3 | 126 | +5 / -6 |
| FEAT_10 | Замер времени `build()` + лог числа таблиц / символов промпта | 94 | +2 / -0 | 125 | +2 / -4 |
| FEAT_11 | JSON-структурное логирование пайплайна | 93 | +2 / -1 | 128 | +7 / -6 |
| FEAT_12 | Optimistic ambiguity fallback — env `OPTIMISTIC_AMBIGUITY_FALLBACK` (**X1**) | 97 | +7 / -2 | 162 | +58 / -23 |
| FEAT_13 | Few-shot ambiguity-промпт «3 условия одновременно» (**X2**) | 97 | +10 / -5 | 141 | +40 / -26 |
| FEAT_14 | Жёсткие правила в SQL-промпте (DISTINCT после SELECT, CAST дат, не транслитерировать имена) | 103 | +25 / -14 | 128 | +6 / -5 |
| FEAT_15 | `sanitize_sql` — markdown / DISTINCT / CAST / NULLIF чистка (**X3** часть 1) | 95 | +5 / -2 | 127 | +6 / -6 |
| FEAT_16 | Валидация через `sqlglot` + запрет DROP/DELETE/UPDATE (**X3** часть 2) | 94 | +3 / -1 | 127 | +4 / -4 |
| FEAT_17 | Retry-цикл до `MAX_RETRIES=7` при провале validate | 97 | +6 / -1 | 126 | +5 / -6 |
| FEAT_18 | LLM-вызовы с exponential backoff на 429 / rate-limit (**X4**) | 94 | +4 / -2 | 125 | +4 / -6 |
| FEAT_19 | Throttle `asyncio.sleep(1.0)` между вопросами | 96 | +5 / -1 | 127 | +5 / -5 |
| FEAT_20 | `_verify_sql_against_query` — LLM-as-judge на сгенерированный SQL (**X5**) | 90 | +5 / -7 | 125 | +6 / -8 |
| FEAT_21 | MinHash-скетчи колонок (**E6**) | 92 | +6 / -6 | 126 | +4 / -5 |
| FEAT_23 | SQL-to-text NL-описания таблиц / колонок (**E8**) | 95 | +9 / -6 | 122 | +4 / -9 |
| FEAT_25 | Per-question schema pruning — Selector-агент (**E9**) | 97 | +14 / -9 | 126 | +5 / -6 |
| FEAT_26 | `learnt_hints` — накопление подсказок между прогонами одной БД (**E10 = F3**) | 102 | +15 / -5 | 124 | +5 / -8 |
| FEAT_27 | True self-correction — передача текста ошибки в next prompt (**X6 = F2**) | 95 | +6 / -3 | 123 | +4 / -8 |
| FEAT_28 | Self-consistency — 3 семпла + majority voting (**X7**) | 93 | +5 / -4 | 124 | +4 / -7 |
| FEAT_29 | Декомпозиция по сложности (easy / non-nested / nested) с разными промптами (**X8**) | 95 | +12 / -9 | 121 | +3 / -9 |
| FEAT_30 | Sub-question decomposition — Decomposer-агент, CoT (**X9**) | 91 | +19 / -20 | 124 | +5 / -8 |
| FEAT_32 | Обнаружение неоднозначностей по таксономии A₁–A₆ — Stage 1 (**X10**) | 95 | +7 / -4 | 127 | +7 / -7 |
| FEAT_33 | Генерация multiple-choice вопросов для уточнения намерения — Stage 2a (**X11**) | 93 | +3 / -2 | 126 | +6 / -7 |
| FEAT_34 | Переписывание запроса q → q′ на основе пользовательского выбора — Stage 2b (**X12**) | 97 | +5 / -0 | 125 | +5 / -7 |
| FEAT_35 | TSV вместо JSON для результатов выборки (**E11**) | 85 | +4 / -11 | 128 | +13 / -12 |
| FEAT_36 | Принудительный LIMIT (10–100) + N+1-проверка для пагинации (**E12**) | 94 | +3 / -1 | 122 | +3 / -8 |
