# BikeFlow Intelligence

Основа аналитической платформы городского велопроката: пакет `citibike`, FastAPI,
Next.js, Docker Compose и минимальный CI. Бизнес-данные пока не загружены.
Состояние этапа и ограничения проверок — в [ROADMAP.md, §6](ROADMAP.md#6-журнал-реализации).

## Требования

- CPython 3.12.13 (`.python-version`), uv 0.12.1.
- Node.js 24.18.0, npm 11.16.0 для локального web.
- Docker с Linux containers и Compose с поддержкой `--wait` для контейнерного запуска.
- Доступ к PyPI, npm и registries образов при первой установке.

## Установка и конфигурация

Из корня репозитория:

```sh
uv python install
uv sync --locked
npm --prefix apps/web ci
```

Настройки необязательны при использовании значений по умолчанию. Для изменения
создайте локальный `.env` из `.env.example` (PowerShell):

```powershell
Copy-Item .env.example .env
```

Не перезаписывайте существующий `.env`; секреты не должны попадать в Git.
API читает корневой `.env` через Pydantic Settings, команды web `dev`/`start` —
через Node.js. Переменные процесса имеют приоритет над файлом.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `BIKEFLOW_API_HOST` | `127.0.0.1` | Bind host локального API |
| `BIKEFLOW_API_PORT` | `8000` | Локальный API port; опубликованный API port Compose |
| `BIKEFLOW_API_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical` |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Адрес API для серверного запроса Next.js |
| `PORT` | `3000` | Локальный web port; опубликованный web port Compose |

В Compose API слушает `0.0.0.0:8000`, web — `0.0.0.0:3000`, а `API_BASE_URL`
переопределяется на `http://api:8000` во внутренней сети. Наружу оба сервиса
публикуются только на `127.0.0.1`. Browser не обращается к Docker DNS-имени `api`.

## Локальный запуск

В первом терминале, из корня:

```sh
uv run --locked python -m apps.api
```

Во втором терминале:

```sh
npm --prefix apps/web run dev
```

Для production-режима web вместо `dev`:

```sh
npm --prefix apps/web run build
npm --prefix apps/web run start
```

Адреса по умолчанию: web — http://127.0.0.1:3000,
API health — http://127.0.0.1:8000/health,
OpenAPI — http://127.0.0.1:8000/openapi.json,
Swagger UI — http://127.0.0.1:8000/docs.
API `/health` и web `/health` возвращают `{"status":"ok"}` и проверяют каждый свой сервис.
API не зависит от PostgreSQL, ingestion или ML.

Главная страница проверяет API при каждом открытии без cache, с таймаутом 2 секунды.
Ошибка сети, HTTP-ошибка или некорректный JSON приводят к состоянию «API недоступен»;
сама web-страница остаётся доступной. Кнопка «Проверить снова» делает новый запрос.
HTTP-ошибки API имеют JSON-формат `{"error":{"code":"...","message":"..."}}`;
внутренние исключения и входные данные в ответ не включаются.

## Docker Compose

Одна команда из корня (освободите локальные порты API/web):

```sh
docker compose up --build -d --wait --wait-timeout 120
```

Используются закреплённые digest базовых образов, `uv sync --locked --no-dev`
и `npm ci`. Runtime-контейнеры работают без root. Web стартует после healthy API,
но его собственный health не зависит от доступности API.

Проверки живых сервисов и сценария отказа:

```sh
docker compose config --quiet
uv run --locked python tests/integration/smoke.py
docker compose stop api
uv run --locked python tests/integration/smoke.py --expect-api unavailable
docker compose up -d --wait --wait-timeout 120
uv run --locked python tests/integration/smoke.py
docker compose down
```

Тот же smoke-скрипт работает с локальными процессами; для негативного сценария
остановите API, оставив web запущенным. При нестандартных портах передайте
`--api-url http://127.0.0.1:8123 --web-url http://127.0.0.1:3100`.

## Качество и сборка

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q tests/unit
uv build
npm --prefix apps/web ci
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
git diff --check
```

Ruff проверяет Python, не меняя исходные Markdown-документы. Biome проверяет
lint и формат web. mypy проверяет пакет, API и тесты. Unit-тесты покрывают импорт,
metadata, health/OpenAPI, HTTP/validation/internal errors и неверную конфигурацию.
Wheel и sdist создаются в игнорируемом `dist/`; окружения и кэши в Git не включаются.
Версии зависимостей фиксируются в `uv.lock` и `apps/web/package-lock.json`;
обновлять lockfiles следует намеренно через `uv lock` и npm.

[Foundation CI](.github/workflows/ci.yml) выполняет Python/frontend checks на PR
и push в main. На main дополнительно собирает оба контейнера, проверяет HTTP
интеграцию и отказ API. Статус реального запуска CI проверяется отдельно от наличия workflow.

## Границы F0/F1

F0 предоставляет только запускаемую и проверяемую основу. Аналитических метрик,
архивов поездок, карты, прогнозов и рекомендаций нет. Первый реальный архив,
Bronze/Silver/Gold, serving DB и source-to-UI reconciliation относятся к F1.

Требования — [PROJECT_SPEC.md](PROJECT_SPEC.md), архитектура —
[ARCHITECTURE.md](ARCHITECTURE.md), проверки — [OPERATIONS.md](OPERATIONS.md).
