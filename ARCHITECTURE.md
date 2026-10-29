# ARCHITECTURE — устройство системы и технологии

**Назначение:** единственное место для схемы компонентов и инженерных решений. Названия полей — [DATA_CONTRACTS.md](DATA_CONTRACTS.md); значения метрик — [METRICS.md](METRICS.md); запуск и эксплуатация — [OPERATIONS.md](OPERATIONS.md).

## 1. Логическая схема

```text
Public sources: Trip ZIP/CSV | GBFS JSON | Weather | Calendar
             |                     |
     Archive ingest           Scheduled snapshots
             \                    /
              -> BRONZE (immutable raw, metadata)
                         |
            Profiling / quality / quarantine
                         |
                  SILVER (Parquet)
                         |
          Verified transformations / Gold
                  /                 \
       ANALYTICS GOLD        ML FEATURE DATASETS
            |                       |
    Serving aggregates       Train -> Validate -> Registry
            |                       |
            |                Batch departures/arrivals
            |                       |
            |             Risk simulation -> Optimizer
            \                       /
                 SERVING TABLES
                  PostgreSQL
                       |
                  FastAPI REST
                       |
         Next.js dashboard + map + charts

Cross-cutting: Prefect, logging, tests, CI/CD, lineage, backups.
```

**Два независимых контура:** фоновые batch/periodic задачи готовят результаты; web/API читают опубликованные serving-таблицы. Пользовательский запрос не должен запускать обучение или перечитывать миллионы поездок.

## 2. Компоненты и ответственность

| Компонент | Ответственность | Выход |
|---|---|---|
| Ingestion | Периодически находить изменения и надёжно доставлять оригиналы | Immutable raw + manifest |
| Quality & Processing | Исследовать и нормализовать данные; отметить исключения | Silver Parquet, quarantine, quality report |
| Analytics | Рассчитать согласованные факты и показатели | Gold aggregates |
| Feature builder | Построить point-in-time признаки и снимки | Версионированные ML datasets |
| Training & Registry | Эксперименты, оценка, хранение кандидатов/champion | Версия модели + метрики |
| Batch inference | Рассчитать будущие отправления/прибытия | Прогнозы с временем и версией |
| Risk & Optimizer | Смоделировать риск/операционные альтернативы | Сценарные предупреждения/рекомендации |
| Serving API | Авторизованные фильтры, версии, статусы, выдача | Предсказуемые REST-ответы |
| Web | Навигация, визуализация, drill-down, явная свежесть | Интерфейс пользователя |

## 3. Хранилища и вычисления

- **Bronze:** оригинальные ZIP/CSV и снимки JSON без смысловой перезаписи; checksum, source URL, UTC received_at, version. Старт — локальный диск; облако — S3-совместимое при необходимости.
- **Silver:** Parquet, единые типы, ключи и признаки качества; подозрительные записи не исчезают бесследно.
- **Gold:** факты «станция × час», маршруты/дни, доступность, витрины; Parquet как расчётное хранилище.
- **PostgreSQL:** метаданные загрузок/заданий, согласованные serving-таблицы. Не хранилище огромных ZIP.
- **Model artifacts:** отдельное версионированное хранилище; MLflow Registry отражает lineage и champion.
- **Время:** исходные временные метки сохранять; аналитические метки нормализовать в UTC, местные календарные представления — `America/New_York` с DST.

**Обновление:** инкрементально по изменённым файлам/периодам, с отслеживанием зависимостей. Новые витрины публикуются атомарно, после проверки. Не допускаются частично пересчитанные данные на одной странице.

## 4. Принятый стек

| Зона | Стартовый выбор | Комментарий |
|---|---|---|
| Язык/зависимости | Python, uv, Ruff | Единый пакет, закреплённые версии |
| HTTP | httpx | Таймауты, retry с backoff, потоковая загрузка |
| Data | Polars, DuckDB, PyArrow, Parquet | Сначала benchmark на данных, затем оптимизация |
| Оркестрация | Prefect | Отдельные потоки архива, GBFS, обновления, inference |
| Metadata / serving | PostgreSQL | Индексы по фактическим паттернам запросов |
| ML | scikit-learn, LightGBM, CatBoost; N-HiTS кандидат | Не все модели обязаны оказаться в production |
| Experiments | MLflow; Optuna по необходимости | Run, dataset fingerprint, bounded tuning, registry |
| Оптимизация | OR-Tools / SciPy | Проверка ограничений и симуляция |
| Backend | FastAPI, Pydantic | REST, OpenAPI, валидация |
| Frontend | Next.js, TypeScript | Разделение страниц, типизированный клиент |
| Графики/карта | Apache ECharts, MapLibre GL JS | Агрегаты и оптимизация слоя точек |
| Фоновые/локальные сервисы | Docker Compose | Не подразумевает HA |
| CI/CD | GitHub Actions | Unit, integration, build, deploy gates |

**Отложены:** Kafka, Spark, Kubernetes, Redis, отдельный feature-store сервис и микросервисы. Их внедрение требует измеренного узкого места, ADR и сравнения затрат.

## 5. Структура mono-repo

```text
apps/api/            # маршруты, схемы, DI, доступ к serving
apps/web/            # страницы, UI, charts, map, API client
src/citibike/ingestion|quality|processing|analytics|features/
src/citibike/forecasting|risk|optimization|serving/
orchestration/       # Prefect flows
configs/             # настройки без секретов
tests/unit|integration|data_contracts|ml|e2e/
notebooks/           # только исследование; логика в src/
docs/adr/           # архитектурные решения
infra/               # Docker/деплой
.github/workflows/   # CI
pyproject.toml, uv.lock, .env.example, README.md
```

**Правило:** отдельный модуль — не отдельный сервис. Первоначально это модульный монолит с отдельными воркерами.

## 6. Архитектурные стоп-сигналы

- Нельзя подключать браузер напрямую к Bronze/Silver или обучать модель в API-request.
- Нельзя рассчитывать risk score на устаревшем GBFS без явного статуса stale.
- Нельзя изменять контракты данных без миграции downstream и tests.
- Нельзя смешивать факты, прогнозы и результаты симуляции в один показатель без маркировки.

Рекомендации и диагностика архитектуры: [knowledge_base/02_DATA_ENGINEERING.md](knowledge_base/02_DATA_ENGINEERING.md), [knowledge_base/05_BACKEND_DASHBOARD.md](knowledge_base/05_BACKEND_DASHBOARD.md).
