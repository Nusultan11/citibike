# ROADMAP — этапы реализации и критерии завершения

**Назначение:** последовательность разработки CitiBike Intelligence Platform, зависимости этапов и проверяемые результаты. Функциональные требования описаны в [PROJECT_SPEC.md](PROJECT_SPEC.md), устройство системы — в [ARCHITECTURE.md](ARCHITECTURE.md). Разделы 1–5 определяют план разработки; раздел 6 является единым журналом выполнения работ.

## 1. Стратегия реализации

Сначала строится **вертикальный срез**: реальный архив → Bronze → Silver → Gold → PostgreSQL → API → экран Dashboard. Затем расширяются глубина EDA, объём истории, аналитика, ML и операционные функции. Это позволяет обнаружить проблемы интеграции рано и не тратить ресурсы на инфраструктуру, необходимость которой не доказана.

Каждый этап должен завершаться работающим результатом и тестами, а не только добавлением файлов или описаний. Архитектура развивается через измерения и небольшие изменения с обратной совместимостью.

## 2. Фазы реализации

| Фаза | Содержание | Проверяемый результат |
|---|---|---|
| **F0 — Foundation** | Monorepo, Python-пакет и окружение, каркас web/API, конфигурация, Docker, минимальный CI | Воспроизводимый запуск API и web; базовые lint/type/unit-тесты проходят |
| **F1 — First End-to-End** | Один целый опубликованный архив, Bronze, базовый Silver/Gold, serving DB, API, первая страница | Число поездок на странице независимо сверяется с исходным файлом; проблемы качества не теряются |
| **F2 — Data Trust** | Полный инвентарь исторических файлов; EDA, качество, версии схем, GBFS snapshots, инкрементальный preprocessing | Data-quality профили и правила, воспроизводимый Silver; нет необъяснённых потерь и дубликатов |
| **F3 — Analytics** | Витрины Gold, метрики, temporal/geospatial аналитика, агрегаты, запросы | Показатели согласуются с определениями в METRICS; измерены скорость и корректность фильтрации |
| **F4 — Forecasting** | ML-features с as-of правилами, отдельные targets departures/arrivals, baseline и кандидаты, backtesting | Сопоставимое тестирование, контроль leakage, воспроизводимые модели/прогнозы; champion выбран обоснованно |
| **F5 — Risk & Optimization** | Симуляция состояния станции, риск дефицита/переполнения, эвристика и ограниченная оптимизация | Ограничения выполняются; сценарии сравниваются с no-action и эвристикой; допущения видны |
| **F6 — Product** | Карта станций, глубокая аналитика, прогнозы, риски, рекомендации и drill-down | Основные пользовательские сценарии проверены E2E; свежесть и период каждого показателя явно обозначены |
| **F7 — Automation** | Prefect workflows, MLflow registry, обновления, мониторинг, fallback, алерты | Пайплайны воспроизводимы и могут работать по расписанию; неудачи не публикуют ложные результаты |
| **F8 — Production Hardening** | Staging/deploy, безопасность, benchmark, нагрузка, backup/restore, rollback, документация эксплуатации | Release gates в OPERATIONS выполнены; реальные проверки подтверждены артефактами тестов |

Этапы F2/F3 и реализация web-компонентов могут частично идти параллельно после F1, если контракты зафиксированы. Тестирование, логирование и проверка качества начинаются с F0, а не откладываются до F8.

## 3. Зависимости и последовательность

```text
F0 Foundation
   └─ F1 Vertical E2E
       ├─ F2 Trustworthy data ── F3 Gold analytics ─┐
       └─ Basic API & web ──────────────────────────┤
                                                   ├─ F4 ML forecasts
                                                   ├─ F6 Product views
                                                   └─ F5 Risk & optimization (после F4 + GBFS)
F7 Automation — по мере готовности рабочих задач; F8 — сквозной выпускной этап
```

**Ограничение для F4:** опубликованные архивы поездок поступают с задержкой. Признаки и backtesting должны учитывать дату доступности информации, а не только дату поездки. История GBFS формируется с начала собственного сбора и не восстанавливается из будущих снимков. Подробные правила — в [ML_SPEC.md](ML_SPEC.md).

**Ограничение для F5:** без внутренних операционных данных результат называется сценарной рекомендацией. Результат симуляции нельзя объявлять доказанной экономией компании.

## 4. Переход между фазами

Для принятия очередного результата необходимы четыре свидетельства:

