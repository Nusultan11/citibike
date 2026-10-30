# BikeFlow Intelligence

Независимая аналитическая и прогнозно-рекомендательная платформа на публичных
данных Citi Bike. История поездок проходит ZIP → immutable Bronze → Silver
Parquet и quarantine → Gold → PostgreSQL → FastAPI → Next.js Dashboard.
Факты, прогнозы и будущие сценарные рекомендации имеют разные источники и статусы.
Опубликованные поездки не измеряют скрытый спрос; симуляция не доказывает экономию.

## Реализовано и подтверждено

| Этап | Обнаруженная реализация (`IMPLEMENTED`) | Доказательства прежних проверок |
|---|---|---|
| F0 | Python-пакет, API/web, Compose, locked dependencies, CI | Команды, SHA и actual CI в [журнале](ROADMAP.md#6-журнал-реализации) |
| F1 | Полный апрель 2026, raw → UI, независимый oracle, atomic publication | **3 860 371** поездка; [evidence](docs/evidence/f1-202604.json), [регрессия F4](docs/evidence/f4-f1-regression.json) |
| F2 | Inventory, 24 NYC месяца 2024-10…2026-09, 104 CSV, canonical Silver, GBFS collections | **92 076 567** canonical из **92 085 994** входных; [EDA](docs/evidence/f2-analysis.md), [evidence](docs/evidence/f2-history.json) |
| F3 | Gold, фильтры, временная/географическая аналитика, маршруты, GBFS intervals, `/analytics` | Default start-window **92 076 054**; [контракт](docs/analytics-contract.md), [сверка](docs/evidence/f3-analytics.json), [benchmark](docs/evidence/f3-performance.md), [UI](docs/evidence/f3-ui.json) |
| F4 | Departures/arrivals на 24 часа, Seasonal Naive/Poisson/LightGBM/CatBoost, backtest, batch/API | Cohort 64 станции; [контракт и catalog](docs/forecasting-contract.md), [эксперимент](docs/evidence/f4-forecasting.md), [evidence](docs/evidence/f4-forecasting.json) |
| F5–F8 | `PLANNED`: risk/optimization, полные product views, Prefect/MLflow automation, production hardening | Не реализованы; критерии и зависимости — [ROADMAP §§1–5](ROADMAP.md#1-стратегия-реализации) |

Журнал сохраняет исторические `COMPLETED` F0–F4 по прежним критериям. Это не
подтверждение новых требований ZIP и не независимая приёмка F4 пользователем.
`VERIFIED` означает фактически пройденную указанную проверку, `NOT VERIFIED` —
недостаток доказательств; старые evidence не объявляются повторно проверенными.
Текущие проверки документации и расхождения записаны в новой записи §6.

## Стек и компоненты

Фактически используются Python 3.12.13, uv 0.12.1, DuckDB/Parquet, httpx,
PostgreSQL/psycopg, FastAPI/Pydantic, Next.js/TypeScript, ECharts, Docker Compose
и GitHub Actions. ML libraries устанавливаются отдельной dependency group и
не входят в API image. Структура: `src/citibike/` — pipeline, analytics, features,
forecasting и serving; `apps/api/`, `apps/web/`, `tests/`, `docs/evidence/`.
Polars/PyArrow, MapLibre, Prefect, MLflow Registry, Optuna и OR-Tools из целевой
[архитектуры](ARCHITECTURE.md) не следует считать внедрёнными по наличию в плане.

## Установка и локальный запуск

Нужны CPython/uv указанных версий, Node.js 24.18.0, npm 11.16.0, Docker с Linux
containers и Compose с `--wait`; свободные порты 5432/8000/3000. Lockfiles —
`uv.lock`, `apps/web/package-lock.json`. Первая установка и загрузка требуют сети.
Только ZIP 24 месяцев исходного F2 занимают 17.39 GB; дополнительно нужны CSV,
Parquet, spill и PostgreSQL (измеренный F3 с retained partitions — около 94.9 GB).

Из корня репозитория:

```sh
uv python install
uv sync --locked --group ml
npm --prefix apps/web ci
```

Создайте `.env` из `.env.example`, сохранив существующий файл. Пример PowerShell
генерирует локальный секрет без вывода его значения:

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    $databaseSecret = uv run --locked python -c "import secrets; print(secrets.token_hex(24))"
    (Get-Content .env -Raw).Replace('PGPASSWORD=', "PGPASSWORD=$databaseSecret") |
        Set-Content .env -Encoding utf8
}
```

`PGHOST/PGPORT` задают localhost:5432, `PGDATABASE/PGUSER` — bikeflow,
`PGPASSWORD` нужно заполнить. `BIKEFLOW_API_HOST/BIKEFLOW_API_PORT` — localhost:8000,
`BIKEFLOW_API_LOG_LEVEL` — info, `API_BASE_URL` — http://127.0.0.1:8000,
`PORT` — 3000. Переменные процесса имеют приоритет над `.env`. Compose использует
`db:5432` и `http://api:8000`, публикует порты на localhost; секрет не попадает
в браузер. Проверяйте конфигурацию через `--quiet`, без раскрытия env в логах.

```sh
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 120
uv run --locked python -m citibike.pipeline --period 2026-04 --url https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip
uv run --locked python tests/integration/smoke.py --expected-count 3860371
```

[Dashboard](http://127.0.0.1:3000), [аналитика](http://127.0.0.1:3000/analytics),
[API health](http://127.0.0.1:8000/health), [OpenAPI](http://127.0.0.1:8000/openapi.json),
[Swagger](http://127.0.0.1:8000/docs). До публикации UI показывает `empty`.
Pipeline — отдельный batch на хосте, не HTTP-операция; `--data-dir` меняет
локальный корень `data/`, `--process-only` проверяет Parquet без записи в БД.

Для API/web вне Docker оставьте `docker compose up -d --wait db`, остановите
`docker compose stop api web`, затем в разных терминалах:

```sh
uv run --locked python -m apps.api
npm --prefix apps/web run dev
```

Production web: `npm --prefix apps/web run build`, затем
`npm --prefix apps/web run start`. Контейнеры API/web работают без root;
PostgreSQL хранится в persistent volume.

## Реальные данные и воспроизводимость

Команды ниже воспроизводят ранее выполненные протоколы; длительные загрузки,
EDA и backtest не нужны для проверки одной правки документации. Результаты
прежних запусков и ограничения находятся в evidence и журнале.

### F1: независимая сверка и восстановление

```sh
uv run --locked python -m citibike.verify --result data/processed/69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284 --output docs/evidence/f1-202604.json --recompute-baseline
```

Путь `--result` — publication ID, возвращённый pipeline. Проверяются raw CSV →
Silver → Gold → PostgreSQL → API → HTML; baseline отдельно использует stdlib
CSV/ZoneInfo/SQLite. Без `--recompute-baseline` сохраняется прежний checksum-bound
raw baseline, остальные звенья проверяются заново.

Повторный pipeline идемпотентен. Corrupt/incomplete ZIP, unknown schema,
all-invalid или рассверка блокируют публикацию; all-invalid сохраняет quarantine
в `data/failed/`. Исправьте причину и повторите команду; прежние period/URL
атомарно возвращают сохранённую версию. Не редактируйте immutable artifacts
и применённые миграции.

Проверки отказа при опубликованном апреле (после каждой остановки — recovery):

```sh
docker compose stop api
uv run --locked python tests/integration/smoke.py --expect-api unavailable --expect-data unavailable
docker compose up -d --wait --wait-timeout 120
uv run --locked python tests/integration/smoke.py --expected-count 3860371
docker compose stop db
uv run --locked python tests/integration/smoke.py --expect-data unavailable
docker compose up -d --wait --wait-timeout 120
uv run --locked python tests/integration/smoke.py --expected-count 3860371
```

`/health` — liveness, не проверка наличия данных. Smoke принимает `--api-url` и
`--web-url`. `docker compose down` сохраняет volume; не удаляйте его для обычного
восстановления. Проверяемые production backup/restore — по [OPERATIONS §6](OPERATIONS.md#6-защита-и-восстановление).

### F2: история и GBFS

PowerShell; `inventory` возвращает путь нового manifest, фиксирующего scope:

```powershell
$inventory = uv run --locked python -m citibike.history inventory
uv run --locked python -m citibike.history ingest --inventory $inventory --ranged
uv run --locked python -m citibike.history build --inventory $inventory
uv run --locked python -m citibike.history verify --inventory $inventory
uv run --locked python -m citibike.history profile --inventory $inventory
uv run --locked python -m citibike.ingestion.gbfs --count 3
```

Исходная inventory:
`data/inventory/807194651725833df9977f9cee4a0635a0f9c37fdad4201d9e61e7143227be1f/inventory.json`.
Listing 2026-10-28 содержал 178 объектов, выбранное окно — 2024-10…2026-09, JC
исключён. Новый discovery может изменить окно; `listed` не означает `verified`.
Проверяются ETag/size, SHA256, CRC и все CSV; неизвестная схема блокирует месяц.

`--ranged` — до четырёх resumable HTTP Range запросов, без флага — обычный GET.
`ingest --period YYYY-MM` повторяет выбранный месяц; после recovery повторите
полный ingest. `build --wait-seconds 14400` может ждать receipts, обычный build
не ждёт. Повторный build проверяет hashes и переиспользует неизменные partitions.
Переиздание обнаруживается новым inventory и теми же ingest/build/verify;
пересчитываются изменённые Silver, глобальные ID-конфликты и зависимые периоды.
[Incremental evidence](docs/evidence/f2-incremental-fixture.json) относится к fixtures.

F1 source-only апрель — 3 860 371; F2 canonical апрель — 3 860 342 из-за 29
конфликтующих с маем строк; в мае исключены 483 exact duplicates. F3 default
считает start events в окне inventory, поэтому отличается от всех accepted F2
на 513 стартов до окна. Это разные populations с отдельным provenance.

Локальные артефакты, вне Git:

- `data/bronze/NYC/<sha256>/` — ZIP/manifest; `data/processed/<id>/` — F1 output.
- `data/inventory/<id>/` — listing, objects/receipts и manifest.
- `data/f2/processed/`, `silver/`, `snapshots/`, `active.json` — base/effective
  Silver, ledger, quarantine и atomic snapshot; версии f2.0.0 / f2.0.0-silver.2.
- `data/f2/auxiliary/`, `audits/`, `states/`, `failures/` — профили, независимые
  row-decision hashes, состояние и ошибки. Каждой CSV-строке назначен disposition.
- `data/gbfs/bronze/<collection_id>/` — raw discovery/info/status, TTL и timestamps;
  `data/gbfs/silver/f2-gbfs-2/`, `station_history/` — normalized collections/history.

История GBFS начинается с собственного сбора; текущие stations не заполняют
прошлые поездки. F2 weather отсутствовал; F4 ERA5 — отдельные evaluation labels.
`verify` не меняет active pointer; `profile` обновляет агрегированные отчёты.
F1/F2/F3 используют DuckDB limits 512 MB / 2048 MB / 2 GB и 2 threads; это
настройки engine, а не измеренный process RSS. Cold/concurrent limits — в evidence.

### F3: аналитика

При наличии активного проверенного F2 snapshot:

```sh
uv run --locked python -m citibike.analytics build
uv run --locked python -m citibike.analytics publish
uv run --locked python -m citibike.analytics verify
uv run --locked python -m citibike.analytics benchmark --output docs/evidence/f3-performance.json
```

Явные входы: `build --data-dir <root> --snapshot <F2 snapshot directory>`,
`publish --gold <Gold publication directory>`; rollback —
`activate --publication-id <previous published ID>`. Gold active и PostgreSQL
active могут различаться: фактическую serving version сообщает API. F1 не меняется.
`data/f3/{contributions,months,publications,runs}/` содержат lineage/checksums
и проверки зависимых event months; неизменные partitions переиспользуются.
Фильтры, ограничения диапазонов, grain, UTC/DST, null/zero и GBFS quality gates —
в [analytics-contract](docs/analytics-contract.md); не дублируются здесь.

### F4: прогнозы

При наличии локальной проверенной F3 publication и ML group:

```sh
uv run --locked --group ml python -m citibike.forecasting build --stations 64
uv run --locked --group ml python -m citibike.forecasting weather
uv run --locked --group ml python -m citibike.forecasting backtest
uv run --locked --group ml python -m citibike.forecasting select
uv run --locked --group ml python -m citibike.forecasting backtest --final
uv run --locked --group ml python -m citibike.forecasting train
uv run --locked --group ml python -m citibike.forecasting predict
uv run --locked --group ml python -m citibike.forecasting publish
uv run --locked --group ml python -m citibike.forecasting verify --serving
```

Это протокол прежнего F4, а не разрешение переиспользовать раскрытый final test
для новых решений. Новые выборы требуют нового независимого периода по ML_SPEC.
CLI: `--gold/--dataset/--experiment/--selection/--model/--forecast/--data-dir`;
`predict --origin` фиксирует origin, повторяемый `--station` — cohort.
Будущие/ещё недоступные inputs отвергаются; одинаковые версии/origin дают один ID.
Rollback — `activate --publication-id` ранее опубликованной forecast version.
Immutable models, predictions/residuals и manifests находятся в `data/f4/`.
`weather --weather-json <path>` переиспользует raw response; ERA5 используется
только после prediction для diagnostics, не как as-of feature.

Оба текущих fallback — Seasonal Naive. Backtest **approximate retrospective**:
исторические publication timestamps неизвестны, +45 суток — гипотеза. Cohort64
покрывает около 2.56% known-ID events. Strict current inference проверяет реальные
input timestamps, но не доказывает operational accuracy. Недостаточная история
даёт `unavailable`; source window старше 62 суток блокирует batch. Неудачный batch
не заменяет корректную публикацию. Prediction intervals не реализованы;
MLflow Registry остаётся F7. Детали — [forecasting-contract](docs/forecasting-contract.md).

## API и пользовательские страницы

| Контур | Существующая выдача |
|---|---|
| F1: `/` | `/v1/publication`, `/v1/kpi`, `/v1/timeseries`; фильтры NYC dates, station, publication |
| F3: `/analytics` | `/v2/analytics/{publication,kpi,timeseries,stations,routes,categories,geography,peaks,compare}`; отдельно `/v2/analytics/availability` |
| F4: batch/API | `/v3/forecasts`; `publication_id`, `station_id`, `prediction_type`, `horizon_h=1..24`, `limit=1..200`, `offset=0..10000` |

Read-only API закрепляет publication и возвращает provenance/quality/freshness.
Даты F1/F3 — полуоткрытое окно America/New_York; параметры проверяет OpenAPI.
Нет публикации — `empty`, не нулевой спрос; недоступная БД — 503; неверный диапазон
— 422. F1/F3 IDs не являются forecast IDs. Карта, полноценные прогнозные web views
и рекомендации в UI относятся к F6.

## F5 — сценарный риск и перевозки

**PARTIAL:** CLI, simulator, threshold heuristic и bounded SciPy MILP проверены
на controlled integration. Два официальных GBFS collections оказались stale
при собственном TTL: реальные входы отклонены, risk=null, real E2E NOT VERIFIED.
[Evidence и ограничения](docs/evidence/f5-risk.md), [машинный результат](docs/evidence/f5-risk.json).

```sh
# Воспроизводимый controlled F4→disposable PostgreSQL→GBFS→F5 + independent oracle:
uv run --locked --group ml python -m tests.integration.test_risk

# Повторный расчёт сохранённого результата независимым ledger, без serving writes:
uv run --locked --group ml python -m citibike.risk verify --result data/f5/results/3403a0eb33544546f5892476900bfb62e4bf06cf074c79cd07c554c0b9ce90b0
```

`python -m citibike.risk run` требует явный timezone-aware `--as-of` и принимает
`--forecast`, `--gbfs`, `--config`, `--stations`, `--data-dir`. Forecast должен
совпадать с фактической публикацией PostgreSQL, быть опубликован к as-of и
покрывать полные будущие часы. GBFS проверяется по original JSON/Parquet,
received/source timestamps, positive TTL и уникальному short_name mapping.
Нет совместимых inputs — immutable unavailable result и exit2, предыдущая
валидная версия сохраняется. Пропущенные станции имеют null risk с причиной.
Results находятся в ignored `data/f5/results`; `active.json` допустим только для
real_official, fixtures используют отдельный `active_controlled.json`.

Config задаёт hours/repeats/seed, demand scales и event orders, число/ёмкость
транспорта, скорость, handling time и относительную стоимость. Три политики
получают одинаковые сохранённые события. Poisson attempts, exogenous returns,
постоянные operating flags и blocked docks — сценарные допущения. Initial GBFS
TTL не подтверждает будущую наблюдаемость. Оптимизатор решает inventory-target
surrogate, не полный routing и не гарантирует минимальные simulated failures.
Wilson intervals характеризуют Monte Carlo precision, не калибровку реальных
отказов. Operational promotion F4 NOT VERIFIED; Seasonal Naive/unavailable
fallback сохранён. Новых зависимостей, API, migrations и F5 web UI нет.

## Проверки и CI

Быстрые проверки и полный локальный suite (PostgreSQL нужен для integration):

```sh
uv sync --locked --group ml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked --group ml pytest -q tests/unit tests/data_contracts tests/ml tests/integration --basetemp .cache/dt
uv build
npm --prefix apps/web ci
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
git diff --check
```

Integration создаёт/удаляет только уникальные disposable `bikeflow_test_<uuid>`;
нужно право CREATE DATABASE. Для проверки без PostgreSQL исключите
`tests/integration`. Fixtures искусственные и явно обозначены. Известный
StarletteDeprecationWarning не скрыт. Wheel/sdist создаются в ignored `dist/`.

[CI](.github/workflows/ci.yml): PR/main — lint/format/types, unit/contracts/ML,
controlled F2/F3 CLI и frontend; main — Docker, disposable PostgreSQL,
forecast/analytics smoke и F1 outage/recovery. Полная история и real backtest
не запускаются на каждом PR. Успех определяется actual run/SHA из журнала,
не наличием YAML. Нагрузочные production gates определяет OPERATIONS.

## Расхождения с обновлённой спецификацией

Нормативная основа — UPDATED ZIP SHA256
`1b228e3c9400d9cfd6c2ed5c340e55b0d7def4cbe962a26cf109345cb281cbbc`.
Ниже сопоставление с кодом и сохранёнными evidence, без новой приёмки F0–F4.

| Требование | Фактическое состояние и последствие |
|---|---|
| Исследовательский EDA / ML-readiness, DATA_CONTRACTS §5 | `VERIFIED` на 24 реальных месяцах: семь протоколов гипотез, zero/missing/rare/peak station-hours, churn/сезонность/география/TV shifts, lineage и coverage cohort64; [расчёты и ограничения](docs/evidence/f2-analysis.md). Первая историческая публикация архивов UNKNOWN; operational readiness не доказана. Model error slices/ablation относятся к корректирующей приёмке F4 |
| Feature-group ablation, ML_SPEC §§3–5 | `VERIFIED`: полный и три ablation variants каждого bounded algorithm на одинаковых validation rows; [новый validation-only разбор](docs/evidence/f4-forecasting.md). Группы косвенно кодируют друг друга; это не причинная importance и не citywide качество |
| Bounded search и все trials, ML_SPEC §4 | `VERIFIED`: immutable protocol до fits; 138/138 real outcomes (default/bounded, ablation, stress, repeats), worker timeout/RSS gates, durable failures на controlled negative tests. Frozen legacy evaluation сохранён; новые исследования запускаются отдельным `research --protocol`, без нового Optuna/MLflow |
| Operational champion, ML_SPEC §5 | `NOT VERIFIED`: forward/shadow evaluation отсутствует. Local champion manifests выбирают baseline, это fallback без доказанного operational uplift; strict current input gate не заменяет forward evaluation |
| MLflow lifecycle, ML_SPEC §7 | `PLANNED` для F7: local manifests не MLflow Registry; требование не отменено |
| Risk/Optimization, ML_SPEC §6 | `IMPLEMENTED/VERIFIED` на controlled scenarios; real-input unavailable gates проверены, real F5 E2E `NOT VERIFIED`, фаза PARTIAL. [F5 evidence](docs/evidence/f5-risk.md) |
| Product/performance, PROJECT_SPEC / OPERATIONS | `PLANNED`: F6–F8 и часть целевого стека. Исторический F3 p95 monthly route с одним category filter **2413.46 ms** превышает цель 300 ms; изолированный cold cache, concurrent SLA, production restore `NOT VERIFIED` |

F2/F3/F4 приняты пользователем в задании F5 с явно сохранённым F3 performance debt
и NOT VERIFIED operational promotion F4. Эти ограничения передаются в F5;
frozen selection/final-test/serving не переизбираются.

## Требования и продолжение разработки

[PROJECT_SPEC](PROJECT_SPEC.md) — продукт; [ARCHITECTURE](ARCHITECTURE.md) —
компоненты; [DATA_CONTRACTS](DATA_CONTRACTS.md) — данные;
[METRICS](METRICS.md) — определения; [ML_SPEC](ML_SPEC.md) — ML;
[OPERATIONS](OPERATIONS.md) — проверки/восстановление;
[ROADMAP §§1–5](ROADMAP.md#1-стратегия-реализации) — план,
[§6](ROADMAP.md#6-журнал-реализации) — единственная история работ.
[knowledge_base](knowledge_base/) — справочные материалы;
`docs/*-contract.md` описывают текущую реализацию и не отменяют нормативные требования.

Процесс по [AGENTS.md](AGENTS.md) и [bikeflow-step](.agents/skills/bikeflow-step/SKILL.md):
**один промпт → один полный этап F0–F8 → реализация → проверки → отчёт → независимая
приёмка**. Внутренние задачи Codex выполняет самостоятельно. Корректирующие промпты
допустимы при PARTIAL/BLOCKED или дефектах приёмки. Ближайшая работа — закрыть
F5 real E2E при совместимых свежих GBFS/forecast inputs, затем независимая приёмка
F5. F6–F8 автоматически не начинать.

[Citi Bike System Data](https://citibikenyc.com/system-data),
[Data Sharing Policy](https://citibikenyc.com/data-sharing-policy).
Проект не аффилирован с оператором. Не пытайтесь идентифицировать пользователей.
В Git — код и агрегированные evidence; raw datasets, models и секреты остаются локально.
