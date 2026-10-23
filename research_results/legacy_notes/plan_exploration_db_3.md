# Анализ результатов BIRD Benchmark (после снижения false-ambiguous rate)

## Текущие результаты (запуск от 2026-04-01)

| Метрика | До (baseline) | После правок | 
|---------|--------------|--------------|
| Overall accuracy | 13.64% (3/22) | **36.36% (8/22)** |
| False ambiguous rate | 72.73% | **0.00%** |
| Simple accuracy | 33.33% | **66.67%** |
| Moderate accuracy | 0.00% | **10.00%** |
| Challenging accuracy | 0.00% | **33.33%** |
| Prompt tokens | 96,755 | 191,034 |

Основной прирост: оптимистичный fallback (шаг 5 из `less_false_ambiguity_plan.md`) + переработка промпта ambiguity.

---

## Правильные запросы (8/22)

| Q | DB | Difficulty |
|---|----|------------|
| Q9 | california_schools | simple |
| Q276 | toxicology | simple |
| Q279 | toxicology | simple |
| Q325 | toxicology | simple |
| Q345 | card_games | moderate |
| Q113 | financial | simple |
| Q589 | codebase_community | simple |
| Q1429 | student_club | challenging |

---

## Провальные запросы — категории и анализ

### Категория A — Баг в `sanitize_sql`: DISTINCT+ORDER BY (3 запроса)

**Затронутые:** Q13, Q17, Q1472

**Суть проблемы:**
`sanitize_sql` оборачивает `SELECT DISTINCT ... ORDER BY` в subquery (требование PostgreSQL),
но ORDER BY выносится наружу и теряет доступ к алиасам таблиц из исходного запроса.

```sql
-- Исходный (корректный SQL от LLM):
SELECT DISTINCT "phone"
FROM "schools" s JOIN "satscores" ss ON ...
ORDER BY ("numge1500"::REAL / "numtsttakr"::REAL) DESC LIMIT 3;

-- Что делает sanitize_sql (баг):
SELECT * FROM (
    SELECT DISTINCT "phone"
    FROM "schools" s JOIN "satscores" ss ON ...
) sub
ORDER BY ("numge1500"::REAL / "numtsttakr"::REAL) DESC LIMIT 3;
-- ❌ numge1500 недоступно вне subquery (алиас ss не виден)
```

**Ошибки в логах:**
- Q13: `column "numge1500" does not exist`
- Q17: `missing FROM-clause entry for table "ss"`
- Q1472: `missing FROM-clause entry for table "yearmonth"`

**Варианты исправления:**
1. В `SQL_PROMPT_TEMPLATE` добавить правило: "При SELECT DISTINCT все колонки ORDER BY ОБЯЗАНЫ быть в SELECT"
2. В `sanitize_sql` добавить ORDER BY колонки в SELECT inner-запроса
3. В `sanitize_sql` заменять алиасы таблиц в ORDER BY на `sub.colname`

**Приоритет: P0 — самый простой и результативный фикс (+3 запроса)**

---

### Категория B — Баги в gold SQL (2 запроса) — НЕ НАША ВИНА

**Затронутые:** Q417, Q1533

Gold SQL для обоих запросов содержит невалидный синтаксис `NULLIF(NULLIF,0)(COUNT(...), 0)`.
Эти запросы дают 0 баллов независимо от качества нашего ответа — проблема в данных бенчмарка.

```sql
-- Q417 gold (невалидный):
... / NULLIF(NULLIF,0)(COUNT(T1.id), 0)

-- Q1533 gold (невалидный):
WHERE T1.Price / NULLIF(NULLIF,0)(T1.Amount, 0) > 29.00
```

**Наш SQL для обоих был корректным.** Ничего не делаем.

---

### Категория C — LLM генерирует `SELECT SUM(), DISTINCT col` (1 запрос)

**Затронутый:** Q685 — "Identify total views on post 'Computer Game Datasets'"

Все 7 попыток генерировали один и тот же невалидный паттерн:
```sql
SELECT
    SUM("viewcount") AS total_views,
    DISTINCT "ownerdisplayname"   -- ❌ DISTINCT после запятой недопустим
FROM "posts"
```

LLM "застрял" в этом паттерне потому что prompt требует DISTINCT для одиночных колонок,
но здесь два поля — и LLM применяет DISTINCT к одному из них.

**Исправления:**
1. Добавить в `SQL_PROMPT_TEMPLATE`: "DISTINCT пишется ТОЛЬКО сразу после SELECT, никогда после запятой"
2. Добавить в `sanitize_sql`: убирать `DISTINCT` если он стоит не сразу после SELECT
3. Добавить feedback в retry-цикл: передавать текст ошибки валидации в следующую попытку