1. **Функция:** реализован конкретный сценарий работы, соответствующий требованиям.
2. **Корректность:** проверены контракты, расчёты, временные границы и существенные крайние случаи.
3. **Воспроизводимость:** запуск и проверка описаны командой, конфигурацией или автоматизированным тестом.
4. **Операционность:** понятны ошибки, задержки, ресурсы и поведение при недоступном источнике.

Если зависимость не выполнена, допускается упрощённая версия с baseline/явным `unavailable`, но нельзя подменять отсутствующие реальные показатели вымышленными.

## 5. Приоритеты и отказ от лишней сложности

- **P0:** точность источников/метрик, наблюдаемость, data lineage, point-in-time корректность, работающий сквозной путь.
- **P1:** базовые прогнозы и сравнение моделей, согласованный Dashboard, автоматическое обновление и восстановление.
- **P2:** улучшение качества ML и сложные оптимизационные сценарии по результатам измерений.
- **Отложить до появления объективной потребности:** Spark-кластер, Kafka, Kubernetes, самостоятельный Feature Store, крупная микросервисная инфраструктура.

Критерии релиза, аварийные сценарии и частоты выполнения находятся только в [OPERATIONS.md](OPERATIONS.md); формулы метрик — только в [METRICS.md](METRICS.md). Практические материалы и ссылки — в папке [knowledge_base](knowledge_base/).

## 6. Журнал реализации

Запись подшага: дата и статус; сделанное; подтверждённые команды и результаты;
commit SHA/PR; ограничения и выводы; один следующий шаг. Статус фазы оценивается по её полному DoD.

### 2026-10-28 — F0.1: COMPLETED; F0: PARTIAL
- Сделано: перенесены 14 документов; созданы AGENTS.md (79 строк), один bikeflow-step/SKILL.md и .gitignore; добавлен единый журнал. ZIP SHA256: a99356cf50802d8137db4f4cf4ff98d6c5ac7702c803bce52a769a965659e643.
- Проверки: `python -m zipfile -t <ZIP>` — Done testing; `python -` (сверка и инвентаризация) — 14/14 идентичны при переносе, итог: 13 идентичны и ROADMAP изменён только разрешённым образом, 17 файлов, 56 относительных ссылок, дубликатов/типовых секретов не найдено; `python <skill-creator>/scripts/quick_validate.py .agents/skills/bikeflow-step` — Skill is valid!; `git diff --check` — 0; `git diff --cached --check` — 13 исходных Markdown hard breaks (код 2), все побайтово подтверждены в ZIP; scoped check новых файлов/ROADMAP и `git -c core.whitespace=-blank-at-eol diff --cached --check` — 0; `git check-ignore --no-index` — секреты/артефакты исключены, .env.example/uv.lock/исходники доступны для Git; staged diff просмотрен, файлов приложения нет.
- Git: origin был пуст; main, `1c79c86d01c80927b10ac45b0efcda45d53eec2e` — `docs: establish BikeFlow F0.1 development foundation`; `git push --set-upstream origin main` успешен, `git ls-remote --heads origin refs/heads/main` подтвердил SHA; `git status --porcelain=v1 --branch` — `## main...origin/main` (чисто). Эта запись уточняется отдельным минимальным коммитом; PR нет.
- Ограничения и выводы: `codex exec --ephemeral --sandbox read-only --color never <discovery-prompt>` — NOT VERIFIED: после ограничения sandbox повторный запуск получил model_not_found для настроенной CLI-модели; конфигурация не менялась. Проверки API/web/CI неприменимы: приложения нет; полный DoD F0 не выполнен. Дополнительные разделы спецификаций не читались; введение ROADMAP прочитано для требуемой правки назначения, остальные документы обработаны только механическими проверками.
- Следующий шаг: после приёмки F0.1 согласовать следующий подшаг F0 — Python-пакет и воспроизводимое окружение (номер подшага в исходном плане не задан); автоматически не начинать.

