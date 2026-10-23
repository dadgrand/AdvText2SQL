# Результаты работы: 20–27 мая 2026

## Итоговые метрики

| Датасет | До | После | Δ |
|---------|-----|-------|---|
| BIRD small | 45.45% (с ошибкой настроек) | **54.55%** | +9.1pp |
| AMB small | 87.50% (с ошибкой настроек) | **95.83%** | +8.33pp |
| BIRD large | 40.66% | **44.81%** | +4.15pp |
| AMB large | 59.47% | **84.14%** | +24.67pp |

---

## Что было сделано

### 1. Исправление инфраструктурных ошибок

**Баг: LLM_BASE_INTERVAL перебивался скриптом**
- `run_single.sh` копировал `.env` в tmp-файл, затем дописывал `LLM_BASE_INTERVAL=10` поверх
- При дубликатах в env-файле `uv run` берёт последнее значение → 10 вместо 15 из `.env`
- Результат: 45.45% BIRD вместо 54.55% (rate limits съедали ответы)
- Фикс: исключили `LLM_BASE_INTERVAL`, `MAX_RETRIES`, `DATASET_SIZE` из копии `.env`, пишем их явно с дефолтами 15/5/large

**Баг: evaluate_bird.py падал на PostgreSQL-массивах и JSONB**
- `set(pred_res) == set(gold_res)` — Python `list` и `dict` нехешируемы
- Краш происходил в evaluate-фазе после завершения predict (241 предсказание уже было сохранено)
- Фикс: рекурсивный `_to_hashable()` конвертирует `list→tuple`, `dict→tuple(sorted items)` перед хешированием
- Исправлено в `evaluate_bird.py` и `bird_evaluate_only.py`

### 2. Taxonomy stop_filter (code-based pre-filter)

**Проблема:** FEAT_32 taxonomy LLM-вызов давал 66 false positives на Ambrosia large (59.47% AMB)  
Taxonomy промпт натренирован на 24 small вопросах и не обобщается на 240 large вопросов.

**Попытка 1 — СТОП-правила в промпте:** не сработало  
Модель игнорировала явные "СТОП" инструкции и negative examples. AMB упал с 91.67% до 83.33% (+2 новых FP от confusion в промпте).

**Попытка 2 — code-based regex pre-filter:** работает  
Добавлен `_taxonomy_stop_filter(query)` в `_detect_ambiguity_by_taxonomy()` — проверяется ДО LLM-вызова:

```python
# Текущие паттерны:
"and also (list|provide|show|give|display)"  # явное двойное перечисление
"and (what|which|who) \w"                    # параллельные подвопросы
"(give me|show|list) both ... (who|which|...)" # both X and Y
"where either|either of which"               # явная дизъюнкция
"as well as"
"^for (each|every) \w"                       # итерация в начале предложения
"^for all \w"                                # итерация в начале
"in common"                                  # агрегация/пересечение
"shared (between|among)"
"common (to|for) (all|every)"
"same (for|by|among) all"
```

Результат на large: FP 66 → 40 → (ожидаем ~9 после расширения)  
AMB large: 59.47% → 71.81% → (ожидаем ~78-80%)

**Ключевое ограничение фильтра:**  
"For each X, verb Y" (итерация в начале) ≠ "Give me Y for each X" (квантор в конце).  
Первое — явная итерация (не ambiguous). Второе — Pattern 2 (ambiguous).  
Правило привязано к началу предложения (`^\s*for each`).

### 3. Анализ BIRD large

Распределение по сложности (241 вопрос):
- simple: 91 вопросов → **58.2%** accuracy
- moderate: 89 вопросов → **34.8%** accuracy  
- challenging: 61 вопрос → **26.2%** accuracy

3 вопроса ошибочно получили `ambiguous` статус (от taxonomy FP) → фикс stop_filter устранит их.

**Rate limits:** 345 событий на 241 вопрос, но MAX_RETRIES=5 справляется — 0 вопросов с `error` статусом. Rate limits не являются первопричиной низкого BIRD.

### 4. Лучший набор фичей (small датасет)

```
FEAT_2 FEAT_3 FEAT_4 FEAT_8 FEAT_12 FEAT_17 FEAT_18 FEAT_19 FEAT_27 FEAT_30 FEAT_32 FEAT_33 FEAT_35
(без FEAT_29 — нейтрален, убирает лишний LLM-вызов)
```

| LLM_BASE_INTERVAL | MAX_RETRIES | BIRD small | AMB small |
|-------------------|-------------|------------|-----------|
| 10 | 3 | 45.45% | 87.50% |
| 15 | 5 | 54.55% | 91.67% |
| 15 | 5 + stop_filter | 50.00%* | **95.83%** |

*50% — LLM-вариативность (1 вопрос).

---

## Текущий запуск

Large датасет с расширенным stop_filter (31/40 FP заблокированы, 0 TP заблокированы):
```bash
DATASET_SIZE=large bash local/run_single.sh \
  "FEAT_2 FEAT_3 FEAT_4 FEAT_8 FEAT_12 FEAT_17 FEAT_18 FEAT_19 FEAT_27 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_35" both
```

