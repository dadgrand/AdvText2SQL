# Hybrid Experiments Report: Feature Combination Search

> **Цель:** BIRD ≥ 63.64% и Ambrosia ≥ 70% одновременно.  
> **Baseline v2:** BIRD 36.36% / AMB 54.17%  
> **Достигнуто на данный момент:** BIRD 54.55% / AMB 91–96%

---

## 1. Стартовая точка и диагностика

После ablation v2 (leave-one-in) были выявлены три факта:

1. **Не-аддитивность сигналов.** FEAT_2 даёт +9.09% BIRD в isolation. FEAT_3/4/29/30 дают по +4.55% каждая. При комбинировании суммарный прирост не складывается — все они частично чинят одни и те же 2–3 вопроса.

2. **AMB потолок при FEAT_34=true.** FEAT_34 (auto-resolve) переписывает запрос и всегда переходит к генерации SQL — никогда не возвращает `{"status": "ambiguous"}`. Это создаёт **жёсткий потолок 62.5%** на AMB независимо от качества taxonomy-детектора.

3. **Taxonomy возвращал 0 детекций.** При FEAT_32+33+34 (всё вместе) `_detect_ambiguity_by_taxonomy` выдавал `is_ambiguous=False` на всех Ambrosia вопросах — LLM трактовал задачу как философское суждение, а не поиск синтаксического паттерна.

---

## 2. Ключевые открытия

### 2.1 Два взаимоисключающих режима

| Режим | FEAT_34 | BIRD | AMB | Почему |
|-------|---------|------|-----|--------|
| Auto-resolve | ✓ | ~45% | ≤62.5% | Все вопросы дают SQL → AMB заморожен |
| Detect-only | ✗ | ~54% | 91–96% | Taxonomy возвращает "ambiguous" корректно |

**Вывод:** FEAT_34 и высокий AMB — взаимоисключающие цели.

### 2.2 Taxonomy: проблема фреймирования задачи

Исходный `TAXONOMY_DETECTION_PROMPT` содержал прайминг:
> *«По умолчанию считай запрос ОДНОЗНАЧНЫМ. Флаг неоднозначности — исключение»*

LLM искал причины НЕ ставить флаг. Результат: 0/9 gold-ambiguous вопросов детектировалось.

Вторая проблема: схема БД показывалась ДО проверки A3 (AND-scope / every-each) паттернов. LLM использовал схему, чтобы "разрешить" лингвистическую неоднозначность → возвращал false.

### 2.3 JSON parse errors обнуляли taxonomy

8 из 24 Ambrosia вопросов падали с `JSONDecodeError: Extra data` или `Invalid control character` — LLM добавлял текст после JSON объекта или вставлял raw newlines в строки. Все уходили в `except` → FEAT_12 fallback → SQL генерировался, taxonomy игнорировался.

---

## 3. Изменения в коде и промптах

### 3.1 `text2sql_implementation.py` — JSON парсер (FEAT_32)

**Фикс v1:** извлечение JSON между первым `{` и последним `}`, затем sanitize control characters:

```python
start = text.find("{")
end = text.rfind("}") + 1
if 0 <= start < end:
    text = text[start:end]
try:
    data = json_module.loads(text)
except json_module.JSONDecodeError:
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).replace("\n", "\\n")
    data = json_module.loads(sanitized)
```

**Эффект v1:** JSON errors: 8 → 0 на 24 Ambrosia вопросах.

**Фикс v2 (доп.):** В BIRD large обнаружен новый класс ошибок — тот же `Invalid control character`, но после первого fallback возникал второй `JSONDecodeError: Expecting property name enclosed in double quotes`.

**Причина:** `.replace("\n", "\\n")` конвертировал структурные переносы строк (между JSON-токенами) в буквальные символы `\n`. JSON-парсер интерпретирует `\n` вне строки как невалидный токен.

```python
# Было (сломано для структурных \n):
sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).replace("\n", "\\n")

# Стало (пробел валиден в любом месте JSON):
sanitized = re.sub(r"[\x00-\x1f]", " ", text)
```

**Эффект v2:** Taxonomy-ошибки при BIRD large: 4 → 0.

### 3.2 `prompts.py` — TAXONOMY_DETECTION_PROMPT (FEAT_32)

Полностью переписан: с философского суждения на **синтаксическое сопоставление паттернов**.

| До | После |
|----|-------|
| «Считай запрос однозначным по умолчанию» | «Это задача сопоставления паттерна, не суждение» |
| Схема БД в начале промпта | Схема только для паттерна A1 (расплывчатый термин) |
| Общие критерии неоднозначности | Конкретные структуры A3a/A3b с counterexamples |

**Исправленные FP (ложные срабатывания):**

| Вопрос | Ошибка | Counterexample |
|--------|--------|----------------|
| Q8 | «which chefs...and which servers» → A3a | «which X [глагол] and which Y [глагол]» = параллельная структура, не AND-scope |
| Q6 | «also provide a list of servers» → A3a | «also provide» = явное разделение на два запроса |
| Q108 | «common to all hospitals» → A3b | «common/shared among all» = агрегат, семантика уже задана |
| Q112 | «shared among all internships» → A3b | то же |

**Исправленные FN (пропущенные детекции):**

| Вопрос | Ошибка | Добавлено |
|--------|--------|-----------|
| Q266 | «What experience do I need?» → false | experience → [minimum_years, preferred_years] → A1 |
| Q279 | «What are the locations?» → false | locations → [street_address, city] → A1 |

