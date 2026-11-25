# Индекс доказательств к презентации (t45)

Одна строка — одно утверждение: блок, утверждение, число, знаменатель, режим декодирования, файл и поле/строка. Числа результатов читаются из `cluster/results/main_table.json`; файл пересобирается одной командой, вручную не редактируется:

```
python cluster/build_main_table.py        # если изменились числа A/B/C
python cluster/build_presentation_index.py  # пересобрать этот индекс
```

Колонка **Статус** означает: `ok` — цитируемая строка найдена в файле; `UNVERIFIED` — файл есть, но строка не найдена (тогда утверждение нельзя показывать, пока не исправлено).

| Блок | Утверждение | Число | Знаменатель | Декодирование | Файл | Поле / строка | Статус |
|---|---|---|---|---|---|---|---|
| 1. Зачем вопрос | Прежние OOD-числа получены на корпусе, где были строки отложенных баз | 1 098 | 6 866 строк sft_all.jsonl | не применимо (данные) | `cluster/provenance_audit.json` | runs[0].contaminated_rows.ood_reserved | ok |
| 1. Зачем вопрос | Подвыборка OOD-120 контаминирована полностью | 120/120 | 120 вопросов | не применимо (данные) | `cluster/provenance_audit.json` | ood120.verdict | ok |
| 1. Зачем вопрос | Историческая перезапись SQLite→PostgreSQL оставляет 32 эталона из 241 неисполнимыми | 32/241 | 241 эталон | не применимо (данные) | `cluster/eval_reports/dryrun_14b-ev/report.md` | строка 20 и 158 | ok |
| 1. Зачем вопрос | Та же перезапись ломает 26 из 39 изменённых dev-эталонов | 26/39 | 39 изменённых эталонов | не применимо (данные) | `cluster/eval_reports/dryrun_14b-ev/report.md` | строка 85 | ok |
| 1. Зачем вопрос | На OOD-876 та же перезапись не ломает ни одного из 28 изменённых эталонов | 0 из 28 | 28 изменённых эталонов | не применимо (данные) | `cluster/eval_reports/dryrun_ood-scale7b-v2/report.md` | строка 95 | ok |
| 1. Зачем вопрос | В прежних прогонах менялись и размер модели, и данные: у 7B-2ep и 14B-1ep один и тот же контаминированный корпус | 1 098 строк OOD в каждом | 6 866 строк корпуса | не применимо (данные) | `cluster/provenance_audit.json` | runs[*].contaminated_rows | ok |
| 2. Как исследовали | Сплит по базам с отпечатками схем: 80 записей / 79 семейств | 80 записей / 79 семейств | 80 db-записей | не применимо (данные) | `datasets/splits/schema_family_split.json` | families | ok |
| 2. Как исследовали | Деконтаминация вопросов: 0 нарушений при Jaccard 0,8, максимум 0,375 | 0 нарушений | 11 273 train × 1 260 eval вопросов | не применимо (данные) | `datasets/splits/splits_report.json` | question_decontamination | ok |
| 2. Как исследовали | Лоадер отфильтровал 1 018 строк и оставил 6 907 (4 216 real + 2 691 synthetic) | 1 018 / 6 907 | 7 925 исходных строк | не применимо (данные) | `datasets/native_pg/loader_report.json` | rows_filtered / rows_kept | ok |
| 2. Как исследовали | Паритет train/serve: 612 токен-идов совпадают, снимок sha256 402d4689… | 612 | 612 токенов промпта | не применимо (данные) | `cluster/batch_report.json` | renderer_parity.token_id_snapshot | ok |
| 2. Как исследовали | Статистика батча: контекст p50/p95/p99 = 557/2217/2235, обрезано 408 ответов (5,91 %), потеряно 16 465 токенов | 408 / 16 465 | 6 907 строк, max_len 2 048 | не применимо (данные) | `cluster/batch_report.json` | batch_statistics.truncation | ok |
| 2. Как исследовали | Численная корректность: 10 проверок из 10 пройдены | 10/10 | 10 проверок | не применимо (данные) | `cluster/runs/numerics_report.json` | summary | ok |
| 2. Как исследовали | Метрика: EX на исполнимом эталоне, парный bootstrap 10 000, точный McNemar | знаменатели 216 / 811 / 209 | 216 из 216; 811 из 876; 209 из 241 | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/eval_shards.py` | функции paired_bootstrap_ci / mcnemar_exact_p | ok |
| 3. Что сработало | LoRA на чистом сплите поднимает OOD-876 | 574 → 638; +7,89 п.п. [+5,06; +10,85], p = 1,17e-07 | 811 исполнимых эталонов | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/results/main_table.json` | pairs.'B_mixed vs A'.ood876 | ok |
| 3. Что сработало | На dev-216 выигрыш на границе значимости | 87 → 101; +6,48 п.п. [+0,00; +12,96], p = 0,05946 | 216 вопросов | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/results/main_table.json` | pairs.'B_mixed vs A'.dev216 | ok |
| 3. Что сработало | Real-only адаптер (B_real) даёт больше, чем смешанный | 649/811 = 80,02 %; +9,25 п.п. [+6,41; +12,21], p = 5,79e-10 | 811 исполнимых эталонов | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/results/main_table.json` | arms.B_real.ood876 + pairs.'B_real vs A'.ood876 | ok |
| 3. Что сработало | Синтетика в текущем пуле преимущества не даёт (при 39,4 % и при 13,77 %) | B_real−B_mixed +1,36 п.п. [−0,74; +3,33], p = 0,2284; C−B_real +0,74 п.п. [−1,11; +2,59], p = 0,5118 | 811 исполнимых эталонов | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/results/main_table.json` | pairs + cluster/runs/B_mixed_vs_B_real_comparison.json | ok |
| 3. Что сработало | Пофайловый леджер: 105 исправлений и 41 регрессия на OOD-876 | 105 / 41 | 811 парных вопросов | batch 1, без паддинга, greedy, 256 токенов, стоп <|im_end|>, без repair | `cluster/eval_reports/paired_b1/summary.json` | ood876.fixes / regressions | ok |
| 3. Что сработало | Восстановлены вторые формулировки синтетики | +1 179 пар, 7 925 строк | 6 866 строк прежнего корпуса | не применимо (данные) | `cluster/corpus_report.json` | old_rule_counterfactual.dropped.total | ok |
| 3. Что сработало | Инференсный селектор схем покрывает длинные строки | 395/408 (96,81 %); 311/408 с бюджетом | 408 длинных строк | не применимо (данные) | `datasets/native_pg/selector_report.json` | coverage.rows_with_all_gold_columns + gate_for_arms | ok |
| 4. Что не сработало | Schema linking: два варианта не отличаются от базового | p = 1,0 (Δ +0,83 п.п.) и p = 0,5488 (Δ +2,50 п.п.) | 120 вопросов | исторический прогон (не batch 1) | `repo/runs/r3-schema-link/analysis.json` | paired.D_highlight_vs_A_off / paired.E_joins_vs_A_off | ok |
| 4. Что не сработало | Грамматический декодер даёт контракт, но не качество | 241/241 → 0/241 (fences), 236/241 → 0/241 (;); качество p = 0,180, Δ +2,07 п.п. [−0,41; +4,56] | 241 запрос | исторический прогон (не batch 1) | `repo/docs/REPORT.md` | строки 900–925 и 978–988 | ok |
| 4. Что не сработало | Селектор с утечкой gold (t31) — 408/408 строк выбирались по эталону; заменён инференсным | 408/408 (утечка) → 395/408 (покрытие) | 408 длинных строк | не применимо (данные) | `datasets/native_pg/selector_report.json` | retired_gold_aware_policy | ok |
| 4. Что не сработало | Форма батча batch-8 — артефакт: меняет 22/18 dev и 46/40 OOD предсказаний, переводит dev через 0,05 | dev B−A +7,87 п.п. (p = 0,0186) против +6,48 (p = 0,0595) | 216 / 811 | batch 8 с левым паддингом (историческое) | `cluster/eval_reports/padding_sensitivity.json` | conclusion.changed_predictions | ok |
| 4. Что не сработало | Односторонний гейт литералов пропускал вопросы с числом, которого нет в SQL | 17/200 пар (8 из них судья считал match) | 200 пар | не применимо (аудит данных) | `cluster/judge/report.json` | mechanical_cross_checks.question_literal_audit | ok |

## Что не является результатом

| Что | Значение | Почему нельзя | Файл |
|---|---|---|---|
| 7B scale adapter на OOD-876 | 620/811 = 76,45 % | старый корпус содержал 1 098 OOD-строк; это не 14B | `cluster/results/contaminated_numbers.json` |
| Наш 14B, ночной прогон | 95/120 = 79,17 % | тот же контаминированный корпус | `cluster/results/contaminated_numbers.json` |
| Исторический 14B QLoRA | 88/120 = 73,33 % | тот же контаминированный корпус | `cluster/results/contaminated_numbers.json` |
| Дельты batch-8 | dev +7,87 / OOD +7,52 п.п. | не serving-контракт | `cluster/results/contaminated_numbers.json` |
| Вклад синтетики | измерен: B_real−B_mixed +1,36 п.п., p = 0,228 | уже не pending: B_real есть; D всё ещё pending | `cluster/results/main_table.json` |
| Арм D | pending | t30 не запущен, записи прогона нет | `cluster/results/main_table.json` |
| PG-hard | закрыт | не оценивался, по вопросам не смотрели | `datasets/pg_hard/manifest.json` |
| Пилот t12 | предварительно | отчёта пилота нет | `datasets/pilot/README.md` |

## Pending (помечены явно, не опущены)

- **D** — новый синтетический пул: t30 не запущен, числа нет (`cluster/results/main_table.json`, `arms.D.pending_reason`).
- **PG-hard** — закрытый набор, финальная оценка после заморозки рецепта (`datasets/pg_hard/manifest.json`).
- **Пилот t12** — покрытие/приёмка в работе (`datasets/pilot/README.md`).
- **14B-подтверждение** рецепта — не запущено (`TASK_PLAN.md`).

