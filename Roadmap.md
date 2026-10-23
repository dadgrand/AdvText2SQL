> ?????????? ?????????? ??????: [docs/README.md](docs/README.md). GPT-???????????? ?????????????; ?????????? ?????? ? ????????????? ??? ???????. ???? ???????? ???????? ????????????? ????.

# Plan: Roadmap — Dynamic Feature Selection Pipeline (Multilabel Classifier)

## Контекст

Ablation v2 завершён (34 фичи, лучший набор: FEAT_2+3+4+8+12+17+18+19+27+29+30+32+33+35).
Текущий BIRD large: **44.81%** (re-eval). Следующая задача по проекту — перейти от
статического набора фич для всех запросов к **динамической селекции фич под каждый запрос**.

**Цель:** показать что pipeline (feature selector → SQL generator) превосходит
простой промпт + сильная baseline-модель (GPT-5.2-mini).

**Задача:** создать датасет (запрос - набор фич), обучить multilabel-classifier,
интегрировать в пайплайн, показать прирост. 
---

## Phase 0 — Baseline и датасет 

### 0.1 GPT-5.2-mini baseline

- Запустить `bird_benchmark.py` + `ambrosia_benchmark.py` с `LLM_MODEL_NAME=gpt-5.2-mini`
- Зафиксировать: BIRD large %, Ambrosia large %, token cost
- Это точка отсчёта: всё что выше = результат нашей системы промпт-инжиниринга

### 0.2 Per-question feature attribution dataset

**Источник:** существующие `research_results/ablation_results_large/feat_N_bird/bird_query_results.json` (34 ablation-прогона)

Для каждого question_id из bird_large (241 вопрос):
- собрать вектор `feature_helps[FEAT_N]` = 1 если этот вопрос правильно решился в прогоне с FEAT_N
- Label: `feature_mask` = бинарный вектор длиной 34

Примерная схема записи:
```json
{
  "question_id": 180,
  "question": "...",
  "db_id": "financial",
  "difficulty": "moderate",
  "evidence": "...",
  "feature_mask": {"FEAT_2": 1, "FEAT_3": 0, "FEAT_12": 1, ...}
}
```

**Потенциальная проблема:** ablation — leave-one-in, не комбинации. Для вопросов где ни одна фича не помогает
в одиночку — нужен дополнительный greedy-search прогон (Phase 1+).

---

## Phase 1 — Rule-based feature selector 

**Цель:** быстрый proof-of-concept без обучения; показать что динамический выбор фич > статический.

**Расширить существующий FEAT_29** (keyword-based complexity routing) до feature selector:

```python
# src/adv_text2sql/mcp_servers/text2sql_tool/src/feature_selector.py
class RuleBasedFeatureSelector:
    ALWAYS_ON = ["FEAT_8", "FEAT_17", "FEAT_18", "FEAT_19", "FEAT_27", "FEAT_35"]

    def select(self, question: str, db_schema: dict) -> List[str]:
        features = list(self.ALWAYS_ON)
        if has_numeric_aggregation(question):  features += ["FEAT_2", "FEAT_3", "FEAT_4"]
        if has_complex_joins(question):         features += ["FEAT_30"]   # CoT
        if has_ambiguity_markers(question):     features += ["FEAT_32", "FEAT_33"]
        if schema_is_large(db_schema):          features += ["FEAT_2", "FEAT_4"]
        return features
```

Интеграция: хук в `Text2SQLGenerator.query()` перед генерацией SQL — selector выбирает
фичи, они используются вместо глобальных env-переменных.

**Эксперимент:** rule-based selector vs best-static-combo vs GPT-5.2-mini на BIRD large.

---

## Phase 2 — Multilabel Classifier 

**Цель:** обученная модель предсказывает оптимальный набор фич под конкретный запрос.

**Архитектура:**
```
question + evidence + difficulty
        ↓
sentence-transformer (multilingual-e5-base или rubert-tiny)
        ↓  [frozen или LoRA rank=8]
Linear(hidden_dim → 34) + Sigmoid
        ↓
feature_probs [0.0–1.0] × 34  →  threshold/top-k → {FEAT_i: bool}
```

**Обучение:**
- Датасет: `data/feature_labels.json` из Phase 0 (241 вопрос → augment с bird_small)
- Loss: `BCEWithLogitsLoss` (multilabel binary cross-entropy)
- Метрики: subset accuracy, Hamming loss, downstream BIRD accuracy
- Split: 80% train / 20% val; stratify по difficulty
- SFT (опционально): LoRA rank=8 на encoder для domain adaptation

---

## Phase 3 — DB-specific feature scoring 

**Цель:** скорить фичи не только по запросу, но и по характеристикам конкретной БД.

- При `build()`: собирать статистики по БД (кол-во таблиц, типы колонок, cardinality, FK/PK)
- Создать `data/db_feature_relevance.json`: `{ db_id: { FEAT_2: 0.9, FEAT_6: 0.1, ... } }`
- Итоговый selector: `score = query_score × db_score`
- Разметка: per-db ablation (подмножество вопросов × фичи → per-db accuracy)

---

## Phase 4 — AutoML + продуктовый вид

- ToDo
---

## Текущая точка отсчёта

| Метрика | Значение | Дата |
|---------|---------|------|
| BIRD large (best static combo, re-eval) | **44.81%** (108/241) | 27 мая |
| BIRD small (best static combo) | **54.55%** (12/22) | 27 мая |
| Ambrosia small | **95.83%** (23/24) | 27 мая |
| GPT-5.2-mini baseline |  Phase 0 | — |

---

## Ближайшие шаги

1. Новый LLM-прогон large с generator-фиксами и зафиксировать обновлённый BIRD, если будет прирост
2. GPT-5.2-mini baseline (Phase 0.1)
3. `scripts/build_feature_dataset.py` (Phase 0.2)

---

Критерий успеха: dynamic pipeline > best-static-combo > GPT-5.2-mini на BIRD large.
