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

### 2026-10-28 — F0: сервисы, Compose и CI — COMPLETED
- Сделано: типизированный FastAPI health/OpenAPI и JSON errors, env-конфигурация, Next.js/TypeScript с реальным API status и пустым состоянием данных, frontend lockfile, non-root API/web Dockerfiles с base digest, Compose health/dependencies, минимальный Foundation CI и README; F0.1/F0.2 сохранены.
- Python: `uv sync --locked`, `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy`, `uv run --locked pytest -q tests/unit`, `uv build` — код 0; 12 passed, 1 upstream deprecation warning; package wheel/sdist без окружений/кэшей.
- Web/HTTP/Docker: `npm --prefix apps/web ci`, `npm --prefix apps/web run lint`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run build` — успешно; dev и standalone production HTTP проверены; `uv run --locked python tests/integration/smoke.py` и вариант `--expect-api unavailable` прошли локально и в Compose, затем recovery прошёл; `docker compose config --quiet`, `docker compose build`, `docker compose up -d --wait --wait-timeout 120` — успешно, оба healthy; браузер desktop/mobile 390px и кнопка повторной проверки подтверждены. `git diff --check`, сверка сохранённых файлов, lockfiles, проверка типовых секретов и negative lint probe — успешно.
- Git/CI: main, исходный HEAD f8ea494a34243fbdcfad3ccf1db996692945e2f7; `249a2490d2035b28a5bbe1e0182bae7fead82fec` — `build: add remaining F0 services and verification gates`, push в origin/main успешен; [Foundation CI 40665704192](https://github.com/Nusultan11/citibike/actions/runs/40665704192) — python, frontend и docker-smoke завершились success, включая реальную остановку API; итоговая запись фиксируется отдельным завершающим коммитом журнала; PR нет.
- Решения/ограничения: вместо конфликтующего ESLint-набора использован Biome 2.6.3 с recommended rules; Node native env loader устранил несовместимость --env-file с Next dev worker, standalone launcher копирует static assets; agentRules=false отключает генерацию вложенного AGENTS.md. Дополнительные проектные спецификации не читались; сгенерированный AGENTS и bundled Next agentRules/config docs прочитаны только для удаления побочного эффекта dev-запуска. Windows sandbox TestClient зависал — проверки повторены с разрешённым доступом; HTTP smoke, случайно запущенный при остановленном web, повторён после старта успешно. Starlette предупреждает об устаревающем httpx TestClient; предупреждение не скрыто. Полный DoD F0 подтверждён локальными и GitHub Actions проверками кода; завершающий коммит журнала будет отдельно перепроверен в CI. Проверки нагрузки, production release gates и F1–F8 не входят в F0 и NOT VERIFIED; production readiness не заявляется.
- Следующий шаг: после приёмки F0 согласовать первый вертикальный срез F1 на одном полном опубликованном архиве; F1 автоматически не начинать.

### 2026-10-28 — F1: локальный E2E проверен; PARTIAL до подтверждения CI
- База: чистый main `b6c9b900630186114a5ff3b9c2bf4337ce4f712b`, origin Nusultan11/citibike; чужих изменений не было. Применён bikeflow-step; прочитаны запрошенные документы и существующая конфигурация. Дополнительно прочитаны Data quality/source-selection/analysis-quality/metric-definition guidance для независимой сверки и browser/computer-use guidance для проверки интерфейса; исходные спецификации не менялись.
- Сделано: streamed HTTP ingestion (timeout/retry/CRC), immutable Bronze/manifest всех CSV; типизированный Silver с raw/local/UTC временем, ledger/quarantine, детерминированной exact-dedup и изоляцией конфликтов; UTC hourly Gold, отдельные departures/arrivals; PostgreSQL migration/COPY/atomic version switch; read-only typed API, SSR Dashboard с ECharts, NY-фильтрами и явными loading/empty/error/unavailable; lockfiles, Compose, CI и README.
- Реальный источник: через Citi Bike System Data и официальный S3 listing выбран полный NYC `https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip`, April 2026 (164592563 bytes, 4 CSV; April выбран как умеренный по размеру завершённый месяц). SHA256 `fefc1fca369818baeb9206792e02a098b71b59f263064b6627b42317965e2ffb`; publication `69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284`, pipeline f1.0.0. Условия доступа и отсутствие аффилиации отражены в README; raw/Parquet и секреты игнорируются Git.
- Независимая сверка: stdlib CSV + ZoneInfo fold/roundtrip + disk SQLite на всех CSV, без Gold/rules helpers. Input=canonical=3860371, exact duplicates=conflicts=invalid=0; 1939 accepted rides без start station, 10498 без end station, 12177 с missing coordinates, 766 long-duration flags — поездки сохранены, station events учитываются по доступности. Полный `[2026-04-01T04:00Z,2026-05-01T04:00Z)` дал 3860371 во всех звеньях raw → Silver → Gold → PostgreSQL → API KPI/hourly sum → видимый HTML. Агрегированные доказательства: `docs/evidence/f1-202604.json`, без исходных поездок.
- Команды Python: `uv sync --locked`, `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked mypy`, `uv run --locked pytest -q tests/unit`, `uv run --locked pytest -q tests/data_contracts`, `uv run --locked pytest -q tests/integration/test_serving.py`, `uv build` — код 0; 36 unit + 1 contract + 3 real-PostgreSQL integration tests. Wheel содержит SQL migration, wheel/sdist не содержат caches/data/secrets. Starlette TestClient выдаёт 1 upstream deprecation warning; он не скрыт. Windows sandbox TestClient зависал — прогон прерван и успешно повторён с разрешённым доступом.
- Web/Docker: `npm --prefix apps/web ci`, `npm --prefix apps/web run lint`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run build`, `docker compose config --quiet`, `docker compose build`, `docker compose up -d --wait --wait-timeout 120` — код 0; db/api/web healthy. Browser desktop 1280px и mobile 390px: реальное число, canvas графика, отсутствие horizontal overflow, загрузка после submit, фильтр одного NY-дня 146083, обратный интервал error, reset; console warnings/errors отсутствуют. Первый browser запрос пришёл во время перезапуска web, после healthy повторён успешно.
- Real E2E: `uv run --locked python -m citibike.pipeline --period 2026-04 --url https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip` — опубликовано, повторно cache+publish идемпотентны; `uv run --locked python -m citibike.audit --bronze data/bronze/NYC/fefc1fca369818baeb9206792e02a098b71b59f263064b6627b42317965e2ffb --output data/evidence/independent-202604.json` — полный независимый проход; `uv run --locked python -m citibike.verify --result data/processed/69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284 --output docs/evidence/f1-202604.json` — все 8 контрольных значений равны. Processing 232.171 s при DuckDB limit 512MB/2 threads; baseline 299.765 s, запуски шли одновременно, speedup/SLA не заявляются; peak RSS NOT VERIFIED (не измерялся).
- HTTP: `uv run --locked python tests/integration/smoke.py --expected-count 3860371`; вариант `--expected-count 146083 --start-date 2026-04-01 --end-date 2026-04-02`; вариант `--expect-data error --start-date 2026-04-02 --end-date 2026-04-01`; `docker compose stop api` + smoke `--expect-api unavailable --expect-data unavailable`, recovery `docker compose up -d --wait --wait-timeout 120` + исходный smoke; `docker compose stop db` + smoke `--expect-data unavailable`, recovery + исходный smoke — все прошли. При остановленной БД API /health остаётся 200, business endpoint 503; web остаётся 200 и не показывает число.
- Исправления по проверкам: нужен pytz для DuckDB timezone fetch; CSV force_not_null сохраняет исходные пустые строки; mypy/format замечания устранены; NY timezone задан в tooltip; публикация без поездок в заявленном месяце блокируется. Тесты проверяют retry/timeout/404/429/500, incomplete/unsafe/corrupt/empty/unknown schema, all-invalid quarantine, null/station coverage, DST 23/25h, half-open bounds, grain/sums, artifact tampering, idempotency, migration checksum и transactional rollback. Локальные metadata и ledger не выдаются за production SLA; owner-role с read-only session, TLS/RBAC/backup hardening, historical inventory, schema evolution и source refresh относятся к следующим этапам.
- Приёмка на момент записи: DoD 1–11,13–14 PASS по указанным артефактам/проверкам; DoD 12 PARTIAL — реальный новый CI ещё не подтверждён; DoD 15 ожидает итоговой записи. F0 проверки сохранены; F2–F8 не начинались. Код будет зафиксирован промежуточным Conventional Commit для реального CI, без ложного объявления F1 завершённым; SHA/run будут записаны после проверки.
- Следующий шаг: завершить приёмку F1 — проверить GitHub Actions на опубликованном SHA и зафиксировать достоверный итог §6; F2 не начинать.
