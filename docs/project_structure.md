# Структура проекта

Актуально после организации файлов 23.10.2026. Подробности исследований —
[01_gpt_research.md](01_gpt_research.md), следующий этап —
[02_feature_selector.md](02_feature_selector.md).

```text
AdvText2SQL/
├── защита/                             # Копии презентаций и исторических отчётов для защиты
│   ├── README.md                       # Оглавление материалов и ссылки на актуальные итоги
│   └── ablation_results_small/         # 2 PDF и 6 Markdown-файлов; оригиналы в research_results
├── docs/                               # Краткие итоги и журнал этапов Roadmap
│   ├── README.md                       # Индекс этапов и правило ведения документации
│   ├── 01_gpt_research.md               # GPT baseline/гибриды и сравнение с Mistral
│   ├── 02_feature_selector.md           # План данных и классификатора; ещё не реализован
│   └── project_structure.md            # Этот файл: дерево и назначение папок
├── research_results/                   # Все архивные и новые результаты исследований
│   ├── README.md                       # Формат хранения и пояснение переноса старых путей
│   ├── ablation_results_large/         # Большие одиночные абляции и baseline Mistral
│   │   ├── feat_baseline/              # Исходный большой baseline Mistral
│   │   ├── feat_N_bird/                # BIRD с одной включённой фичей
│   │   └── feat_N_ambrosia/            # Ambrosia с одной включённой фичей
│   ├── ablation_results_small/         # Малые абляции, поиски комбинаций, исторические отчёты
│   │   └── feat_best_hybrid_large/     # Важно: здесь лежит best hybrid НА БОЛЬШИХ наборах
│   ├── runs/                           # Новые воспроизводимые запуски и диагностики
│   │   ├── gpt54mini_baseline_large_20261016/
│   │   │   ├── bird/                   # GPT baseline: 99/241, 41,08%
│   │   │   ├── ambrosia/               # Неудачные попытки с полными 240; диагностика пустых БД
│   │   │   └── ambrosia_227/           # GPT baseline: 160/227, 70,48%, TP=0
│   │   ├── gpt54mini_hybrid_large_20261016/
│   │   │   ├── bird/                   # Исходный 14-фичевый гибрид: 93/241, 38,59%
│   │   │   └── ambrosia_227/           # Гибрид: 166/227, 73,13%, F1=57,93%
│   │   ├── gpt54mini_hybrid_bird_no32_33_50_20261023/
│   │   │   └── bird/                   # Диагностическая выборка 50: 22/50, не полный benchmark
│   │   ├── gpt54mini_hybrid_bird_no32_33_full_20261023/
│   │   │   └── bird/                   # Полный адаптированный гибрид: 113/241, 46,89%
│   │   ├── gpt54mini_main_prompt_ambrosia227_20261016/
│   │   │   └── ambrosia_227/           # Контроль с точным промптом main: 46,70%
│   │   ├── ambrosia_ambiguity_main_20261016/ # Те же 12: точный промпт main
│   │   ├── bird_small_openrouter_deepseek_20261016/ # Ранний smoke-test, 22 вопроса
│   │   └── feature_audit_20261023/      # Полный аудит 33 флагов и таблица больших абляций
│   ├── legacy_notes/                   # Прежние планы local/old; только исторические заметки
│   ├── legacy_snapshots/               # Прежние all_*_results.json и db_schemas.json из корня
│   └── organization_20261023/          # Контроль целостности и журнал этой уборки
├── data/                               # Исходные benchmark-вопросы и производные данные обучения
│   ├── bird_small.json                 # 22 вопроса, подмножество bird_large
│   ├── bird_large.json                 # 241 вопрос BIRD
│   ├── ambrosia_small.json             # 24 вопроса, подмножество ambrosia_large
│   ├── ambrosia_large.json             # 240 исходных вопросов
│   ├── ambrosia_large_available_227.json          # Явно зафиксированная доступная выборка
│   ├── ambrosia_large_available_227.manifest.json # ID, исключения и hashes
│   ├── feature_labels*.json            # Исторические labels; требуют аудита до обучения
│   ├── ambrosia_train.json             # Исходные train-данные Ambrosia
│   ├── train_queries.json              # Исходные примеры проекта
│   └── launch_db/                      # Инструкции и утилиты размещения БД
├── src/adv_text2sql/                    # Основная библиотека и MCP/Text2SQL реализация
│   └── mcp_servers/text2sql_tool/src/
│       ├── text2sql_implementation.py   # Генератор, извлечение схем, FEAT_N, обработка SQL
│       └── prompts.py                  # Шаблоны промптов
├── benchmarks/                         # BIRD/Ambrosia pipeline, оценщики и учёт токенов
├── scripts/                            # Поддерживаемые инструменты текущих исследований
│   ├── run_openrouter_baseline.py       # Baseline / hybrid / hybrid_no32_33; checkpoint и metadata
│   ├── feature_presets.py              # Фиксированные наборы флагов
│   ├── baseline_connection.py          # Таймауты PostgreSQL и повторы подготовки БД
│   ├── research_paths.py               # Новые пути и совместимость старых metadata/команд
│   ├── ambrosia_subset.py              # Формирование выборки 227
│   ├── bird_subset.py                  # Выборка 50 заблокированных вопросов и сравнение
│   ├── compare_hybrid_runs.py          # Сравнение новых запусков с baseline и Mistral
│   ├── audit_feature_results.py        # Аудит всех больших одиночных абляций без LLM
│   ├── build_feature_dataset.py        # Старый builder labels; требуется доработка перед обучением
│   ├── prompt_versions.py              # Чтение точного промпта из main без переключения ветки
│   ├── diagnose_ambrosia.py             # Read-only диагностика пустых серверных БД
│   ├── check_openrouter.py             # Короткая платная проверка API, только при явном запуске
│   └── watch_baseline.ps1              # Наблюдение за конкретным процессом и остановка при сбое
├── local/                              # Shell-запуски с SSH
│   ├── run_openrouter_baseline.sh       # Основной текущий запуск: туннель + Python runner
│   ├── start_tunnel.sh                 # Наблюдение/переподключение SSH
│   ├── run_single.sh                   # Рабочий исторический запуск произвольных FEAT через .env
│   ├── ablation_full.sh                # Исторические малые абляции
│   ├── ablation_large_bird.sh          # Исторические большие BIRD-абляции
│   ├── ablation_large_ambrosia.sh      # Исторические большие Ambrosia-абляции
│   └── run_baseline_large.sh           # Исторический baseline через .env
├── tests/                              # Офлайн-проверки наборов, путей, таймаутов, повторов
├── hints/                              # Старый cache FEAT_26; не смешивать train/test и модели
├── nl_descriptions/                    # Cache описаний FEAT_23; учитывать версию схемы/модели
├── Roadmap.md                          # Исходный архитектурный план динамического выбора фичей
├── practices_doc.md                    # Полный каталог практик; актуальные итоги — в docs
├── README.md                           # Исходная инструкция проекта из main, сохранена
├── CLAUDE.md                           # Существующая инструкция по работе с репозиторием
├── bird_benchmark.py                   # Исходная точка запуска из main, сохранена
├── ambrosia_benchmark.py               # Исходная точка запуска из main, сохранена
├── bird_evaluate_only.py               # Исходная переоценка; путь задаётся RESULTS_FILE
├── .env / .env.openrouter              # Локальные секреты и подключения; не публиковать
├── .env.example                        # Исходный пример настроек из main
├── pyproject.toml / uv.lock            # Окружение и зависимости из main
├── .gitattributes                      # LF для bash-скриптов
├── .gitignore                          # Существующие исключения Git
├── .claude/                            # Локальные настройки инструмента, сохранены
└── .venv/                              # Локальное окружение, не исходный код проекта
```