**Приоритет: P0 (+1 запрос), плюс полезно для общей надёжности**

---

### Категория D — Транслитерация имён собственных (1 запрос)

**Затронутый:** Q1339 — "Calculate the average cost that Elijah Allen spent in September and October"

LLM перевёл имя на русский:
```sql
WHERE m."first_name" ILIKE '%Элайджа%' AND m."last_name" ILIKE '%Аллен%'
-- ❌ В БД хранится 'Elijah', 'Allen'
```

**Причина:** system prompt содержит "Учитывай особенности русской морфологии в запросах" —
LLM интерпретирует это как разрешение/обязательство транслитерировать имена.

**Исправление:**
Добавить в `SYSTEM_PROMPT_TEMPLATE`:
```
13. Имена собственные, названия, строковые значения из запроса пользователя —
    использовать ТОЧНО как написано в запросе. Не переводить и не транслитерировать!
```

**Приоритет: P0 (+1 запрос)**

---

### Категория E — Ошибки в логике SQL (5 запросов)

| Q | DB | Difficulty | Проблема |
|---|----|------------|---------|
| Q498 | card_games | moderate | Вернул `sets.name` вместо `set_translations.translation` (нужен текст перевода, не название сета) |
| Q1480 | debit_card_specializing | moderate | Возвращает полную строку даты `'201301'` вместо месяца `'01'` — нужен `SUBSTRING(date FROM 5 FOR 2)` |
| Q1529 | debit_card_specializing | moderate | Использует колонку `amount` вместо `price`; неправильная обработка дат (date в этой БД — TEXT в формате YYYYMM) |
| Q180 | financial | moderate | Неправильный JOIN: `client.account_id` не существует — нужно идти через `disp`: `client → disp → account` |
| Q1388 | student_club | moderate | Использует `WHERE amount = MAX(amount)` вместо `GROUP BY ... ORDER BY SUM DESC LIMIT 1` — не учитывает суммирование |

**Для Q1480 и Q1529** — проблема в понимании формата дат в БД `debit_card_specializing`:
поле `Date` в таблице `yearmonth` хранится как TEXT в формате `YYYYMM` (например, `'201301'`).
LLM не понимает этот формат и пытается работать с ним как с обычной датой.

**Возможное улучшение:** В `_format_column_statistics` для полей, которые выглядят как YYYYMM,
явно указывать формат: `date (TEXT, format: YYYYMM)`.

**Приоритет: P1 (требует более глубокой доработки промптов)**

---

### Категория F — NULL handling (1 запрос)

**Затронутый:** Q1365 — "expenses of the budget with the lowest remaining"

Наш SQL:
```sql
WHERE "b"."remaining" = (SELECT MIN("remaining") FROM "budget")
```

Gold SQL:
```sql
ORDER BY T1.remaining NULLS FIRST LIMIT 1
```

Gold использует `NULLS FIRST` — если минимальное remaining это NULL, `WHERE = MIN()` не найдёт такие строки.
Это edge case с NULL values в агрегации.

**Приоритет: P2**

---

## Итоговый приоритет исправлений

| Категория | Запросов | Сложность | Ожидаемый прирост accuracy |
|-----------|----------|-----------|---------------------------|
| A — Баг sanitize_sql (DISTINCT+ORDER BY) | 3 | Низкая | +13.6% → ~50% |
| C — DISTINCT после запятой в SELECT | 1 | Низкая | +4.5% |
| D — Транслитерация имён | 1 | Низкая | +4.5% |
| B — Gold SQL баги | 2 | Нефиксируемо | 0% |
| E — Логика SQL | 5 | Высокая | +9-14% |
| F — NULL handling | 1 | Средняя | +4.5% |

**После A+C+D → ожидаемая точность: ~50-55%**
**После A+C+D+E (частично) → ожидаемая точность: ~59-64%**

---

## Следующие шаги

1. **[P0]** Исправить `sanitize_sql` — баг с DISTINCT+ORDER BY алиасами
2. **[P0]** Добавить в `SQL_PROMPT_TEMPLATE` запрет на `DISTINCT` после запятой
3. **[P0]** Добавить в `SYSTEM_PROMPT_TEMPLATE` запрет на транслитерацию имён
4. **[P1]** Улучшить логику для Q498 (нужен translation, не set name)
5. **[P1]** Улучшить обработку TEXT-дат формата YYYYMM (Q1480, Q1529)
6. **[P1]** Улучшить логику GROUP BY + ORDER BY + LIMIT для агрегаций (Q1388)
7. **[P2]** NULL handling в агрегациях (Q1365)
