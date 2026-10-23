# План: DB Exploration — Итерация 2. Исправление ошибок + логирование

## Контекст

Первый прогон `bird_benchmark.py` на `bird_small.json` (22 вопроса) показал **0% accuracy**.
Все `predicted_sql` = `"error"`. Ни один SQL-запрос не был сгенерирован.

Лог-файл: [pseudo_logs_of_bird_try.txt](file:///Users/vlad/Desktop/uni/Магистратура/2_quarter_project/AdvText2SQL/local/pseudo_logs_of_bird_try.txt)

---

## Диагностика: 3 корневых проблемы

### Проблема 1: `InFailedSqlTransaction` — каскадный отказ PostgreSQL

**Что происходит:** В `_explore_column_statistics()` используется одно соединение (`with self.engine.connect() as conn`) для всех таблиц. Когда SQL-запрос для одной таблицы падает (например, type detection для `satscores`), PostgreSQL переводит транзакцию в состояние `aborted`. После этого **все** последующие запросы на этом же соединении падают с ошибкой `InFailedSqlTransaction`.

**Как проявляется:** Первая таблица `set_translations` (или `satscores`) падает → `cards`, `rulings`, `schools`, `frpm` и все остальные тоже падают каскадно → column statistics = пустой dict.

**Решение:** После каждого неудачного SQL-запроса нужно вызывать `conn.rollback()`, чтобы сбросить абортированную транзакцию. Либо использвать отдельное соединение или `SAVEPOINT` на каждую таблицу.

### Проблема 2: `UndefinedFunction: bigint ~ unknown` — regex на не-TEXT колонках

**Что происходит:** Регулярные выражения (`~`) в `_detect_column_types_for_table()` предполагают, что все колонки имеют тип TEXT. Однако некоторые БД (например, `california_schools` с таблицей `satscores`) содержат колонки с реальными типами (`BIGINT`, `INTEGER`).

**Как проявляется:**
```
operator does not exist: bigint ~ unknown
LINE 1: ...ll12__nonnull", COUNT(*) FILTER (WHERE "enroll12" ~ '^-?[0-9...
```

**Решение:** Перед применением regex нужно кастовать колонку в TEXT: `"col"::text ~ '^...'`. Альтернативно — проверять тип колонки из inspector и пропускать regex для уже типизированных колонок.

### Проблема 3: `RateLimitError 429` — LLM вызовы без retry/backoff

**Что происходит:** Все 22 вызова `_check_ambiguity()` падают с `openai.RateLimitError: 429`. В текущем коде нет retry с exponential backoff. Метод `_check_ambiguity()` возвращает `{"status": "error"}`. Метод `query()` (строки 802–804) при `status == "error"` сразу возвращает `{"status": "error"}`. В `bird.py` (строка 45) при `status != "success"` и `!= "ambiguous"` записывается `predicted_sql = "error"`.

**Цепочка отказа:**
```
_check_ambiguity() → 429 → {"status": "error"}
query() → return {"status": "error"}   ← SQL ДАЖЕ НЕ ГЕНЕРИРУЕТСЯ
bird.py → predicted_sql = "error"
execute_safe("error") → SyntaxError
```

**Решение:** Добавить retry с exponential backoff для LLM-вызовов. А также пересмотреть логику `query()` — при ошибке ambiguity check не останавливать pipeline, а продолжать генерацию SQL (ambiguity check опциональный).

---

## Часть 1 — Исправление `_explore_column_statistics()`: rollback транзакций

### 1.1 ROLLBACK после ошибки

В `_explore_column_statistics()` после каждого `except` для таблицы добавить `conn.rollback()`:

```python
def _explore_column_statistics(self) -> dict:
    inspector = inspect(self.engine)
    tables = inspector.get_table_names(schema="public")
    result = {}

    with self.engine.connect() as conn:
        for table in tables:
            try:
                row_count = self._get_table_row_count(conn, table)
                columns = inspector.get_columns(table, schema="public")
                col_types = self._detect_column_types_for_table(conn, table, columns)

                col_stats = {}
                for col in columns:
                    name = col["name"]
                    info = col_types.get(name)
                    if not info or info["detected_type"] == "empty":
                        continue
                    stats = self._compute_column_stats(
                        conn, table, name, info["detected_type"], row_count,
                    )
                    col_stats[name] = {
                        "detected_type": info["detected_type"],
                        "distinct_count": info["distinct_count"],
                        "stats": stats,
                    }

                result[table] = {"row_count": row_count, "columns": col_stats}
            except Exception:
                logger.exception(f"Column statistics exploration failed for table {table}")
                try:
                    conn.rollback()
                except Exception:
                    pass

    return result
```

### 1.2 ROLLBACK в `_get_table_row_count()`

Fallback на `COUNT(*)` тоже может упасть, если транзакция уже aborted. Обернуть в rollback:

```python
def _get_table_row_count(self, conn, table: str) -> int:
    try:
        res = conn.execute(text(
            "SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = :table_name"
        ), {"table_name": table})
        row = res.fetchone()
        if row and row[0] > 0:
            return int(row[0])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    try:
        res = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"'))
        return int(res.scalar())
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
```

---

## Часть 2 — Исправление `_detect_column_types_for_table()`: CAST в TEXT

### 2.1 Добавить `::text` перед regex

Для каждой колонки добавлять `::text` перед оператором `~`:

```python
# Было:
f"COUNT(*) FILTER (WHERE \"{safe}\" ~ '^-?[0-9]+$') AS \"{safe}__integer\""

# Стало:
f"COUNT(*) FILTER (WHERE \"{safe}\"::text ~ '^-?[0-9]+$') AS \"{safe}__integer\""
```

То же самое для `__numeric`, `__date`, `__json`, `__nonnull` (IS NOT NULL не требует каста).

### 2.2 Альтернатива — проверять тип колонки через inspector

Если `col["type"]` уже не TEXT — пропустить regex и сразу присвоить тип на основе SQL-типа.
Это быстрее и не требует sampling для таких колонок.

**Решение: использовать гибридный подход:**
- Если тип колонки уже не TEXT/VARCHAR → определять detected_type по SQL-типу (BIGINT→integer, NUMERIC→numeric, DATE→date)
- Если TEXT/VARCHAR → делать regex-детекцию как раньше, но с `::text` на всякий случай

### 2.3 `_compute_column_stats()` — тоже добавить rollback

В `_compute_column_stats()` при ошибке SQL нужен rollback:

```python
except Exception:
    logger.exception(f"Stats computation failed for {table}.{column}")
    try:
        conn.rollback()
    except Exception:
        pass
```

---

## Часть 3 — Исправление pipeline: RateLimitError + логика `query()`

### 3.1 Retry с exponential backoff для LLM-вызовов

Создать утилиту retry или использовать `tenacity`:

```python
import asyncio

async def _llm_call_with_retry(self, messages, max_retries=3, base_delay=2.0):
    """Обёртка для LLM вызовов с retry и exponential backoff."""
    for attempt in range(max_retries):
        try:
            return await self.llm_client.create(messages)
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Rate limited, retrying in {delay}s (attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
            else:
                raise
    raise RuntimeError(f"LLM call failed after {max_retries} retries")
```

Применить в `_check_ambiguity()`, `generate_sql()`, `_verify_sql_against_query()`.

### 3.2 Изменить логику `query()` при ошибке ambiguity check

Текущая логика (строки 802–804):
```python
if ambiguity_check["status"] == "error":
    return {"status": "error"}  # ← СТОП, SQL не генерируется
```

**Новая логика:** ambiguity check — это soft check. При ошибке продолжать генерацию SQL:

```python
if ambiguity_check["status"] == "error":
    logger.warning("Ambiguity check failed, proceeding with SQL generation")
    # НЕ возвращаем error, продолжаем
```

### 3.3 Добавить задержку между вопросами в benchmark

В `bird.py` добавить `asyncio.sleep()` между вопросами, чтобы не упираться в rate limit:

```python
for item in queries:
    ...
    result = await tool.query(question)
    await asyncio.sleep(1.0)  # throttle between questions
```

---

## Часть 4 — Логирование

### 4.1 Структурированное логирование в файл

Добавить `FileHandler` с JSON-форматом для удобного анализа:

```python
# В text2sql_implementation.py, при инициализации модуля
import logging
import json
from datetime import datetime

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)

def setup_logging():
    logger = logging.getLogger("text2sql_tool")
    logger.setLevel(logging.DEBUG)

    # Консольный вывод — INFO и выше, компактный
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(console)

    # Файловый вывод — DEBUG, JSON
    file_handler = logging.FileHandler("text2sql_debug.log", mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    logger.addHandler(file_handler)
```

### 4.2 Ключевые точки логирования

| Место | Уровень | Что логируем |
|-------|---------|-------------|
| `build()` начало/конец | INFO | `db_uri`, время build, количество таблиц |
| `_explore_db_relationships()` | INFO | Кол-во FK, PK найденных |
| `_explore_column_statistics()` каждая таблица | INFO | Таблица, row_count, кол-во колонок |
| `_detect_column_types_for_table()` | DEBUG | SQL запрос, результат детекции |
| `_compute_column_stats()` ошибка | WARNING | Таблица, колонка, тип ошибки |
| `query()` начало | INFO | user_query |
| `_check_ambiguity()` результат | INFO | ambiguous/ok/error |
| `generate_sql()` результат | INFO | SQL или ошибка |
| `_llm_call_with_retry()` retry | WARNING | attempt, delay, error message |
| `query()` конец | INFO | status, sql (если success) |

### 4.3 Логирование в benchmark

В `base.py` / `bird.py` добавить лог при build каждой БД:

```python
logger.info(f"Building tool for db_id={db_id}")
...
logger.info(f"Build completed for db_id={db_id}, tables: {len(tool.db_schema.split('TABLE '))-1}")
```

---

## Часть 5 — Дополнительные улучшения

### 5.1 `_dump_db_schemas_json` — дампить также relationships и stats

Сейчас дампится только `tool.db_schema`. Расширить `_dump_db_schemas_json()` в `base.py`:

```python
def _dump_db_schemas_json(self, tool_dict, output_path="db_schemas.json"):
    schemas = {}
    for db_id, tool in tool_dict.items():
        schemas[db_id] = {
            "schema": tool.db_schema,
            "relationships": tool.relationships_str,
            "column_stats": tool.column_stats_str,
        }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(schemas, f, ensure_ascii=False, indent=2)
```

### 5.2 Таймаут и замер времени `build()`

Добавить замер времени для `build()` чтобы видеть, сколько уходит на exploration:

```python
import time

def build(self):
    start = time.time()
    ...
    elapsed = time.time() - start
    logger.info(f"Build completed in {elapsed:.1f}s")
```

---

## Сводка: файлы и изменения

| Файл | Что изменить |
|------|-------------|
| `text2sql_implementation.py` | rollback в `_explore_column_statistics()`, `_get_table_row_count()`, `_compute_column_stats()`; CAST `::text` в `_detect_column_types_for_table()`; гибридная детекция типов; `_llm_call_with_retry()`; логика `query()` при error ambiguity; `setup_logging()`; логирование ключевых точек; таймер `build()` |
| `benchmarks/base.py` | Расширить `_dump_db_schemas_json()`; логирование build |
| `benchmarks/bird.py` | `asyncio.sleep()` между вопросами; логирование |
| `benchmarks/token_tracking.py` | _(без изменений)_ |

---

## Приоритет исправлений

1. **🔴 КРИТИЧНО** — rollback транзакций (`_explore_column_statistics` + `_get_table_row_count`)
2. **🔴 КРИТИЧНО** — CAST `::text` перед regex в `_detect_column_types_for_table()`
3. **🔴 КРИТИЧНО** — не прерывать pipeline при ошибке ambiguity check в `query()`
4. **🟡 ВАЖНО** — retry с backoff для LLM-вызовов
5. **🟡 ВАЖНО** — throttle между вопросами в benchmark
6. **🟢 ПОЛЕЗНО** — структурированное логирование в файл
7. **🟢 ПОЛЕЗНО** — расширенный дамп schemas, таймер build

---

## Проверка

1. Поднять SSH-туннель
2. Запустить `uv run --env-file .env bird_benchmark.py`
3. Убедиться, что:
   - Build проходит без cascading errors (логи без `InFailedSqlTransaction`)
   - Column statistics собираются для всех таблиц (включая `satscores`)
   - LLM-вызовы проходят (с retry при 429)
   - `predicted_sql` содержит реальные SQL-запросы, а не `"error"`
   - Accuracy > 0%
4. Проверить лог-файл `text2sql_debug.log` на читаемость и полноту
