# BikeFlow Intelligence

Проверяемая история поездок Citi Bike: полный официальный NYC ZIP → immutable
Bronze → Silver Parquet и quarantine → почасовой Gold → PostgreSQL → FastAPI →
Next.js Dashboard. Реальный апрель 2026: **3 860 371** допустимая опубликованная
поездка из четырёх CSV. [Агрегированные доказательства](docs/evidence/f1-202604.json)
содержат checksum, reconciliation и измерения; сырые поездки в Git не включены.
Состояние этапов — только в [ROADMAP.md, §6](ROADMAP.md#6-журнал-реализации).

## Требования и установка

- CPython 3.12.13, uv 0.12.1; зависимости закреплены в `uv.lock`.
- Node.js 24.18.0, npm 11.16.0; зависимости web закреплены в npm lockfile.
- Docker с Linux containers и Compose с поддержкой `--wait`.
- Сеть для первой установки и загрузки архива, свободные порты 5432/8000/3000.
- Нужен диск под ZIP, распакованный CSV, DuckDB spill и Parquet. Движок ограничен
  512 MB и двумя потоками; это не измерение peak RSS процесса.

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

## Источник и границы

[Citi Bike System Data](https://citibikenyc.com/system-data) и
[Data Sharing Policy](https://citibikenyc.com/data-sharing-policy). Независимый
проект без аффилиации с Citi Bike. Исходные данные предоставляются без гарантии
полноты; запрещено пытаться идентифицировать пользователей. В Git — код и
агрегированные доказательства, без исходных ZIP/Parquet и секретов.

F1 проверен на одном современном месяце; historical schema inventory, обновление
объекта у источника, расширенная EDA/quality и incremental preprocessing — F2.
Карты, GBFS snapshots, ML, рекомендации и production SLA здесь не реализованы.
Требования: [PROJECT_SPEC.md](PROJECT_SPEC.md), архитектура:
[ARCHITECTURE.md](ARCHITECTURE.md), проверки: [OPERATIONS.md](OPERATIONS.md).