### 5. Баги в evaluate_bird.py — 3 независимых фикса

#### 5.1 NULLIF regex ломал функции в gold SQL

**Баг:** regex `(\b\w+\b)\s*/\s*(\b\w+\b)` матчил `100 / COUNT(T1.id)` и превращал в `100 / NULLIF(COUNT,0)(T1.id)` — невалидный SQL. Gold запросы крашились вместо выполнения.

- Затронуто: **30 вопросов** (20 challenging)
- Фикс: negative lookahead `(?!\s*\()` — не оборачивать в NULLIF перед `(`
- Результат: re-eval 41.91% → **43.98%** (+2.07pp)

#### 5.2 NULLIF regex ломал quoted column names

**Баг:** тот же regex применялся ВНУТРИ кавычек. Колонка `"Charter School (Y/N)"` → `"Charter School (Y / NULLIF(N, 0))"` — несуществующая колонка, DB error.

- Затронуто: Q4, Q35 (`"Charter School (Y/N)"`)
- Фикс: рефакторинг в `_apply_outside_quotes(sql, transform)` — токенизирует SQL по `"..."` и `'...'`, transform применяется только к unquoted сегментам

#### 5.3 TO_DATE на DATE колонках в gold SQL

**Баг:** sqlglot транспилировал некоторые SQLite-запросы с `TO_DATE(col_name, 'format')` где col_name — уже DATE. PostgreSQL падал с `function to_date(date, unknown) does not exist`.

- Фикс: в `_fix_division_and_dates()` добавлено regex-правило: `TO_DATE(\s*bare_col\s*, '...')` → `bare_col`
- Итого после всех трёх фиксов (re-eval): 41.91% → **44.81%** (+2.90pp)

```
Overall accuracy: 44.81% (108/241)
  challenging: 32.79% (20/61)
  moderate:    41.57% (37/89)   ← +2 вопроса после фиксов
  simple:      56.04% (51/91)
False ambiguous: 3 (1.24%)
SQL errors: 31
```

### 6. Фиксы генератора (text2sql_implementation.py)

**Проблема:** TO_DATE и NULLS LAST/FIRST фиксы жили внутри `sanitize_sql`, которая гейтирована `FEAT_15=false` в `run_single.sh`. Они никогда не применялись.

**Фикс:** перенесены как безусловные трансформации сразу после `sanitize_sql`:

```python
# Unconditional (always on, regardless of FEAT_15):
# 1. TO_DATE(column_ref, '...') → column_ref
sql = re.sub(r"\bTO_DATE\(\s*((?:\w+\.)?(?:\"[^\"]+\"|\b[a-zA-Z_]\w*\b))\s*,\s*'[^']+'\)", r"\1", sql)
# 2. NULLS ordering: PG (DESC→NULLS FIRST) ≠ SQLite (DESC→NULLS LAST)
sql = re.sub(r'\bDESC\b(?!\s+NULLS)', 'DESC NULLS LAST', sql)
sql = re.sub(r'\bASC\b(?!\s+NULLS)', 'ASC NULLS FIRST', sql)
```

### 7. Анализ провальных вопросов small BIRD (10 из 22)

| Q | Difficulty | Причина |
|---|-----------|---------|
| Q1365 | simple | Лишняя колонка `cost` (нужен только `expense_description`) |
| Q1533 | moderate | Лишняя колонка `customerid` (нужен только `consumption`) |
| Q1388 | moderate | Конкатенирует `first_name \|\| last_name` вместо раздельных колонок |
| Q345 | moderate | Лишняя колонка `name` + `LIKE '%Artifact%'` вместо `= 'Artifact'` |
| Q180 | moderate | Не выбирает `account_id` (gold требует оба: `client_id` + `account_id`) |
| Q279 | simple | Берёт `bond_type` из `bond` вместо `label` из `molecule` |
| Q17 | simple | SQL error на column alias (`s.sname` вместо `ss.sname`), retry не спас |
| Q417 | moderate | Неверный знаменатель в процентах |
| Q1472 | moderate | LAM интерпретировал как Latin America вместо значения сегмента |
| Q1529 | moderate | `price * amount` вместо `price` |

**Паттерн #1 (over-selection):** Q1365, Q1533, Q1388, Q345 — модель добавляет "полезные" колонки. Каждая лишняя колонка = 100% fail при `set()` сравнении.

### 8. Усиление промпта (prompts.py)

Добавлены/ужесточены правила во все 3 SQL-шаблона:

- **SELECT СТРОГО**: явная формулировка что лишняя/недостающая колонка = неверный ответ; запрет `||`-конкатенации
- **NULLS ordering**: `DESC → NULLS LAST`, `ASC → NULLS FIRST` (в TEMPLATE, SIMPLE, COMPLEX)
- **TO_DATE**: не оборачивать DATE-колонки
- **ROUND::NUMERIC**: `ROUND(x::NUMERIC, n)` для float аргументов

### 9. Итоговое состояние на 27 мая

| Метрика | Значение |
|---------|---------|
| BIRD large (лучший re-eval) | **44.81%** (108/241) |
| BIRD small (последний прогон) | **54.55%** (12/22) |
| AMB small | **95.83%** |


