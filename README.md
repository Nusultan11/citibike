# BikeFlow Intelligence

Проверяемая история поездок Citi Bike: полный официальный NYC ZIP → immutable
Bronze → Silver Parquet и quarantine → почасовой Gold → PostgreSQL → FastAPI →
Next.js Dashboard. Реальный апрель 2026: **3 860 371** допустимая опубликованная
поездка из четырёх CSV. [Агрегированные доказательства](docs/evidence/f1-202604.json)
содержат checksum, reconciliation и измерения; сырые поездки в Git не включены.
Исторический Silver F2 охватывает **2024-10 — 2026-09**: 104 CSV, **92 076 567**
канонических строк из **92 085 994** входных. [EDA](docs/evidence/f2-analysis.md) и
[машинное evidence](docs/evidence/f2-history.json) содержат полный source inventory,
версионирование, dispositions, GBFS и измеренные ресурсы.
Состояние этапов — только в [ROADMAP.md, §6](ROADMAP.md#6-журнал-реализации).

## Требования и установка

- CPython 3.12.13, uv 0.12.1; зависимости закреплены в `uv.lock`.
- Node.js 24.18.0, npm 11.16.0; зависимости web закреплены в npm lockfile.
- Docker с Linux containers и Compose с поддержкой `--wait`.
- Сеть для первой установки и загрузки архива, свободные порты 5432/8000/3000.
- Нужен диск под ZIP, распакованный CSV, DuckDB spill и Parquet. F1 использует
  лимит DuckDB 512 MB; F2 — 2048 MB для transform, 2 GB для EDA/publication,
  два потока на соединение. Эти лимиты не являются peak RSS процесса.
  Полный F2 — длительный batch: только ZIP выбранных 24 месяцев занимают 17.39 GB,
  дополнительно нужны Silver, независимые identity partitions и рабочий диск.

Из корня репозитория:

```sh
uv python install
uv sync --locked
npm --prefix apps/web ci
```

Создайте `.env` из `.env.example`, не перезаписывая существующий файл. Заполните
`PGPASSWORD` случайным локальным секретом. Например, PowerShell:

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    $databaseSecret = uv run --locked python -c "import secrets; print(secrets.token_hex(24))"
    (Get-Content .env -Raw).Replace('PGPASSWORD=', "PGPASSWORD=$databaseSecret") |
        Set-Content .env -Encoding utf8
}
```

| Переменная | Значение примера | Назначение |
|---|---|---|
| `PGHOST`, `PGPORT` | `127.0.0.1`, `5432` | PostgreSQL для локальных pipeline/API |
| `PGDATABASE`, `PGUSER` | `bikeflow`, `bikeflow` | База и пользователь serving |
| `PGPASSWORD` | заполнить локально | Секрет, не включать в Git |
| `BIKEFLOW_API_HOST`, `BIKEFLOW_API_PORT` | `127.0.0.1`, `8000` | Локальный API |
| `BIKEFLOW_API_LOG_LEVEL` | `info` | Уровень логирования API |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Серверный запрос Next.js к API |
| `PORT` | `3000` | Локальный web port |

Compose переопределяет адрес БД на `db:5432`, API — на `http://api:8000`.
Порты публикуются только на localhost; секрет не передаётся браузеру. Переменные
процесса имеют приоритет над `.env`. Не выводите полный `docker compose config`
в публичные логи; для проверки конфигурации используйте `--quiet`.

## Запуск и загрузка реального архива

```sh
docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 120
uv run --locked python -m citibike.pipeline --period 2026-04 --url https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip
uv run --locked python tests/integration/smoke.py --expected-count 3860371
```

До первой успешной публикации Dashboard показывает `empty`, без выдуманных
значений. Pipeline запускается на хосте с тем же locked Python-окружением:
это отдельная batch-команда, обработка ZIP не выполняется внутри HTTP-запросов.
`--data-dir` меняет корень локальных артефактов (по умолчанию `data/`);
`--process-only` останавливается после проверки Parquet, без записи в БД.