## Файлы внутри одного запуска

- `metadata.json` — статус, модель, флаги, hashes, покрытие, токены и снимки расхода.
- `query_results.json` — checkpoint с ответами по question_id.
- `report.json` — итоговые метрики; у BIRD — также результаты по вопросам.
- `raw_responses.jsonl` — полные запросы/ответы новых запусков, когда включено сохранение.
- `bird.log` / `ambrosia.log` — ход работы.
- `all_gold_results.json` / `all_predicted_results.json` — исполненные результаты BIRD.
- `comparison.json` / `comparison.md` / `ANALYSIS.md` — дополнительные сравнения конкретных запусков.

## Проверка и ручные запуски

Из корня проекта, **сначала dry-run** (нет API/БД):

```powershell
.venv\Scripts\python.exe scripts/run_openrouter_baseline.py --configuration hybrid --benchmark ambrosia --dry-run
.venv\Scripts\python.exe scripts/run_openrouter_baseline.py --configuration hybrid_no32_33 --benchmark bird --dry-run
```

Ручной запуск с SSH; платные вызовы начинаются только при выполнении команды:

```powershell
bash local/run_openrouter_baseline.sh --configuration hybrid --benchmark ambrosia
bash local/run_openrouter_baseline.sh --configuration hybrid_no32_33 --benchmark bird
```

Завершённый запуск с совпадающей конфигурацией пропускается. Новый эксперимент
записывайте в новую папку через `--output research_results/runs/имя_эксперимента`.
При одинаковой папке и другой конфигурации runner откажется смешивать ответы.
Старые пути вида `runs/...` принимаются Python runner как совместимые aliases,
но новые документы и команды используют `research_results/runs/...`.

Офлайн-сравнение полного BIRD:

```powershell
.venv\Scripts\python.exe scripts/compare_hybrid_runs.py --run-dir research_results/runs/gpt54mini_hybrid_bird_no32_33_full_20261023
```

Файлы, существующие в `main`, при уборке не редактировались и не удалялись.
Поэтому исходные README и точки запуска могут описывать старое расположение
выходных файлов. Современный путь запуска и каталог результатов описаны здесь.
