# План: DB Exploration — FK/PK + Column Statistics

## Зачем

Текущий `_get_db_schema_light()` возвращает только имена таблиц, столбцов и типы.
Этого не хватает для генерации правильных SQL-запросов — LLM не знает:
- какие таблицы связаны и через какие ключи (JOIN строятся наугад)
- реальных типов данных (все колонки объявлены как TEXT)
- диапазонов значений, распределений, форматов дат

Цель: обогатить системный промпт связями между таблицами и статистиками колонок.

---

## Часть 1 — FK / PK через SQLAlchemy Inspector

### Почему Inspector, а не raw SQL

Исходный вариант использовал запросы к `information_schema.table_constraints` + JOIN-ы.
Это корректно и стандартно, но:
- `information_schema.constraint_column_usage` — PostgreSQL-специфичная вьюха
- Для multi-column FK raw SQL может дать Cartesian product колонок
- SQLAlchemy inspector уже импортирован и используется в коде

Inspector-методы `get_foreign_keys()` и `get_pk_constraint()` проще, надёжнее и database-agnostic.

### 1.1 Метод `_explore_db_relationships()`

Извлекает FK и PK для всех таблиц через `inspector`:

```python
def _explore_db_relationships(self) -> dict:
    inspector = inspect(self.engine)
    tables = inspector.get_table_names(schema="public")

    foreign_keys = []
    primary_keys = {}

    for table in tables:
        pk = inspector.get_pk_constraint(table, schema="public")
        if pk and pk.get("constrained_columns"):
            primary_keys[table] = pk["constrained_columns"]

        for fk in inspector.get_foreign_keys(table, schema="public"):
            foreign_keys.append({
                "from_table": table,
                "from_cols": fk["constrained_columns"],
                "to_table": fk["referred_table"],
                "to_cols": fk["referred_columns"],
            })

    return {"foreign_keys": foreign_keys, "primary_keys": primary_keys}
```

### 1.2 Метод `_format_relationships()`

DDL-стиль — компактный и привычный для LLM:

```
-- Primary Keys:
-- orders: id
-- products: product_id

-- Foreign Keys:
-- orders.customer_id -> customers.id
-- order_items.order_id -> orders.id
```

---

## Часть 2 — Column Statistics

### 2.1 Количество строк — `_get_table_row_count()`

Два подхода с fallback:

1. **Быстрый (приблизительный):** `pg_stat_user_tables.n_live_tup` — PostgreSQL-специфично, требует `ANALYZE`
2. **Fallback (точный):** `SELECT COUNT(*) FROM "table"`

```python
def _get_table_row_count(self, conn, table: str) -> int:
    # Сначала пробуем статистику
    res = conn.execute(text(
        "SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = :table_name"
    ), {"table_name": table})
    row = res.fetchone()
    if row and row[0] > 0:
        return int(row[0])

    # Fallback на точный подсчёт
    res = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
    return int(res.scalar())
```

### 2.2 Детекция типов — `_detect_column_types_for_table()`

Все колонки в БД — TEXT (результат миграции через pgloader).
Нужно определить реальный тип контента через sampling.

**Один SQL-запрос на таблицу** — для каждой колонки считаем regex-совпадения на sample из 500 строк:

```sql
SELECT
    COUNT(*) FILTER (WHERE "col" IS NOT NULL) AS col_nonnull,
    COUNT(*) FILTER (WHERE "col" ~ '^-?[0-9]+$') AS col_integer,
    COUNT(*) FILTER (WHERE "col" ~ '^-?[0-9]+(\.[0-9]+)?$') AS col_numeric,
    COUNT(*) FILTER (WHERE "col" ~ '^\d{4}-\d{2}-\d{2}') AS col_date,
    COUNT(*) FILTER (WHERE "col" ~ '^[\[{]') AS col_json,
    COUNT(DISTINCT "col") AS col_distinct
FROM (SELECT * FROM "table" ORDER BY RANDOM() LIMIT 500) s
```

Запрос генерируется динамически для всех колонок таблицы сразу (один round-trip на таблицу).

**Классификация (Python):**
- `integer_count / nonnull >= 0.9` → integer
- `numeric_count / nonnull >= 0.9` → numeric
- `date_count / nonnull >= 0.9` → date
- `json_count / nonnull >= 0.5` → json/list
- иначе → categorical

**Почему regex, а не CAST:**
PostgreSQL не имеет `TRY_CAST`. Использование `col::numeric` внутри `CASE WHEN` упадёт на первом невалидном значении.
PL/pgSQL-функция с exception handling требует `CREATE FUNCTION`, а пользователь read-only.

### 2.3 Статистики по типу — `_compute_column_stats()`

**Числовые (integer/numeric):**
```sql
SELECT
    MIN("col"::numeric), MAX("col"::numeric),
    ROUND(AVG("col"::numeric), 2),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "col"::numeric)
FROM "table"
WHERE "col" IS NOT NULL AND "col" ~ '^-?[0-9]+(\.[0-9]+)?$'
```
Для больших таблиц (>50k строк) медиана пропускается (требует полной сортировки).

**Категориальные:**
- Если unique <= 10: показать все значения
- Если unique > 10: top-5 по частоте + общее число уникальных