Адреса: [Dashboard](http://127.0.0.1:3000), [API health](http://127.0.0.1:8000/health),
[OpenAPI](http://127.0.0.1:8000/openapi.json), [Swagger UI](http://127.0.0.1:8000/docs).
Для запуска API/web вне Docker оставьте `docker compose up -d --wait db`,
остановите контейнеры `api web` и используйте в разных терминалах:

```sh
uv run --locked python -m apps.api
npm --prefix apps/web run dev
```

Production web: `npm --prefix apps/web run build`, затем
`npm --prefix apps/web run start`. Docker API/web работают без root и используют
закреплённые digest базовых образов. PostgreSQL хранится в persistent volume.

## Контракты данных и API

- Выбор источника — один завершённый официальный NYC месяц; JC вне охвата.
  Современная схема проверяется до обработки; неизвестная схема блокирует запуск.
- Bronze: `data/bronze/NYC/<source_sha256>/source.zip` и `manifest.json` с URL,
  received time, размером, checksum, ZIP inventory и всеми CSV. Повторная команда
  проверяет checksum и использует тот же объект без новой загрузки.
- Silver сохраняет исходные поля, типизированные local/UTC timestamps, station ID
  как строки, provenance и quality flags. Каждая CSV-запись имеет disposition
  в `row_ledger.parquet`. Карантин сохраняет причины и исходные значения.
- Канонизация: только полностью одинаковые записи одного `ride_id`, первый
  `(source_file, source_row_number)`. Все варианты конфликтующего ID изолируются.
  Missing ID, неправильное/неоднозначное/несуществующее NY-время, отрицательная
  длительность и окончание после получения архива исключают поездку из метрик.
- Неизвестные станции не исключают допустимую поездку из общего числа; теряется
  только соответствующий station event. Короткая/длинная длительность отмечается,
  без произвольного cutoff. Некорректные координаты становятся null с флагом.
- Gold: отдельные departures по старту и arrivals по окончанию, UTC hour grain,
  `net_trip_flow_s = arrivals_s - departures_s`. Общий счётчик — канонические
  допустимые поездки с началом внутри `[start,end)`. Пропуски не означают нулевой спрос.
- Перед публикацией независимый stdlib CSV/ZoneInfo/SQLite baseline проходит
  по всем исходным CSV и сверяет результат. PostgreSQL получает только Gold.
  Миграция с checksum, COPY, проверки сумм и смена активной версии — одна транзакция.
  API читает опубликованную версию в read-only session; клиент закрепляет её ID.

Read-only endpoints:

| Endpoint | Ответ |
|---|---|
| `GET /v1/publication` | Активная публикация, источник, период, версии, качество и coverage |
| `GET /v1/kpi` | `completed_published_rides`, применённые фильтры и metadata |
| `GET /v1/timeseries` | UTC hourly points, фильтры и metadata |

KPI/timeseries принимают `start_date`, `end_date` (обе даты или ни одной,
календарь America/New_York, конец не включён), `scope=NYC`, `station_id`,
`publication_id`. По умолчанию весь месяц активной публикации. Даты вне покрытия,
неверный интервал/параметры — 422. `station_id` фильтрует старты; timeseries также
показывает отдельные arrivals/net flow этой станции. Неизвестная станция даёт
`empty`, не утверждение нулевого спроса. Нет публикации/наблюдений — 200 `empty`
и KPI null; недоступна БД — 503; ошибки имеют `{"error":{"code":"...","message":"..."}}`.
`/health` у API и web проверяет liveness своего сервиса, а не наличие данных.

## Независимая сверка и восстановление

После pipeline выполните полную сверку raw CSV → Silver → Gold → PostgreSQL → API
→ видимое число HTML (имя каталога — возвращённый pipeline publication ID):

```sh
uv run --locked python -m citibike.verify --result data/processed/69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284 --output docs/evidence/f1-202604.json --recompute-baseline
```

Без `--recompute-baseline` используется уже сохранённый независимый baseline для
этого checksum; остальные звенья проверяются заново. Baseline использует отдельные
правила преобразования времени через fold/roundtrip, stdlib CSV и disk SQLite,
не импортирует helpers Gold/quality. Evidence содержит только агрегаты и hashes.

Повторный pipeline идемпотентен. Неполный/повреждённый ZIP, schema error, all-invalid
или независимая рассверка блокируют публикацию, предыдущая версия остаётся активной.
All-invalid сохраняет quarantine в `data/failed/`. После ошибки исправьте её причину
и повторите исходную команду. Для возврата к ранее проверенному объекту повторите
pipeline с его period/URL: сохранённая версия активируется атомарно.
Не редактируйте опубликованные файлы и применённые SQL-миграции вручную.

Проверки отказа и восстановления (при загруженном апреле 2026):

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

`docker compose down` останавливает сервисы с сохранением volume. Не удаляйте
volume для обычного восстановления; backup/restore production относится к F8.
При нестандартных портах smoke принимает `--api-url` и `--web-url`.

## Проверки и CI

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q tests/unit
uv run --locked pytest -q tests/data_contracts
uv run --locked pytest -q tests/integration/test_serving.py
uv build
npm --prefix apps/web ci
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
git diff --check
```

Integration требует доступный PostgreSQL и право CREATE DATABASE: тесты создают
и удаляют только уникальную `bikeflow_test_<uuid>`, не меняя опубликованную базу.
Unit fixtures малы, искусственные и явно обозначены. Starlette TestClient выдаёт
upstream deprecation warning об httpx; он не скрыт. Wheel/sdist создаются в `dist/`.

[CI](.github/workflows/ci.yml) выполняет Python/contracts/frontend на PR и main;
на main также Docker, отдельную тестовую БД, fixture pipeline, empty/filter/error,
отказ и recovery API/БД. Полный архив на каждом PR не загружается. Наличие workflow
не доказывает успех CI: реальные run/SHA записываются в журнал.

## F2: исторический слой и GBFS

Официальный ListObjects inventory на 2026-10-28 содержит 178 объектов. Последние
24 опубликованных полных NYC месяца — **2024-10 — 2026-09**; JC исключён.
Inventory перечисляет всю опубликованную историю, но скачиваются только выбранные
24 месяца. Для каждого ZIP проверяются pinned ETag/размер, SHA-256, CRC и все CSV;
отсутствующий объект блокирует публикацию. `listed` ещё не означает `verified`.

Команды из корня, PowerShell (первый discovery возвращает путь inventory):

```powershell
$inventory = uv run --locked python -m citibike.history inventory
uv run --locked python -m citibike.history ingest --inventory $inventory --ranged
uv run --locked python -m citibike.history build --inventory $inventory
uv run --locked python -m citibike.history verify --inventory $inventory
uv run --locked python -m citibike.history profile --inventory $inventory
uv run --locked python -m citibike.ingestion.gbfs --count 3
```

Проверенная inventory исходного прогона:
`data/inventory/807194651725833df9977f9cee4a0635a0f9c37fdad4201d9e61e7143227be1f/inventory.json`.
Повторный discovery 2026-10-28 16:17 UTC подтвердил тот же scope и те же objects/ETags.

F1 Dashboard сохраняет свой source-only результат апреля **3 860 371**. В глобальной
F2 партиции апрельского архива accepted **3 860 342**: 29 строк конфликтуют с
майским источником и находятся в quarantine. В мае также исключены 483 exact
duplicates апрельских записей. Обе версии имеют отдельный provenance; F2 не меняет
активную F1 публикацию. Повторная raw-to-UI регрессия:

```sh
uv run --locked python -m citibike.verify --result data/processed/69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284 --output docs/evidence/f2-f1-regression.json
```

`--ranged` использует до четырёх resumable HTTP Range запросов к тому же
официальному URL, с проверкой каждого блока и итогового ZIP. Без флага используется
последовательный GET. `--period YYYY-MM` позволяет повторить только неудавшийся
месяц из выбранного окна. После восстановления выполните полный `ingest` ещё раз:
неизменные проверенные объекты переиспользуются. `build --wait-seconds 14400`
может ожидать receipts при параллельном ingest; обычный build не ожидает.

Повтор `build` проверяет hashes и не перерабатывает неизменные source/pipeline
версии. Чтобы обнаружить переиздание у источника, выполните новый `inventory`,
затем те же `ingest/build/verify` с новым путём. Изменённый ZIP получает отдельную
immutable Bronze version. Пересчитываются его Silver и глобальные ID-конфликты;
зависимости включают старые и новые месяцы начала/окончания поездок. Неизменные
эффективные партиции переиспользуются. Контролируемый тест изменения источника
лежит в [fixture evidence](docs/evidence/f2-incremental-fixture.json) и явно
отделён от реальной истории.

Локальные артефакты (в Git не включаются):

- `data/inventory/<id>/`: исходные XML listing и manifest; `objects/`, `receipts/`
  содержат результаты фактической проверки.
- `data/bronze/NYC/<sha256>/`: неизменяемый ZIP и source manifest.
- `data/f2/processed/<id>/`: versioned базовые Silver, ledger и quarantine.
  Mapping определяется реальными CSV headers; неизвестная схема блокирует месяц.
- `data/f2/auxiliary/`, `audits/`, `states/`, `failures/`: полные EDA-профили,
  независимый CSV/ZoneInfo/SQLite baseline, row-decision hashes, состояние и ошибки.
- `data/f2/silver/<id>/`: итоговые canonical Silver, полный ledger и quarantine
  с source/schema/pipeline lineage. Базовая версия `f2.0.0`, итоговая публикация
  `f2.0.0-silver.2`; исправление итогового слоя не требует повторного raw transform.
- `data/f2/snapshots/<id>/manifest.json`, `data/f2/active.json`: проверенный набор
  всех 24 партиций и атомарно заменяемый указатель. Ошибка до активации сохраняет
  предыдущую публикацию. F1 PostgreSQL/API/UI продолжают обслуживать апрель 2026.
- `data/gbfs/bronze/<collection_id>/`: оригинальные discovery/information/status
  JSON, received/source timestamps, TTL и checksums. Нормализация `f2-gbfs-2`
  находится в `data/gbfs/silver/f2-gbfs-2/`; history — в `data/gbfs/station_history/`.

Каждая входная CSV-строка имеет ровно один disposition. Независимый аудит проходит
весь raw и сравнивает per-CSV counts, метрики и hash упорядоченных row decisions;
межмесячные решения дополнительно сверяются на независимо вычисленных
fingerprints. Редкие пики и неизвестные категории сохраняются с flags. Критические
нарушения schema/grain/count/checksum/reconciliation блокируют публикацию;
допустимые пропуски и неполное station coverage дают warning.

GBFS URLs берутся динамически из официального discovery. Реально получена версия
2.3; mapping 3.0 проверяется fixtures. Poll ограничен 1–120 collections и двумя
часами, учитывает station-status TTL, timeout и backoff. Счётчик `0` сохраняется;
неизвестное значение, отсутствие снимка и stale не превращаются в ноль. Для
положительного TTL применяется консервативное истечение по TTL, для TTL=0
требуется refresh и действует предел freshness 300 секунд. Stale не означает
нарушение провайдером пяти минут GBFS: наш положительный TTL gate строже.

`dim_station_history` использует только наблюдённые свежие information feeds,
интервалы `[valid_from,valid_to)` без перекрытий и обратного заполнения. Станции
GBFS и trip station aliases различаются; неоднозначные aliases не соединяются.
Длительные промежутки между collections остаются неизвестными. Weather и
историческая train/serve availability отсутствуют; ML не создаётся.

`verify` проверяет уже опубликованный snapshot без изменения active pointer;
`profile` формирует те же агрегированные отчёты. Полные профили остаются локально,
компактные результаты — `docs/evidence/f2-history.json` и `f2-analysis.md`.
Время и OS peak RSS записываются отдельно для worker attempts и publication;
cache/retry не выдаются за холодный запуск. Лимит DuckDB не является измерением RSS.
CI дополнительно проходит CLI-протокол на 24 маленьких синтетических месяцах:
первый build, unchanged repeat, verification и one-changed-source rebuild.

## F3: историческая аналитика

Вход — проверенный активный F2 Silver snapshot; повторная загрузка архивов не нужна.
Матрица population, grains, формул, coverage и ограничений:
[analytics-contract](docs/analytics-contract.md). Нормативные формулы — METRICS.md.

```powershell
uv run --locked python -m citibike.analytics build
uv run --locked python -m citibike.analytics publish
docker compose build
docker compose up -d --wait --wait-timeout 120
uv run --locked python -m citibike.analytics verify
uv run --locked python -m citibike.analytics benchmark --output docs/evidence/f3-performance.json
```

`build --data-dir <root> --snapshot <F2 snapshot directory>` поддерживает явный
версионированный вход. `publish --gold <Gold publication directory>` публикует
проверенную версию. `activate --publication-id <previous published ID>` возвращает
serving на уже опубликованную версию одним transaction; предыдущие физические
partitions сохраняются. Gold active указывает последний успешный build, PostgreSQL
active — выбранную serving-версию; фактическую версию сообщает API. F1 указатель
не меняется. При сбое staging не становится доступен через active publication.

Локально: `data/f3/contributions/` — additive source aggregates; `months/` —
event-month Parquet с checksums, grain gates и независимой сверкой каждой группы;
`publications/` — immutable lineage/membership; `runs/` — результаты повторов.
Изменение исходной partition пересчитывает зависимые старые/новые event months,
включая начала и окончания по разные стороны границ. Workers выполняются
последовательно с DuckDB 2 GB / 2 threads; измеренный OS RSS сохраняется отдельно.

Страница [исторической аналитики](http://localhost:3000/analytics) использует
`/v2/analytics/{publication,kpi,timeseries,stations,routes,categories,geography,peaks,compare}`.
Параметры: `publication_id`, NYC `start_date`/`end_date` (конец исключён),
`station_id`, `vehicle`, `membership`, `grain=hour|day|month`, `limit=1..100`,
`offset=0..10000`; `to_station` применим только к routes, `event_type=start|end` —
к geography. `compare_start`/`compare_end` — только compare: одинаковые local days,
UTC hours, weekday mix, полное observed-hour coverage и ненулевой baseline.
Часовые series и география ограничены 31 днём (география также допускает station
filter). Маршруты за весь опубликованный период используют exact serving cube;
один календарный месяц — monthly aggregates. Другие city-wide route ranges
ограничены 31 днём; более длинные требуют `station_id` или `to_station`.
Пустые страницы сохраняют total_groups, сортировка стабильна.
Все запросы читают PostgreSQL aggregates, закрепляют одну publication и передают
provenance/quality/coverage. OpenAPI: [API docs](http://localhost:8000/docs).

Исторические координаты берутся из события поездки; versioned WGS84 grid 0.01°
не является районом или равновеликой сеткой. District остаётся unknown. GBFS,
впервые наблюдённый 2026-10-28, не заполняет исторические координаты.
`/v2/analytics/availability` имеет отдельные timezone-aware `start`/`end` и
GBFS UUID `station_id`; trip category filters отклоняются. Valid intervals
ограничены положительными TTL, следующей collection и максимумом 60 секунд,
без интерполяции gaps. Empty/full ratios требуют ≥95% покрытия, двух разных
source updates и полного refresh cycle. Неполные/stale наблюдения дают N/A
с причиной. Средняя доступность считается на наблюдённую operating station,
а не как полный network total. Scope GBFS — собранный operator feed; юрисдикции
станций независимо не размечены. Он отделён от NYC trip population.

Полная проверка F3 при работающем PostgreSQL:

```powershell
uv run --locked pytest -q tests/unit tests/data_contracts tests/integration/test_serving.py tests/integration/test_analytics.py
```

Integration создаёт и удаляет только свои уникальные disposable БД.
CI выполняет build/unchanged/changed-source протокол на 24 controlled месяцах и
PostgreSQL/HTTP/HTML smoke; fixtures не служат доказательством реальных метрик.
`verify` сверяет реальный Silver → Gold → HTTP → HTML; `benchmark` дополнительно
собирает 30 последовательных HTTP samples на workload, p50/p95/p99 и SQL plans.
Первый запрос новой сессии не выдаётся за холодный OS cache. Цель p95 ≤300 ms
проверяется измерениями, без заявления SLA. Raw данные, Parquet и `.env` в Git
не включаются; компактные результаты находятся в `docs/evidence/`.

Результаты реального F3: [validation и performance](docs/evidence/f3-performance.md),
[Silver/API/HTML](docs/evidence/f3-analytics.json),
[30-sample benchmark](docs/evidence/f3-performance.json),
[browser QA](docs/evidence/f3-ui.json). В default NYC окне — 92076054 поездки;
513 accepted starts до окна учитываются отдельно. Полный PostgreSQL с retained
partitions и индексами занимает около 94.9 GB; Gold Parquet — 1.87 GB, route cube —
0.106 GB. Измеренный p95 месячного route query с одним category filter — 2413.46ms;
цель 300ms для него не достигнута. Cold isolated OS cache — NOT VERIFIED.

## F4 — Forecasting

Два отдельных прогноза **наблюдаемых опубликованных** departures/arrivals,
24 следующих полных UTC часа, календарь America/New_York. Это не весь спрос,
не inventory и не подтверждённая operational accuracy. Полный контракт и catalog:
[forecasting-contract](docs/forecasting-contract.md).

ML libraries установлены отдельной dependency group; API image их не включает:

```powershell
uv sync --locked --group ml
uv run --locked --group ml python -m citibike.forecasting build --stations 64
uv run --locked --group ml python -m citibike.forecasting weather
uv run --locked --group ml python -m citibike.forecasting backtest
uv run --locked --group ml python -m citibike.forecasting select
uv run --locked --group ml python -m citibike.forecasting backtest --final
uv run --locked --group ml python -m citibike.forecasting train
uv run --locked --group ml python -m citibike.forecasting predict
uv run --locked --group ml python -m citibike.forecasting publish
docker compose build api
docker compose up -d --wait --wait-timeout 120
uv run --locked --group ml python -m citibike.forecasting verify --serving
```

Вход по умолчанию — принятая локальная F3 publication. CLI допускает явные
`--gold`, `--dataset`, `--experiment`, `--selection`, `--model`, `--forecast`,
`--data-dir`. `predict --origin` повторяет конкретный сохранённый origin;
`--station` можно повторить для явного cohort. Без origin используется текущее
время. Будущий origin и модель/данные, ещё не доступные в origin, отклоняются.
Одинаковые model/dataset/origin/cohort/code versions дают одну publication.
Rollback: `activate --publication-id` ранее опубликованной forecast version.

Артефакты и predictions — immutable `data/f4/`, вне Git. Сохраняются checksums,
source/Gold/dataset/feature/station/model versions, manifests, predictions/residuals,
folds, параметры, seed 42, timing и OS RSS. Validation выполняется до select;
final test locked к selection и не может использоваться для нового tuning.
Poisson, LightGBM, CatBoost сравниваются с delayed Seasonal Naive на одинаковых
eligible rows. Train-only encodings/scalers, labels доступны до fold fit_asof.
Default + один bounded вариант на алгоритм; 2 threads, <=80000 train rows/тип/fold.
Real experiment ограничен 64 stations; coverage сообщается, не экстраполируется.

Историческая первая дата публикации архивов неизвестна. Backtest — **approximate
retrospective**: гипотеза source month end +45 суток с учётом всех зависимых
source months, включая следующий архив на границах. Текущий canonical F2 snapshot
не является восстановленной исторической версией. Strict historical replay до
фактического создания этих immutable inputs недоступен. Production inference
проверяет реальные source/dataset/model availability timestamps; short lags,
future weather, historical GBFS и текущая station capacity не используются.

`weather` получает ERA5 только как **evaluation outcome labels** после prediction:
cold/heat/heavy-rain segments. Это reanalysis городского grid, не as-of forecast
и не station measurement. Raw response хранится локально; `--weather-json` позволяет
повторить сохранённый ответ. Weather никогда не входит в model feature matrix.
Осадки агрегируются как preceding-hour sums; температура — по timestamp.
Исправление границы дня в auxiliary weather diagnostics описано вместе с
[измерениями F4](docs/evidence/f4-forecasting.md); сохранённые predictions,
параметры и frozen selection не изменены.

При недостаточной истории — unavailable; нули и отсутствующие observations
различаются. Source window старше 62 суток блокирует корректный batch; неудачный
batch не заменяет последнюю корректную publication. Prediction intervals не
заявлены. MLflow Registry automation остаётся F7; F4 использует versioned local
champion manifests. Inference не запускается из HTTP.

Read-only [forecasts API](http://localhost:8000/v3/forecasts): `publication_id`,
`station_id`, `prediction_type`, `horizon_h=1..24`, `limit=1..200`, `offset=0..10000`.
Ответ содержит provenance, timestamps, quality/freshness и status; F1/F3 IDs
не принимаются как forecast versions. Полноценные прогнозные web views — F6.

Проверки ML и serving:

```powershell
uv run --locked --group ml pytest -q tests/unit tests/data_contracts tests/ml tests/integration/test_serving.py tests/integration/test_analytics.py tests/integration/test_forecasting.py
```

CI использует controlled fixtures и smoke budgets без real-data retraining.
Цель daily batch <=5 минут требует фактического замера, не является SLA.

## Источник и границы

[Citi Bike System Data](https://citibikenyc.com/system-data) и
[Data Sharing Policy](https://citibikenyc.com/data-sharing-policy). Независимый
проект без аффилиации с Citi Bike. Исходные данные предоставляются без гарантии
полноты; запрещено пытаться идентифицировать пользователей. В Git — код и
агрегированные доказательства, без исходных ZIP/Parquet и секретов.

F1 проверен на апреле 2026; F2 расширяет локальный исторический Silver и добавляет
реальные GBFS collections. Фактический охват и результаты зафиксированы в журнале
и evidence полного прогона. `/` сохраняет F1; `/analytics` показывает F3.
Карт F6, ML, рекомендаций и заявлений о production SLA нет.
Требования: [PROJECT_SPEC.md](PROJECT_SPEC.md), архитектура:
[ARCHITECTURE.md](ARCHITECTURE.md), проверки: [OPERATIONS.md](OPERATIONS.md).
