# Шпаргалка 06 — тесты, MLOps, CI/CD и релизы

**Когда открывать:** тесты, проверки слоёв, контейнеры, GitHub Actions, инциденты, восстановление. Нормативные release gates — [../OPERATIONS.md](../OPERATIONS.md).

## 1. Команды — шаблоны, только после настройки проекта

```bash
# Python environment
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q tests/unit
uv run --locked pytest -q tests/integration

# Container checks
docker compose config
docker compose up -d --build
docker compose ps

# Frontend (из apps/web)
npm ci
npm run lint
npm run build
```

**Важно:** команды — примеры; перед запуском сверять их с конфигурацией репозитория, lockfiles и README. Успех подтверждать фактическим запуском.

## 2. Приоритетные тестовые фикстуры

- Архив с несколькими CSV, дублированным `ride_id` и конфликтующей строкой.
- Изменение схемы из старой в новую; неизвестная категория.
- Два перехода DST: отсутствующий час весной и повторный час осенью в NYC.
- Missing start/end station, invalid duration, negative GBFS counts, stale source times.
- join-explosion: 2 поездки × 3 snapshots → не 6 поездок после Gold.
- Историческая погода с прогнозом, выпущенным после forecast origin (должна отвергаться).
- API: null, empty, pagination, диапазон дат, 503/timeout, инъекционный ввод.
- Optimizer: capacity constraints, station with no free docks, no-action baseline.

## 3. Минимальный CI pipeline

```text
Pull request:
  checkout -> uv sync -> Ruff -> Pytest fast suite
  apps/web: npm ci -> lint -> typecheck -> build
Main / staging:
  Docker build -> integration -> migrations -> smoke -> E2E
Release:
  backup -> deploy version -> health + canary requests -> rollback if failed
```

Не проводить тяжёлый многочасовой backtest на каждый PR. Его запускать по новой модели/dataset и по schedule. GitHub Actions хранит CI secrets, не помещать их в YAML текст.

## 4. Checklists эксплуатации

**На каждом scheduled run:** статус, входной checksum, объём, duration, memory, выходной version, quality gate.  
**Перед новой моделью:** validate input availability, backtesting, train/inference parity, shadow и review champion.  
**Перед релизом:** проверить source→UI, API p95/ошибки, fresh/stale markers, restore backup, rollback модель/код, разрешение на публикацию данных.  
**После инцидента:** краткая запись impact → root cause → fix → regression test, иначе ошибка повторится.

## 5. Чистый код и автоматизация

- Функции с явными входами, небольшие модули, типы, единая реализация production-метрики и независимый test oracle.
- Запланированные задачи: immutable inputs → idempotent processing → quality gate → atomic publish; при сбое сохранять прежнюю версию и понятный error state.
- Для долгих EDA/backtest/tuning нужны resource budgets, checkpoints, bounded retries и отдельные jobs; не выполнять их из HTTP и не запускать полный многолетний pipeline на каждый PR.
- Оптимизацию обосновывать профилированием CPU/RAM/I/O и сравнением correctness до/после; новые сервисы — только при измеренной необходимости.

## 6. Методика нагрузочного теста

- Фиксировать окружение: CPU, RAM, DB size, concurrent users, period, queries.
- Измерять p50/p95/p99, throughput, error rate, RAM peak; отдельно ingestion throughput и ML inference time.
- Сравнивать повторные запуски одинаковых сценариев до/после оптимизации.
- Цифры в PROJECT_SPEC — цели до benchmark, не подтверждённые измерения.
- Отдельно проверить server/app caching и отсутствие публикации наполовину обновлённых витрин.

## Материалы

- pytest — https://docs.pytest.org/en/stable/
- Ruff — https://docs.astral.sh/ruff/
- uv — https://docs.astral.sh/uv/
- Prefect — https://docs.prefect.io/
- MLflow tracking — https://mlflow.org/docs/latest/ml/tracking/
- Docker Compose — https://docs.docker.com/compose/
- GitHub Actions — https://docs.github.com/en/actions
- Playwright — https://playwright.dev/docs/intro
- Vitest — https://vitest.dev/guide/
- k6 load testing — https://grafana.com/docs/k6/latest/
- Prometheus — https://prometheus.io/docs/introduction/overview/
- Grafana — https://grafana.com/docs/grafana/latest/
- OWASP API Security — https://owasp.org/API-Security/
- PostgreSQL backup/restore — https://www.postgresql.org/docs/current/backup.html
