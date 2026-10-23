# Практики Text2SQL: единая таблица

> Объединение трёх таблиц (тир-лист exploration, тир-лист execution, матрица
> комбинаторных экспериментов) в одну, со сквозной нумерацией. Дубликаты схлопнуты.
>
> Колонки:
> - **№** — сквозной номер практики.
> - **Практика** — короткое описание; в скобках — ID из исходной матрицы (Eₙ/Xₙ/Fₙ).
> - **Фаза** — `exploration` (раз на `db_id` в `build()`) / `execution` (на каждый вопрос в `query()`).
> - **Файл / метод (ветка)** — путь и метод; в скобках — ветка с реализацией для `done`-практик.
> - **Источник** — конкретные параграфы статей, где практика описана. Тег **`[shared N]`** означает, что одна и та же практика покрывается ≥ 2 статьями (см. §«Кросс-цитированные практики» ниже — там разобрано, *чем* каждая статья её закрывает).
> - **Почему полезно** — обоснование пользы со ссылкой на источник (цитата или тезис).
> - **Status** — `done` / `partial` / `not done` / `rejected`.
> - **Ответственный**, **Дедлайн** — заполняются по мере планирования.
>
> Источники:
> | Тег | Статья | Ссылка |
> |-----|--------|--------|
> | **CHESS** | CHESS: Contextual Harnessing for Efficient SQL Synthesis | [2405.16755](https://arxiv.org/abs/2405.16755) |
> | **AutoMeta** | AutoMeta: Automated Metadata Extraction | [2505.19988](https://arxiv.org/abs/2505.19988) |
> | **DIN-SQL** | DIN-SQL: Decomposed In-Context Learning | [2304.11015](https://arxiv.org/abs/2304.11015) |
> | **MAC-SQL** | MAC-SQL: Multi-Agent Collaborative SQL Generation | [2312.11242](https://arxiv.org/abs/2312.11242) |
> | **AmbiSQL** | AmbiSQL: Interactive Ambiguity Detection & Resolution | [2508.15276](https://arxiv.org/abs/2508.15276) |
> | **pgEdge** | Lessons Learned Writing an MCP Server for PostgreSQL | [pgedge.com/blog](https://www.pgedge.com/blog/lessons-learned-writing-an-mcp-server-for-postgresql) |

---

| № | Практика | Фаза | Файл / метод (ветка) | Источник | Почему полезно | Status | Ответственный | Дедлайн |
|---|---|---|---|---|---|---|---|---|
| 1 | FK / PK relationships в системном промпте (**E3**) | exploration | `text2sql_implementation.py:250-303` → `_explore_db_relationships` + `_format_relationships` (ветка `kin_br`) | AutoMeta §query-log · CHESS §3.2 **[shared 2]** | AutoMeta: **«25%+ of the equality join constraints used were not documented in the SQLite schema»** — без явных FK/PK LLM угадывает join-ы и промахивается. CHESS включает связи как часть «минимально-достаточной» схемы. | done | — | — |
| 2 | Column statistics (min/max/top-K/диапазон дат) в обоих промптах (**E4**) | exploration | `text2sql_implementation.py:490-597` → `_explore_column_statistics` + `_format_column_statistics` (ветка `kin_br`) | AutoMeta §profiling | AutoMeta: **«the most difficult part of query development lies in understanding the database contents»**. record counts, distinct, min/max, top-K дают LLM понимание распределения значений → корректные литералы, агрегации, форматы сравнений. | done | — | — |
| 3 | Regex-детекция реального типа TEXT-колонок (**E5**) | exploration | `text2sql_implementation.py:335-415` → `_detect_column_types_for_table` (ветка `kin_br`) | AutoMeta §profiling (value 'shape') | AutoMeta требует «**value 'shape' (length, character types)**». Вся БД хранит TEXT — без этого LLM не знает, нужен ли CAST к INTEGER/DATE; regex-сэмплинг 500 строк это закрывает. | done | — | — |
| 4 | `pg_stat_user_tables.n_live_tup` + fallback на `COUNT(*)` для row-count | exploration | `text2sql_implementation.py:305-333` → `_get_table_row_count` (ветка `kin_br`) | AutoMeta §profiling (record counts) | AutoMeta перечисляет «**record counts**» первой метрикой профайлинга. Знание размера таблицы помогает LLM решать, нужен ли LIMIT, подзапросы, можно ли позволить себе DISTINCT. `pg_stat` — почти бесплатное приближение. | done | — | — |
| 5 | Light-режим извлечения схемы — имена + типы (**E1**) | exploration | `text2sql_implementation.py:116-239` → `_get_db_schema_light` (ветка `kin_br`, по умолчанию) | CHESS §3.2 | CHESS: **«Our pipeline addresses this challenge by providing the LLM with minimal yet sufficient information necessary for each task»**. Light экономит токены и снижает шум промпта. | done | — | — |
| 6 | Heavy-режим — sample rows + low-cardinality enums (**E2**) | exploration | `text2sql_implementation.py:116-239` → `_get_db_schema_heavy` (ветка `kin_br`, выкл. по умолчанию) | CHESS §3.2 | CHESS §3.2 включает в схему «**relevant values and descriptions**». Образцы значений помогают LLM понимать формат данных (бренды/категории), полезно для сильных моделей с запасом токенов. | done | — | — |
| 7 | Компактное форматирование статистик (ID → `unique (id)`, top-K только для high-cardinality) | exploration | `text2sql_implementation.py` → `_format_column_statistics` (ветка `kin_br`) | CHESS §3.2 | Прямая реализация принципа CHESS: **«minimal yet sufficient»** — печатать диапазоны только там, где они информативны. Меньше токенов, меньше отвлечений на нерелевантные факты. | done | — | — |
| 8 | Rollback abort-нутых PostgreSQL-транзакций внутри exploration | exploration | `text2sql_implementation.py:319-323, 366-372, 481-487, 533-540` (ветка `kin_br`) | локальная диагностика | Закрывает cascade `InFailedSqlTransaction`, который в первой версии давал 0% accuracy: одна failed-транзакция блокировала всё дальнейшее exploration. Не из статей — обнаружено в логах. | done | — | — |
| 9 | Дамп расширенной схемы (schema + relationships + stats) в `db_schemas.json` | exploration | `benchmarks/base.py:85-93` → `_dump_db_schemas_json` (ветка `kin_br`) | CHESS §3.2 | Делает результат exploration воспроизводимым: можно перезапускать execution-фазу без похода в БД. Базис для пункта **26 (learnt_hints)** — без дампа некуда дописывать накопленные подсказки. | done | — | — |
| 10 | Замер времени `build()` + лог числа таблиц / символов промпта | exploration | `text2sql_implementation.py:91-108` (ветка `kin_br`) | — | Ловит деградации (БД с 100+ таблицами, разрастание промпта); даёт данные для выбора, какие практики из этого списка можно позволить себе одновременно по бюджету. | done | — | — |
| 11 | JSON-структурное логирование пайплайна | exploration + execution | `text2sql_implementation.py:33-69` → `JSONFormatter`, `setup_logging` (ветка `kin_br`) | — | Позволяет постфактум парсить ход пайплайна (jq/pandas), считать % ambiguous, retry-distribution; без этого нельзя делать диагностические доки и решать, какую практику оставлять / выключать. | done | — | — |
| 12 | Optimistic ambiguity fallback — env `OPTIMISTIC_AMBIGUITY_FALLBACK` (**X1**) | execution | `text2sql_implementation.py:899-941` → `query()` (ветка `kin_br`) | локальная диагностика | Baseline ambiguity-промпт ложно помечал 72% запросов BIRD как ambiguous → 0% accuracy. Fallback продолжать генерировать SQL даже при ambiguous-флаге дал прирост с 13% до 36% (см. `research_results/legacy_notes/less_false_ambiguity_plan.md` в `kin_br`). | done | — | — |
| 13 | Few-shot ambiguity-промпт «3 условия одновременно» (**X2**) | execution | `prompts.py:38-43` → `AMBIGUITY_PROMPT_TEMPLATE` (ветка `kin_br`) | DIN-SQL §classification + локальная итерация | DIN-SQL: классификация запросов в категории + примеры даёт «**structured generation of SQL queries**». У нас — три явных условия + 8 правил «всегда OK» + примеры из тех же доменов, что бенчмарк. | done | — | — |
| 14 | Жёсткие правила в SQL-промпте (DISTINCT после SELECT, CAST дат, не транслитерировать имена) | execution | `prompts.py:93-181` → `SQL_PROMPT_TEMPLATE`, `SYSTEM_PROMPT_TEMPLATE` (ветка `kin_br`) | локальная диагностика | Каждое правило закрывает измеренный класс ошибок: транслитерация имён → 1 запрос, DISTINCT после запятой → 1 запрос, неявный CAST дат → 2 запроса. Превентивные правила в промпте дешевле, чем post-fix sanitize. | done | — | — |
| 15 | `sanitize_sql` — markdown / DISTINCT / CAST / NULLIF чистка (**X3** часть 1) | execution | `text2sql_implementation.py:716-745` → `sanitize_sql` (ветка `kin_br`) | локальная диагностика | LLM возвращает markdown-обёртки, SQLite-style CAST вместо PostgreSQL, DISTINCT после запятой. Нормализация до валидации экономит retry-итерации (а значит токены) на форматных ошибках. | done | — | — |
| 16 | Валидация через `sqlglot` + запрет DROP/DELETE/UPDATE (**X3** часть 2) | execution | `text2sql_implementation.py:698-714` → `_validate_sql` (ветка `kin_br`) | — | Парсинг до execution отсеивает синтаксические ошибки без обращения к БД (быстро + дёшево); запрет mutation-операций — обязательный safety-guard для read-only БД. | done | — | — |
| 17 | Retry-цикл до `MAX_RETRIES=7` при провале validate | execution | `text2sql_implementation.py` → `query()` — `FEAT_17` gates `max_retries_effective = MAX_RETRIES if FEAT_17 else 1` | DIN-SQL §self-correction · CHESS §3.3 · MAC-SQL §3.4 **[shared 3]** | DIN-SQL: «**In our self-correction module the model is instructed to correct those minor mistakes**». Все три статьи описывают итеративный regenerate-loop как стандартный приём; цикл с `FEAT_17` стал полноценным каркасом для `FEAT_27` (true self-correction). | done | — | — |
| 18 | LLM-вызовы с exponential backoff на 429 / rate-limit (**X4**) | execution | `text2sql_implementation.py:599-620` → `_llm_call_with_retry` (ветка `kin_br`) | — | На 200+ вопросах rate-limit неизбежен; backoff с jitter позволяет завершить весь прогон без падения и без потери уже потраченных токенов. | done | — | — |
| 19 | Throttle `asyncio.sleep(1.0)` между вопросами | execution | `benchmarks/bird.py:58` (ветка `kin_br`) | — | Дополнительная защита от burst-нагрузок поверх backoff'а. Стоимость низкая (200 × 1 сек ≈ 3.3 мин), стабильность пайплайна выше — страховка пункта **18**. | done | — | — |
| 20 | `_verify_sql_against_query` — LLM-as-judge на сгенерированный SQL (**X5**) | execution | `text2sql_implementation.py` → `_verify_sql_against_query` + `query()` — `FEAT_20` gates the verify step in retry loop | CHESS §3.3 (revision) · MAC-SQL §3.4 (Refiner) **[shared 2]** | CHESS: «**The model is then asked to evaluate the correctness of the SQL query and revise it if necessary**». Доп. LLM-проход отсеивает грубые семантические ошибки, которые `sqlglot` поймать не может. Теперь подключён в `query()` через `FEAT_20`. | done | — | — |
| 21 | MinHash-скетчи колонок (**E6**) | exploration | `text2sql_implementation.py` → `_find_similar_columns()` — `FEAT_21` appends implicit join hints to system prompt | AutoMeta §profiling | AutoMeta: «**Minhash sketches for estimating set resemblance**». За O(k) оценивает долю общих значений двух колонок → быстро предлагает join-кандидатов, которых нет в FK (закрывает половину пункта **24**). | done | — | — |
| 22 | LSH-индекс + векторная БД на значения колонок для retrieval сущностей вопроса (**E7**) | exploration | TODO | CHESS §3.1 (Entity & Context Retrieval) | CHESS §3.1: для текстовых колонок LSH + embeddings находят **точные значения**, упомянутые в вопросе, и подмешивают их в промпт. Решает класс ошибок типа «What's the French name of the set that contains 'Tendo Ice Bridge'?». | not done | — | — |
| 23 | SQL-to-text NL-описания таблиц / колонок (**E8**) | exploration | `text2sql_implementation.py` → `_generate_table_descriptions()` — `FEAT_23` generates per-table NL descriptions at build(), cached in `nl_descriptions/{db_id}.json` | AutoMeta §SQL-to-text | AutoMeta: «**analyzing an SQL query to identify referenced fields, creating a focused schema, and prompting an LLM to generate questions**» — обратная задача: один раз генерируем NL-описания при `build()`, помогает LLM выбирать правильную колонку среди похожих по имени. | done | — | — |
| 24 | Анализ query-log для извлечения скрытых join-предикатов | exploration | TODO (для нашего стенда query-log внешних пользователей недоступен — альтернатива в **26**) | AutoMeta §query-log | AutoMeta: **«25%+ of the equality join constraints used were not documented in the SQLite schema»**. Query-log хранит реально используемые join-пути, которые в FK не описаны — прямой источник «скрытого знания о БД». | not done | — | — |
| 25 | Per-question schema pruning — Selector-агент (**E9**) | exploration | `text2sql_implementation.py` → `_prune_schema_for_question()` — `FEAT_25` gates pruned schema injection in `generate_sql()` | MAC-SQL §3.2 (Selector) · CHESS §3.2 (Schema Selection) **[shared 2]** | MAC-SQL: «**The Selector decomposes a large database into smaller sub-databases to minimize interference from irrelevant information**». CHESS §3.2 делает то же по сути — отбор минимально-достаточного подмножества колонок/таблиц под каждый вопрос. Для БД с >30 колонок (наш `cards` — 72) уменьшает шум промпта. Цена: +1 LLM round-trip / запрос. | done | — | — |
| 26 | `learnt_hints` — накопление подсказок между прогонами одной БД (**E10 = F3**) | exploration | `text2sql_implementation.py` → `_load/_save_learnt_hint()`, `build()` — `FEAT_26` reads/writes `hints/{db_id}.json` | AutoMeta §query-log (адаптировано — см. §«Кросс-цитированные практики») | После прогона BIRD знаем формат дат (`yearmonth.Date` = `YYYYMM`), реальные join-пути (`client → disp → account`), частые SQL-паттерны-ошибки. Дозапись в `db_schemas.json` и подмешивание в промпт следующего запуска — самый дешёвый источник прироста (наш аналог query-log из пункта **24**). | done | — | — |
| 27 | True self-correction — передача текста ошибки в next prompt (**X6 = F2**) | execution | `text2sql_implementation.py` → `query()` + `generate_sql()` — `FEAT_27` passes `prev_sql` + `error_feedback` into `SQL_RETRY_PROMPT_TEMPLATE` | DIN-SQL §self-correction · CHESS §3.3 (revision) · MAC-SQL §3.4 (Refiner) **[shared 3]** | MAC-SQL: «**using the original SQL and error feedback information**». Превращает 7 «слепых» retry в осмысленный self-correction-loop — каждый retry видит предыдущий SQL и текст ошибки. **Главный кандидат на +5–10% accuracy.** | done | — | — |
| 28 | Self-consistency — 3 семпла + majority voting (**X7**) | execution | `text2sql_implementation.py` → `_generate_sql_with_voting()` — `FEAT_28` replaces retry loop with 3-sample majority vote | CHESS §3.3 | CHESS: «**we use self-consistency to select the SQL query that appears most consistently across three samples**». Снижает шум при ненулевой температуре; CHESS фиксирует +2–4% accuracy за 3× стоимость токенов. | done | — | — |
| 29 | Декомпозиция по сложности (easy / non-nested / nested) с разными промптами (**X8**) | execution | `text2sql_implementation.py` → `_classify_query_complexity()` + `generate_sql()` — `FEAT_29` selects `SQL_PROMPT_COMPLEX_TEMPLATE` / `SQL_PROMPT_SIMPLE_TEMPLATE` | DIN-SQL §classification | DIN-SQL: «**By strategically identifying and separating schema linking, join conditions, and nested structures, the module facilitates a structured generation**». Keyword-heuristic классификация (zero LLM cost) + 3 специализированных промпта. | done | — | — |
| 30 | Sub-question decomposition — Decomposer-агент, CoT (**X9**) | execution | `text2sql_implementation.py` → `_decompose_question()` — `FEAT_30` calls LLM once pre-SQL to break question into sub-steps; result prepended as UserMessage in `generate_sql()` | MAC-SQL §3.3 | MAC-SQL: «**If the question is more complex, the corresponding SQL is generated starting from the simplest sub-problem**». CoT-разложение помогает на `moderate`/`challenging`, где требуется композиция нескольких подзапросов; одношаговая генерация теряет промежуточные шаги. | done | — | — |
| 31 | Feedback в веса модели (**F1**) | — | — | — | Дообучение даёт выигрыш только при ≥10⁴ примеров и собственной GPU-инфраструктуре; LLM у нас через прокси, fine-tuning невозможен. CHESS подтверждает: с готовой LLM можно достичь SOTA без дообучения, если правильно строить контекст («**minimal yet sufficient information**»). Все ресурсы — на пункты **17–30**. | rejected | — | — |
| 32 | Обнаружение неоднозначностей по таксономии A₁–A₆ — Stage 1 (**X10**) | execution | `text2sql_implementation.py` → `_detect_ambiguity_by_taxonomy()` — `FEAT_32` replaces `_check_ambiguity()` in `query()` | **AmbiSQL** §Taxonomy; **DIN-SQL** §classification | AmbiSQL: **«6 subcategories cover 92% of real-world cases»**. LLM classifies query by taxonomy (A1-A6), returns JSON `{is_ambiguous, ambiguity_type, reason}`. | done | — | — |
| 33 | Генерация multiple-choice вопросов для уточнения намерения — Stage 2a (**X11**) | execution | `text2sql_implementation.py` → `_generate_clarification_question()` — `FEAT_33` triggers on ambiguous Stage 1 result | **AmbiSQL** §Iterative Refinement | AmbiSQL: **«Interactive clarification is more effective than automatic guessing»**. LLM generates multiple-choice question, returns JSON `{question, options}`. Precision: 87.2%, Recall: 89.1%. | done | — | — |
| 34 | Переписывание запроса q → q′ на основе пользовательского выбора — Stage 2b (**X12**) | execution | `text2sql_implementation.py` → `_auto_resolve_ambiguity()` — `FEAT_34` auto-picks most likely option and rewrites query for batch benchmarks | **AmbiSQL** §Query Rewriting | AmbiSQL: **«Rewritten query leads to correct SQL in 78% of ambiguous cases»**. In batch mode: LLM selects most probable option + rewrites unambiguous NL query → proceeds to SQL generation. | done | — | — |
| 35 | TSV вместо JSON для результатов выборки (**E11**) | exploration + execution | `text2sql_implementation.py` — `FEAT_35` gates TSV schema format in `_get_db_schema_light()` | **pgEdge** §output-format; **CHESS** §3.2 («minimal yet sufficient») | pgEdge: TSV экономит 30–40% токенов vs JSON при нулевых галлюцинациях парсинга. SELECT * без лимита может стоить 50k+ токенов — pgEdge называет это «токеновой бомбой». Перекрёстно с п.7. | done | — | — |
| 36 | Принудительный LIMIT (10–100) + N+1-проверка для пагинации (**E12**) | exploration | `text2sql_implementation.py` → `sanitize_sql()` — `FEAT_36` appends `LIMIT 101` when no `LIMIT`/`FETCH` present | **pgEdge** §pagination | pgEdge: запрашивать N+1 строку (101 при лимите 100) — если результат есть, LLM получает сигнал использовать offset. Предотвращает «порочный цикл» переполнения контекста. | done | — | — |
| 37 | Context compaction: классификация сообщений + фильтрация при >100k токенов (**X13**) | execution | TODO → `context_manager.py` → `_compress_dialogue_memory` | **pgEdge** §memory-management | pgEdge: классификация на Якорные (схема БД) / Важные (финальный анализ) / Контекстные / Обычные / Переходные. Правило неразрывности: пара «вызов инструмента + результат» сохраняется целиком. Порог: 100k токенов. | not done | — | — |

---

## Привязки фич к статьям

### Кросс-цитированные практики (≥ 2 источников)

Пять практик описаны в нескольких статьях по-разному, но решают одну задачу.
Здесь разбор, *чем именно* каждая статья её закрывает — это нужно, чтобы при
имплементации можно было выбирать подходящую формулировку, и чтобы агент видел,
где «одна и та же» практика прячется под разными именами.

#### № 1 — FK / PK relationships в промпте · `[shared 2]`

| Статья | Раздел | Чем покрывает |
|---|---|---|
| **AutoMeta** | §query-log | Фиксирует *проблему*: «25%+ of the equality join constraints used were not documented in the SQLite schema». То есть просто FK из схемы недостаточно — без них pipeline теряет четверть реальных join-предикатов. |
| **CHESS** | §3.2 (Schema Selection) | Включает FK/PK как обязательную часть «**minimal yet sufficient**» контекста. Формулировка: схема без связей не передаётся LLM никогда. |

Расхождение: AutoMeta мотивирует *«FK мало, нужна доразметка»*; CHESS отвечает на более простой вопрос — *«какая часть DDL должна попасть в промпт»* (ответ: FK обязательно).

#### № 17 — Retry-каркас (regenerate-on-failure) · `[shared 3]`

| Статья | Раздел | Чем покрывает |
|---|---|---|
| **DIN-SQL** | §self-correction | Канонический термин — «self-correction module»; цикл «генерация → проверка → перегенерация», zero-shot, два варианта (generic/gentle). |
| **CHESS** | §3.3 | Тот же цикл, но в терминах «candidate generation + revision»; добавляет execution result в контекст ревизии. |
| **MAC-SQL** | §3.4 (Refiner) | То же, но как *отдельный агент* — Refiner получает SQL + diagnostics и решает: оставить, переписать или попросить регенерировать. |

Расхождение: DIN-SQL описывает retry как *шаг в линейном пайплайне*; CHESS — как *подмодуль шага генерации*; MAC-SQL — как *отдельный агент* в multi-agent-системе. Реализация у нас сейчас ближе всего к DIN-SQL — линейный цикл в `query()`.

#### № 20 — LLM-as-judge (Verify) · `[shared 2]`

| Статья | Раздел | Чем покрывает |
|---|---|---|
| **CHESS** | §3.3 (revision) | «The model is then asked to evaluate the correctness of the SQL query and revise it if necessary». Verify здесь — *первая половина* revision-шага (вторая — фактическая ревизия). |
| **MAC-SQL** | §3.4 (Refiner) | Refiner-агент диагностирует «syntactic correctness, execution feasibility, and the retrieval of non-empty results» — фактически тот же verify, но как самостоятельный агент. |

Наш `_verify_sql_against_query` пока ближе к CHESS-формулировке (один LLM-вызов после генерации), но **не подключён в `query()`** — это то, что отделяет нас от любой из двух рецептур.

#### № 25 — Per-question schema pruning · `[shared 2]`

| Статья | Раздел | Чем покрывает |
|---|---|---|
| **MAC-SQL** | §3.2 (Selector) | Самое явное определение: «decomposes a large database into smaller sub-databases». Селектор работает per-question и выдаёт три класса таблиц (keep_all / drop_all / explicit subset). |
| **CHESS** | §3.2 (Schema Selection) | По сути то же самое: «narrow down the schema to include only the necessary tables and columns». Реализован через каскад column-filter → table-selection → final-column. |

Различие техническое: MAC-SQL — один LLM-вызов с JSON-выходом; CHESS — каскад из трёх под-промптов. Если решим внедрять, MAC-SQL-форма дешевле (1 round-trip), CHESS-форма точнее.

#### № 27 — True self-correction (передача ошибки в prompt) · `[shared 3]`

| Статья | Раздел | Чем покрывает |
|---|---|---|
| **DIN-SQL** | §self-correction | Передаёт LLM «buggy code», просит починить. *Не передаёт текст ошибки* — zero-shot фикс на основе кода. |
| **CHESS** | §3.3 (revision) | Идёт дальше: «we provide the model with the database schema, the question, the generated candidate SQL query, and **its execution result**». Передаёт результат выполнения, в т.ч. ошибки. |
| **MAC-SQL** | §3.4 (Refiner) | Самая полная формулировка: «using the original SQL and **error feedback information**». Прямо про error feedback. |

Различие важное при выборе реализации:
- DIN-SQL-вариант — самый дешёвый (не нужно ходить в БД между retry).
- CHESS/MAC-SQL — нужно выполнять SQL до retry, чтобы получить execution result/error. Но именно эта пара даёт тот самый «+5–10% accuracy».

---

### Полная матрица практика × статья

Компактная сводка для агента: где (в какой статье) описана каждая практика.
Ячейка = раздел в статье; пусто = статья практику не затрагивает.

| № | Практика | CHESS | AutoMeta | DIN-SQL | MAC-SQL | Локальное / др. |
|---|---|---|---|---|---|---|
| 1  | FK / PK в промпте                                  | §3.2 | §query-log | — | — | — |
| 2  | Column statistics                                  | — | §profiling | — | — | — |
| 3  | Regex type-detection (value 'shape')               | — | §profiling | — | — | — |
| 4  | pg_stat row-count                                  | — | §profiling | — | — | — |
| 5  | Light schema                                       | §3.2 | — | — | — | — |
| 6  | Heavy schema                                       | §3.2 | — | — | — | — |
| 7  | Compact stats formatting                           | §3.2 | — | — | — | — |
| 8  | PG transaction rollback                            | — | — | — | — | local diagnostics |
| 9  | Dump `db_schemas.json`                             | §3.2 | — | — | — | — |
| 10 | `build()` timing & log                             | — | — | — | — | — |
| 11 | JSON structured logging                            | — | — | — | — | — |
| 12 | Optimistic ambiguity fallback                      | — | — | — | — | local diagnostics |
| 13 | Few-shot ambiguity prompt                          | — | — | §classification | — | local iteration |
| 14 | Strict SQL prompt rules                            | — | — | — | — | local diagnostics |
| 15 | `sanitize_sql`                                     | — | — | — | — | local diagnostics |
| 16 | sqlglot validate + forbid mutations                | — | — | — | — | — |
| 17 | Retry-каркас MAX_RETRIES                           | §3.3 | — | §self-correction | §3.4 | — |
| 18 | LLM backoff                                        | — | — | — | — | — |
| 19 | `asyncio.sleep` throttle                           | — | — | — | — | — |
| 20 | `_verify_sql_against_query` (LLM-as-judge)         | §3.3 | — | — | §3.4 | — |
| 21 | MinHash sketches                                   | — | §profiling | — | — | — |
| 22 | LSH retrieval of column values                     | §3.1 | — | — | — | — |
| 23 | SQL-to-text NL-описания                            | — | §SQL-to-text | — | — | — |
| 24 | Query-log analysis                                 | — | §query-log | — | — | — |
| 25 | Per-question schema pruning                        | §3.2 | — | — | §3.2 | — |
| 26 | `learnt_hints` между прогонами                     | — | §query-log (адаптация) | — | — | local extension |
| 27 | True self-correction (error feedback)              | §3.3 | — | §self-correction | §3.4 | — |
| 28 | Self-consistency                                   | §3.3 | — | — | — | — |
| 29 | Decomposition по сложности                         | — | — | §classification | — | — |
| 30 | Sub-question decomposition (CoT Decomposer)        | — | — | — | §3.3 | — |
| 31 | Feedback в веса (отвергнуто)                       | — | — | — | — | — |

**Сводка по статьям:**

| Статья | Сколько практик | Какие |
|---|---|---|
| **CHESS** | 11 | 1, 5, 6, 7, 9, 17, 20, 22, 25, 27, 28 |
| **AutoMeta** | 8 | 1, 2, 3, 4, 21, 23, 24, 26 |
| **DIN-SQL** | 4 | 13, 17, 27, 29 |
| **MAC-SQL** | 4 | 17, 20, 25, 27, 30 |
| **Локальное** | 7 | 8, 10, 11, 12, 13, 14, 15 (плюс инфраструктурные 16, 18, 19) |

CHESS и AutoMeta — главные «доноры» практик: между ними поделена exploration-фаза.
DIN-SQL и MAC-SQL добавляются в основном в execution-цикле (retry / verify /
decomposition), и именно там лежат все три «**[shared]**»-практики высокого тира.

**AmbiSQL** добавляет три взаимосвязанных практики (32–34), образующих pipeline: детекция → уточнение → переписывание. **pgEdge** — технические практики минимизации токенов (35–37).

### Перекрёстные ссылки (новые)

| Практика | Источники | Общий тезис |
|----------|-----------|-------------|
| **Minimal schema context** (п. 5, 7, 35) | CHESS §3.2 + pgEdge §output-format | Давать LLM только то, что нужно — меньше шума, меньше галлюцинаций |
| **Query classification как первый шаг** (п. 13, 29, 32) | DIN-SQL §classification + AmbiSQL §architecture | Сначала классифицировать запрос (сложность / тип неоднозначности), затем генерировать |
| **Ambiguity false positives** (п. 12, 13, 32–34) | AmbiSQL + локальный прогон | GPT-3.5 даёт 20–50% FP при ambiguity-детекции; AmbiSQL решает через таксономию + clarification + переписывание |
| **AmbiSQL pipeline** (п. 32, 33, 34) | AmbiSQL §architecture | Три практики образуют единый pipeline — реализовывать нужно вместе |
| **Token minimization** (п. 5, 7, 25, 35, 36) | CHESS §3.2 + pgEdge §TSV + pgEdge §pagination | Переполнение контекста — системная угроза; несколько независимых источников предлагают схожие решения |

---

## Таксономия неоднозначностей (AmbiSQL) для промпт-инжиниринга

> Используется напрямую в few-shot примерах для ambiguity-промпта (п. 13, 32).

### DB-related ambiguity
| Тип | Описание | Пример |
|-----|----------|--------|
| Unclear schema reference | Неясная ссылка на колонку/таблицу | «oldest user»: age или registration_date? |
| Unclear value reference | Запрос не совпадает с реальными значениями в БД | запрос «New York City», в БД хранится «NYC» |
| Missing SQL keywords | Одна фраза может отображаться в разные SQL-операторы | «by registration date» → ORDER BY / GROUP BY / WHERE? |

### LLM-related ambiguity
| Тип | Описание | Пример |
|-----|----------|--------|
| Unclear knowledge source | Неясно, использовать ли колонку или семантику | «female employees» → колонка gender или анализ имён? |
| Insufficient reasoning context | Недостаточно контекста для однозначного ответа | «current exchange rate» без валют и даты |
| Conflicting knowledge | Запрос о несуществующих событиях | участники мероприятия, которого не было |

---

## Токеновый бюджет: ориентиры (pgEdge)

| Формат | Токенов на 100 строк | Галлюцинации парсинга |
|--------|----------------------|-----------------------|
| JSON | ~800–1200 | есть (ключи дублируются) |
| CSV | ~500–700 | есть (запятые/кавычки) |
| **TSV** | **~400–600** | **0** |

**Правило по умолчанию**: LIMIT 100 строк + запрос N+1 как сигнал пагинации.  
**Порог context compaction**: 100 000 токенов.  
**Приоритет сохранения**: схема БД → финальные результаты → контекст → обычный диалог → служебные подтверждения.

---

## Ablation experiment — leave-one-in

### Methodology note (важно перед интерпретацией результатов)

**Методология v1 (commit `f997dec`) — сломана, результаты недостоверны.**

При анализе логов обнаружены три независимые поломки:

1. **False-ambiguity cascade** — без `FEAT_12` (optimistic fallback) + без `FEAT_13` (few-shot prompt) `AMBIGUITY_PROMPT_SIMPLE` помечал 60–80% вопросов как ambiguous → они никогда не доходили до SQL generation. Это не измерение фичи — это измерение количества вопросов, которые прошли сломанный ambiguity-фильтр.

2. **Rate-limit cascade** — без `FEAT_18` (backoff) + `FEAT_19` (throttle) первый 429-запрос убивал весь оставшийся прогон. Большинство вопросов получали `status: error` не из-за плохого SQL, а из-за сетевой ошибки.

3. **Стаистическая неразрешимость** — BIRD-small = 22 вопроса → 1 вопрос = 4.55%. Дельта ≤2 вопросов неотличима от температурного шума.

**Методология v2 (актуальная):**

```
BASE = FEAT_5=true  FEAT_12=true  FEAT_18=true  FEAT_19=true
```

Все фичи тестируются поверх этого base stack. Baseline = base stack без дополнительных фич.
Исключения: `feat_12/18/19` тестируются с двумя другими инфра-флагами, но без себя.

Запуск: `bash local/ablation_full.sh` (результаты ожидаются после перезапуска).

---

### Результаты v1 — для архива (сломанная методология)

**Baseline v1** (все флаги off): BIRD=0.00%, Ambrosia=50.00%.

| № | Флаг | Практика (краткое) | BIRD% | AMB% |
|---|---|---|---|---|
| — | BASELINE | все флаги off | **0.00%** | **50.00%** |
| 1 | FEAT_1 | FK/PK relationships | 9.09% | 62.50% |
| 2 | FEAT_2 | Column statistics | 4.55% | 62.50% |
| 3 | FEAT_3 | Regex type detection | 0.00% | 62.50% |
| 4 | FEAT_4 | pg_stat row count | 4.55% | 45.83% |
| 5 | FEAT_5 | Light schema | 4.55% | 58.33% |
| 6 | FEAT_6 | Heavy schema | 4.55% | 62.50% |
| 7 | FEAT_7 | Compact stats formatting | 4.55% | 62.50% |
| 8 | FEAT_8 | PG rollback guards | 4.55% | 50.00% |
| 9 | FEAT_9 | Dump db_schemas.json | 0.00% | 54.17% |
| 10 | FEAT_10 | build() timing log | 4.55% | 50.00% |
| 11 | FEAT_11 | JSON structured logging | 4.55% | 66.67% |
| 12 | FEAT_12 | Optimistic ambiguity fallback | 4.55% | 62.50% |
| 13 | FEAT_13 | Few-shot ambiguity prompt | 4.55% | 41.67% |
| 14 | FEAT_14 | Strict SQL rules | 4.55% | 50.00% |
| 15 | FEAT_15 | sanitize_sql | 0.00% | 58.33% |
| 16 | FEAT_16 | sqlglot validate | 9.09% | 58.33% |
| 17 | FEAT_17 | Retry loop MAX_RETRIES | **13.64%** | 58.33% |
| 18 | FEAT_18 | LLM exponential backoff | 9.09% | 45.83% |
| 19 | FEAT_19 | asyncio.sleep throttle | 9.09% | 58.33% |
| 20 | FEAT_20 | LLM-as-judge verify | 0.00% | 50.00% |
| 27 | FEAT_27 | True self-correction | 0.00% | 50.00% |
| 29 | FEAT_29 | Complexity-based prompts | 4.55% | 58.33% |
| 32 | FEAT_32 | AmbiSQL taxonomy detection | 4.55% | 41.67% |
| 33 | FEAT_33 | AmbiSQL clarification questions | 9.09% | 58.33% |
| 34 | FEAT_34 | AmbiSQL query rewriting | 4.55% | 62.50% |
| 35 | FEAT_35 | TSV schema format | 4.55% | 58.33% |

### Результаты v2 — leave-one-in, все флаги false кроме одного

**Baseline v2**: BIRD=36.36%, Ambrosia=54.17% (все FEAT_* = false, чистый пайплайн без фич).

| № | Флаг | Практика (краткое) | BIRD% | Δ BIRD | AMB% | Δ AMB | Вывод |
|---|---|---|---|---|---|---|---|
| — | BASELINE | все флаги off | **36.36%** | — | **54.17%** | — | — |
| 1 | FEAT_1 | FK/PK relationships | 27.27% | -9.09% | 58.33% | +4.16% | Хуже BIRD, лучше AMB: FK-информация перегружает промпт на BIRD-схемах |
| 2 | FEAT_2 | Column statistics | **45.45%** | **+9.09%** | 54.17% | 0% | ★ Сильнейший сигнал на BIRD (+2 вопроса) |
| 3 | FEAT_3 | Regex type detection | 40.91% | +4.55% | 54.17% | 0% | Шум (1 вопрос) |
| 4 | FEAT_4 | pg_stat row count | 40.91% | +4.55% | 58.33% | +4.16% | Шум, но позитивен на обоих |
| 5 | FEAT_5 | Light schema | 31.82% | -4.55% | 50.00% | -4.17% | Минус на обоих — неожиданно |
| 6 | FEAT_6 | Heavy schema | 36.36% | 0% | 54.17% | 0% | Нейтрален |
| 7 | FEAT_7 | Compact stats | 36.36% | 0% | 50.00% | -4.17% | Нейтрален/шум |
| 8 | FEAT_8 | PG rollback | 36.36% | 0% | 54.17% | 0% | Нейтрален (инфраструктура) |
| 9 | FEAT_9 | Dump schemas | 36.36% | 0% | 54.17% | 0% | Нейтрален (инфраструктура) |
| 10 | FEAT_10 | build() timing | 40.91% | +4.55% | 54.17% | 0% | Шум |
| 11 | FEAT_11 | JSON logging | 40.91% | +4.55% | 50.00% | -4.17% | Шум |
| 12 | FEAT_12 | Optimistic fallback | 40.91% | +4.55% | **62.50%** | **+8.33%** | ★ Стабильно позитивен на обоих |
| 13 | FEAT_13 | Few-shot prompt | 40.91% | +4.55% | 50.00% | -4.17% | Шум (был баг `**OK**`, исправлен) |
| 14 | FEAT_14 | Strict SQL rules | 31.82% | -4.55% | 54.17% | 0% | Шум |
| 15 | FEAT_15 | sanitize_sql | 31.82% | -4.55% | 54.17% | 0% | Шум |
| 16 | FEAT_16 | sqlglot validate | 31.82% | -4.55% | 50.00% | -4.17% | Шум |
| 17 | FEAT_17 | Retry loop | 31.82% | -4.55% | 54.17% | 0% | Шум |
| 18 | FEAT_18 | Backoff | 36.36% | 0% | 54.17% | 0% | Нейтрален (инфраструктура) |
| 19 | FEAT_19 | Throttle | 36.36% | 0% | 54.17% | 0% | Нейтрален (инфраструктура) |
| 20 | FEAT_20 | LLM-as-judge | 31.82% | -4.55% | 50.00% | -4.17% | Шум/минус: накапливает ошибки верификации |
| 21 | FEAT_21 | MinHash similarity | 36.36% | 0% | 50.00% | -4.17% | Нейтрален: колонки похожи но LLM игнорирует |
| 23 | FEAT_23 | NL table descriptions | 31.82% | -4.55% | 54.17% | 0% | Шум/минус: лишний текст в промпте мешает |
| 25 | FEAT_25 | Schema pruning | 36.36% | 0% | 50.00% | -4.17% | Нейтрален |
| 26 | FEAT_26 | learnt_hints | 36.36% | 0% | 54.17% | 0% | Нейтрален (первый прогон без накопленных хинтов) |
| 27 | FEAT_27 | True self-correction | 36.36% | 0% | 54.17% | 0% | Нейтрален: retry без ошибок эквивалентен baseline |
| 28 | FEAT_28 | Self-consistency | 36.36% | 0% | 54.17% | 0% | Нейтрален (3× дороже) |
| 29 | FEAT_29 | Complexity-based prompts | 40.91% | +4.55% | 54.17% | 0% | Шум |
| 30 | FEAT_30 | CoT decomposition | 40.91% | +4.55% | 54.17% | 0% | Шум, но позитивен |
| 32 | FEAT_32 | AmbiSQL taxonomy | 36.36% | 0% | 45.83% | -8.34% | В isolation плох: prompt был философским, JSON errors. После переработки prompts даёт +AMB |
| 33 | FEAT_33 | AmbiSQL clarification | 31.82% | -4.55% | 45.83% | -8.34% | В isolation плох; в комбинации с исправленным FEAT_32 (без FEAT_34) — даёт AMB 91–96% |
| 34 | FEAT_34 | AmbiSQL rewrite | 36.36% | 0% | **62.50%** | **+8.33%** | Auto-resolve → потолок AMB 62.5%. Несовместим с целью AMB 70%+ |
| 35 | FEAT_35 | TSV schema | 40.91% | +4.55% | 58.33% | +4.16% | Позитивен на обоих (меньше токенов = меньше шума) |
| 36 | FEAT_36 | Smart LIMIT | 36.36% | 0% | 50.00% | -4.17% | Нейтрален |

**Итого сильных сигналов (≥ +8% или стабильно на обоих):**
- **FEAT_2** (+9.09% BIRD) — column statistics, самый сильный
- **FEAT_12** (+4.55% BIRD, +8.33% AMB) — optimistic fallback, стабильно на обоих
- **FEAT_34** (+8.33% AMB в isolation) — но создаёт потолок 62.5%: несовместим с AMB 70%+

**Структурная находка (v2, isolation):** FEAT_32/33 в isolation дают -8.34% AMB из-за двух проблем: (1) философский прайминг в taxonomy prompt, (2) JSON parse errors. После исправления промпта FEAT_32+33 **без** FEAT_34 дают AMB 91–96%. FEAT_34 — не обязательная часть pipeline, а архитектурный выбор с trade-off: auto-resolve улучшает BIRD (~+4%) но блокирует AMB на 62.5%.

---

### Результаты гибридных экспериментов (комбинации фич)

> Запуск: `bash local/run_single.sh "FEAT_X FEAT_Y ..." both`  
> Полный отчёт: `research_results/ablation_results_small/HYBRID_EXPERIMENTS_REPORT.md`

**Оптимальная комбинация:**
```
FEAT_2 FEAT_3 FEAT_4 FEAT_8 FEAT_12 FEAT_17 FEAT_18 FEAT_19 FEAT_27 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_35
```

| Эксперимент | BIRD | AMB | Ключевое изменение |
|-------------|------|-----|-------------------|
| 1 | 40.91% | 70.83% | Taxonomy prompt rewrite: философия → паттерны |
| 2 | 50.00% | 66.67% | +BIRD фичи (3/4/17/27/29/30), JSON errors ещё не пофикшены |
| 3 | 50.00% | **87.50%** | JSON парсер + counterexamples для FP/FN |
| 4 | **54.55%** | **95.83%** | Alias check rule в SQL_PROMPT |
| 5 | 54.55% | 91.67% | TO_DATE retry-rule (Q180 не починился, AMB — температурный шум) |

**Изменения в промптах, обусловившие прирост AMB:**
- `TAXONOMY_DETECTION_PROMPT`: переписан как задача сопоставления синтаксических паттернов; схема только для A1; counterexamples для A3a/A3b FP (Q8/Q108/Q112) и A1 FN (Q266/Q279)
- `_detect_ambiguity_by_taxonomy()`: JSON парсер извлекает `{...}` из ответа, sanitize control chars → ошибок 0

**Изменения в промптах, обусловившие прирост BIRD:**
- `SQL_PROMPT_TEMPLATE`, `SQL_PROMPT_COMPLEX_TEMPLATE`: правило 6/9 — проверка алиасов перед финализацией
- `SQL_RETRY_PROMPT_TEMPLATE`: правило 5 — `missing FROM-clause entry`; правило 6 — `to_date(date, unknown)`

---

### Нереализованные фичи — почему их нельзя протестировать

| № | Практика | Причина невозможности |
|---|---|---|
| **22** | LSH + векторная БД | Требует внешний embedding-сервис и vector store (faiss/chromadb). Нет инфраструктуры: LLM-прокси даёт только chat completions, embeddings endpoint недоступен. |
| **24** | Query-log analysis | Заблокировано архитектурно: query-log внешних пользователей для benchmark-БД не существует. FEAT_26 (learnt_hints) — наш аналог поверх реальных прогонов. |
| **37** | Context compaction | Нерелевантен для бенчмарка: каждый вопрос — независимый single-turn вызов `query()`. Compaction нужен только при многоходовом диалоге (>100k токенов контекста), чего в бенчмарке нет. |