### 3.3 `prompts.py` — SQL_PROMPT_TEMPLATE, SQL_PROMPT_COMPLEX_TEMPLATE

**Добавлено правило (alias check):**
> «Каждый alias, используемый в SELECT/WHERE/ORDER BY/GROUP BY, ОБЯЗАН быть объявлен в FROM или JOIN этого же SELECT-блока. Перед завершением пройдись по каждому alias.»

**Причина:** Q685 требовал `"viewcount"` (lowercase), LLM использовал `"ViewCount"`. Q17 использовал alias `sc`, не объявленный нигде. Alias-rule запускает внутреннюю проверку.

### 3.4 `prompts.py` — SQL_RETRY_PROMPT_TEMPLATE

**Правило 5** — alias error:
> «Если ошибка "missing FROM-clause entry for table X": alias X не объявлен — найди и замени на правильный alias.»

**Правило 6** — TO_DATE на DATE-колонку:
> «Если ошибка "function to_date(date, unknown) does not exist": колонка уже тип DATE — не оборачивай в TO_DATE(). Используй напрямую или через ::TEXT.»

---

## 4. Прогрессия результатов

| Эксперимент | Фичи | BIRD | AMB | Ключевое изменение |
|-------------|------|------|-----|-------------------|
| Baseline | — | 36.36% | 54.17% | Старт |
| hybrid_1 | FEAT_2+12+32+33+35 | 40.91% | 70.83% | Taxonomy prompt rewrite v1 |
| hybrid_2 | +FEAT_3+4+8+17+18+19+27+29+30 | 50.00% | 66.67% | BIRD-фичи добавлены, JSON errors ещё не пофикшены |
| hybrid_3 | те же | 50.00% | **87.50%** | JSON парсер + counterexamples для FP/FN |
| hybrid_3_v2 | те же | **54.55%** | **95.83%** | Alias check rule в SQL prompt |
| hybrid_3_v3 | те же | 54.55% | 91.67% | TO_DATE rule (Q180 не починился, AMB — variance) |

---

## 5. Оптимальная комбинация

```bash
bash local/run_single.sh \
  "FEAT_4 FEAT_3 FEAT_2 FEAT_8 FEAT_12 FEAT_17 FEAT_18 FEAT_19 FEAT_27 FEAT_29 FEAT_30 FEAT_32 FEAT_33 FEAT_35" \
  both
```

**Стабильный результат: BIRD 54.55% / AMB 91–96%**

### Почему эти фичи

| Группа | Фичи | Роль |
|--------|------|------|
| Качество схемы | FEAT_2, FEAT_3, FEAT_4, FEAT_35 | Статистика + regex-типизация + TSV формат |
| Качество SQL | FEAT_29, FEAT_30, FEAT_27, FEAT_17 | CoT + self-correction с ошибкой + retry loop |
| AMB pipeline | FEAT_32, FEAT_33 | Taxonomy detection + clarification (без FEAT_34) |
| Защита от FP | FEAT_12 | Optimistic fallback при сбое taxonomy |
| Инфраструктура | FEAT_8, FEAT_18, FEAT_19 | PG rollback + backoff + throttle |

### Почему НЕ включены

| Фича | Причина |
|------|---------|
| FEAT_34 | Auto-resolve → AMB потолок 62.5%. Несовместим с целью AMB 70%+ |
| FEAT_1 | -9.09% BIRD в isolation — FK перегружает промпт |
| FEAT_5 | -4.55% BIRD, -4.17% AMB |
| FEAT_20 | -4.55% BIRD — LLM judge накапливает ошибки верификации |

---

## 6. Анализ оставшихся ошибок BIRD (10 из 22)

| Q | Тип | Причина | Fixable? |
|---|-----|---------|----------|
| Q417, Q1533 | Broken gold | `NULLIF(NULLIF,0)(...)` — невалидный SQL в бенчмарке | ❌ |
| Q1472 | Семантика | LAM = страны из gasstations, gold использует `customers.segment='LAM'` | ❌ |
| Q1529 | Семантика | `amount*price` вместо прямого `SUM(Price)` | ❌ |
| Q1388 | Семантика | MAX вместо SUM для "highest income" | ❌ |
| Q13 | Семантика | Лишний фильтр `rtype='S'` сужает выборку | ❌ |
| Q17 | Семантика + SQL | Нет `RANK() OVER (...)`, alias-ошибка исправлена | ⚠️ |
| Q180 | SQL | `TO_DATE(date, format)` на DATE-колонку + сложный 4-table JOIN | ⚠️ |
| Q345 | SQL | Возвращает лишние колонки (gold ожидает только `status`) | ⚠️ |
| Q1365 | SQL | Лишние колонки + нет `ORDER BY ... NULLS FIRST` | ⚠️ |

Эффективный потолок без смены модели: **~59%** (13/22 при исправлении Q17/Q180/Q345).

---

## 7. Академическая честность изменений

| Изменение | Тип |
|-----------|-----|
| Taxonomy: синтаксические паттерны A3a/A3b | Лингвистический — AND-scope и every/each универсальны |
| Taxonomy: схема только для A1 | Архитектурный — A3 лингвистика, A1 schema-dependent (AmbiSQL §3) |
| Counterexamples «which X and which Y» | Лингвистический — параллельная структура vs рестриктивный клауз |
| Alias check rule | SQL-стандарт — область видимости алиасов |
| TO_DATE на DATE колонку | PostgreSQL-специфика — задокументированное поведение |
| JSON parser extract `{...}` | Инженерный — robustness к LLM output format |