```sql
SELECT "col", COUNT(*) AS freq
FROM "table" WHERE "col" IS NOT NULL
GROUP BY "col" ORDER BY freq DESC LIMIT 5
```

**Даты:**
```sql
SELECT MIN("col"::date), MAX("col"::date)
FROM "table"
WHERE "col" ~ '^\d{4}-\d{2}-\d{2}'
```

**JSON/списки:** помечаем тип, без детальной статистики.

### 2.4 Оркестратор — `_explore_column_statistics()`

Для каждой таблицы:
1. Получить row count
2. Запустить type detection (1 запрос на таблицу)
3. Для каждой колонки вычислить stats по типу

Возвращает:
```python
{
    "table_name": {
        "row_count": 1234,
        "columns": {
            "col_name": {
                "detected_type": "numeric",
                "distinct_count": 50,
                "stats": {"type": "numeric", "min": 0.5, "max": 999.0, ...}
            }
        }
    }
}
```

### 2.5 Форматирование — `_format_column_statistics()`

Компактный формат для промпта:

```
TABLE orders (1,234 rows)
  - order_id: integer, unique (id)
  - amount: numeric, range [0.50, 999.00], mean=45.20, median=40.00
  - status: categorical, 4 unique, values: "completed", "pending", "cancelled", "refunded"
  - created_at: date, range [2020-01-01, 2024-12-31]
  - metadata: json/list
  - customer_name: categorical, 892 unique, top: "Иванов"(50), "Петров"(30), ...
```

**Оптимизации токенов:**
- Пропускаем колонки без данных (все NULL)
- ID-колонки (distinct == row_count) помечаем просто `unique (id)`
- stddev не показываем (LLM редко нужна)

---

## Часть 3 — Интеграция

### 3.1 Обновлённый `build()`

```python
def build(self):
    self.db_schema = self._get_db_schema_light()
    self.db_relationships = self._explore_db_relationships()
    self.relationships_str = self._format_relationships()
    self.column_stats = self._explore_column_statistics()
    self.column_stats_str = self._format_column_statistics(self.column_stats)
    self.system_prompt = self._create_system_prompt()
```

### 3.2 Обновлённый `_create_system_prompt()`

```python
def _create_system_prompt(self) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        db_schema=self.db_schema,
        db_relationships=self.relationships_str,
        column_statistics=self.column_stats_str,
        sql_dialect="PostgreSQL",
    )
```

### 3.3 Обновлённый `SYSTEM_PROMPT_TEMPLATE` в `prompts.py`

```
### Схема базы данных:
{db_schema}

### Связи между таблицами:
{db_relationships}

### Статистика столбцов:
{column_statistics}

### Правила генерации SQL:
...
```

---

## Часть 4 — Производительность

| Аспект | Решение |
|--------|---------|
| Большие таблицы (100k+ строк) | `ORDER BY RANDOM() LIMIT 500` для type detection; медиана пропускается |
| Много колонок в таблице | Один запрос на всю таблицу, не по одному на колонку |
| Сетевая задержка (SSH-туннель) | Минимизация round-trip через батчинг |
| `pg_stat_user_tables` может быть пустой | Fallback на `COUNT(*)` |

**Оценка:** типичная БД — 4-8 таблиц по 5-60 колонок. На каждую таблицу: 1 запрос type detection + N запросов stats.
Worst case: ~960 запросов по <100ms = ~96 секунд. `build()` запускается один раз на БД, это допустимо.

**Оценка токенов:** ~60-80 символов на колонку ≈ 400-500 токенов на таблицу. Для 8 таблиц — ~4000 токенов.
Текущая light-схема ≈ 2000 токенов. Суммарно ~6000 — допустимо для контекстов 128k+.

---

## Часть 5 — Обработка ошибок

Все SQL-запросы обёрнуты в `try/except` (по аналогии с `_get_db_schema_heavy()`).
Если статистика для колонки/таблицы не получена — пропускаем.
Частичная информация лучше, чем упавший `build()`.

---

## Часть 6 — Проверка

1. Поднять SSH-туннель (`ssh -N -L 5444:10.11.1.6:5444 ...`)
2. Добавить временный `print(self.system_prompt)` в `build()` для визуальной проверки
3. Запустить `uv run --env-file .env bird_benchmark.py` на `bird_small.json` (22 вопроса)
4. Проверить `db_schemas.json` — убедиться, что связи и статистики корректны
5. Сравнить accuracy до и после (особенно вопросы с JOIN)

---

## Сводка: файлы и методы

| Файл | Что изменено |
|------|-------------|
| `text2sql_implementation.py` | 7 новых методов + обновлены `build()`, `_create_system_prompt()` |
| `prompts.py` | 2 новые секции в `SYSTEM_PROMPT_TEMPLATE` |

| Метод | Назначение |
|-------|-----------|
| `_explore_db_relationships()` | FK + PK через inspector |
| `_format_relationships()` | DDL-формат для промпта |
| `_get_table_row_count()` | Row count с fallback |
| `_detect_column_types_for_table()` | Regex-детекция типов на sample |
| `_compute_column_stats()` | Статистики по обнаруженному типу |
| `_explore_column_statistics()` | Оркестратор |
| `_format_column_statistics()` | Компактное форматирование |
