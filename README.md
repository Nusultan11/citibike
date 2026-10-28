# BikeFlow Intelligence

Минимальный устанавливаемый Python-пакет `citibike` для разработки платформы
аналитики городского велопроката. Текущее состояние — F0.2; общий F0 не завершён.
API, web, ingestion, обработка данных и ML пока не реализованы.

## Требования

- Git и [uv](https://docs.astral.sh/uv/getting-started/installation/).
  Команды проверены с uv 0.12.1.
- CPython 3.12.13, закреплённый в `.python-version`.
  `requires-python` ограничивает пакет серией 3.12, начиная с этой patch-версии.
- Доступ к PyPI и загрузкам Python при первоначальной установке.

## Окружение

Из корня репозитория:

```sh
uv python install
uv sync --locked
```

`uv` создаёт `.venv`, устанавливает пакет в editable-режиме и dev-зависимости
из `uv.lock`. Активация окружения не требуется: команды запускаются через `uv run`.
Runtime-зависимостей пока нет. `uv.lock` создаётся командой `uv lock`;
обновлять его следует только при намеренном изменении зависимостей.

## Проверки и сборка

```sh
uv run --locked python -c "import citibike; from importlib.metadata import version; print(version('citibike'))"
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src/citibike
uv run --locked pytest -q tests/unit
uv build
git diff --check
```

Тесты проверяют импорт установленного пакета и доступность distribution metadata.
Wheel и source distribution создаются в `dist/`, который исключён из Git.

## Документация

Требования — [PROJECT_SPEC.md](PROJECT_SPEC.md), архитектура —
[ARCHITECTURE.md](ARCHITECTURE.md), проверки — [OPERATIONS.md](OPERATIONS.md).
Текущий результат и следующий согласуемый подшаг — в журнале
[ROADMAP.md, §6](ROADMAP.md#6-журнал-реализации).