### 2026-10-28 — F0.2: COMPLETED; F0: PARTIAL
- Сделано: PEP 621 пакет citibike 0.1.0, src layout, CPython 3.12.13 (requires-python >=3.12.13,<3.13), uv_build==0.12.1, dev-группа Ruff/mypy/pytest, настоящий uv.lock, README и 2 smoke-теста импорта/установленной metadata; runtime-зависимостей нет.
- Проверки: `uv sync --locked` — успешно; `uv run --locked ruff check .` — All checks passed!; `uv run --locked ruff format --check .` — 2 files already formatted; `uv run --locked mypy src/citibike` — Success, 1 source file; `uv run --locked pytest -q tests/unit` — 2 passed; `uv build` — wheel и sdist в dist/; `git diff --check` — код 0.
- Дополнительные проверки: импорт/metadata через `uv run --locked python -c` — 0.1.0; offline-восстановление `uv sync --locked --offline --reinstall` и `uv run --locked --offline pytest -q tests/unit` в отдельном UV_PROJECT_ENVIRONMENT — 2 passed; wheel установлен `uv pip install --python .cache/wheel-smoke/Scripts/python.exe --no-deps --offline dist/citibike-0.1.0-py3-none-any.whl`, изолированный `python -I -c` подтвердил импорт из site-packages; Python-проверки содержимого архивов, неизменности lockfile, исходных документов и типовых секретов — успешно.
- Git: main; исходный HEAD 2fe4b1a105311d649523716df68dffe62b0e842f; промежуточный коммит `239e9d0a96dfed2d2a3c624bbeba959799f2d9a4` — `build: add interim F0.2 Python package and locked environment`; diff/staged files проверены, после коммита рабочая директория чистая; эта ссылка фиксируется отдельным минимальным коммитом журнала; PR нет, push не выполнялся.
- Ограничения и выводы: uv 0.12.1, Windows x86_64; Python 3.12.16 не установлен (проверенный URL каталога вернул 404), закреплён доступный uv Python 3.12.13; новые patch-релизы не проверены. Первый format-check затронул исходный Markdown-пример — добавлено исключение *.md в новой конфигурации Ruff, исходники документов сохранены. Первое offline-окружение прервано sandbox Windows при создании launcher, следующий тест дал 2 failed из-за неполной установки; переустановка тех же locked-зависимостей с разрешённым доступом устранила проблему. uv предупредил о cache внутри исходников; архивы проверены, cache/секретов в них нет. Дополнительные проектные документы не открывались; официальная документация uv нужна для проверки совместимости backend. API/web/Docker/CI не создавались и NOT VERIFIED; полный DoD F0 открыт.
- Следующий шаг: после приёмки F0.2 согласовать минимальный каркас API в рамках F0; автоматически не начинать.

### 2026-10-28 — F0: сервисы, Compose и CI — PARTIAL
- Сделано: типизированный FastAPI health/OpenAPI и JSON errors, env-конфигурация, Next.js/TypeScript с реальным API status и пустым состоянием данных, frontend lockfile, non-root API/web Dockerfiles с base digest, Compose health/dependencies, минимальный Foundation CI и README; F0.1/F0.2 сохранены.
- Python: `uv sync --locked`, `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy`, `uv run --locked pytest -q tests/unit`, `uv build` — код 0; 12 passed, 1 upstream deprecation warning; package wheel/sdist без окружений/кэшей.
- Web/HTTP/Docker: `npm --prefix apps/web ci`, `npm --prefix apps/web run lint`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run build` — успешно; dev и standalone production HTTP проверены; `uv run --locked python tests/integration/smoke.py` и вариант `--expect-api unavailable` прошли локально и в Compose, затем recovery прошёл; `docker compose config --quiet`, `docker compose build`, `docker compose up -d --wait --wait-timeout 120` — успешно, оба healthy; браузер desktop/mobile 390px и кнопка повторной проверки подтверждены. `git diff --check`, сверка сохранённых файлов, lockfiles, проверка типовых секретов и negative lint probe — успешно.
- Git: исходные main/HEAD и origin/main — f8ea494a34243fbdcfad3ccf1db996692945e2f7, F0.2-коммиты присутствуют; промежуточная фиксация для запуска реального CI предстоит, SHA/CI evidence будут записаны после проверки; PR нет.
- Решения/ограничения: вместо конфликтующего ESLint-набора использован Biome 2.6.3 с recommended rules; Node native env loader устранил несовместимость --env-file с Next dev worker, standalone launcher копирует static assets; agentRules=false отключает генерацию вложенного AGENTS.md. Дополнительные проектные спецификации не читались; сгенерированный AGENTS и bundled Next agentRules/config docs прочитаны только для удаления побочного эффекта dev-запуска. Windows sandbox TestClient зависал — проверки повторены с разрешённым доступом; HTTP smoke, случайно запущенный при остановленном web, повторён после старта успешно. Starlette предупреждает об устаревающем httpx TestClient; предупреждение не скрыто. Реальные Actions на финальном коммите пока NOT VERIFIED: F0 DoD по CI ещё открыт; F1–F8 не реализованы.
- Следующий шаг: подтвердить Foundation CI на отправленном main, затем зафиксировать окончательный статус F0; F1 не начинать.
