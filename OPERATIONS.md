# OPERATIONS — эксплуатация, тестирование и release-gates

**Назначение:** единственная инструкция по качеству поставки и работе production. Поток компонентов — [ARCHITECTURE.md](ARCHITECTURE.md); контракты — [DATA_CONTRACTS.md](DATA_CONTRACTS.md); этапы — [ROADMAP.md](ROADMAP.md).

## 1. Среды и воспроизводимость

| Среда | Назначение | Правило |
|---|---|---|
| Development | Локальная работа, учебные эксперименты | `uv.lock`, frontend lock, `.env.example` |
| Staging | Полная интеграция и smoke/e2e | Отдельная БД/хранилище, копии без секретов |
| Production | Публикуемая демонстрационная система | Только проверенные версии и ограничения доступа |

Один Docker Compose на одном сервере — удобный старт, но **не** high availability. Не выдавать приложение за круглосуточно доступное без измерений и резервирования.

## 2. Исполняемые процессы

| Процесс | Триггер | Публикация |
|---|---|---|
| Trip ingest | Проверка новых/изменённых архивов по расписанию | После checksum/schema |
| GBFS collection | Регулярный polling с учётом `ttl`, времени ответа | Новые валидные snapshots |
| Preprocessing/Gold | Новые или изменённые входные разделы | После reconciliation |
| Batch ML | Ежесуточно, если доступны признаки | После shape + freshness gate |
| Risk refresh | После нового GBFS / forecast | Только при валидных входах |
| Retraining | Появились достаточные новые actual; degradation review | Только после offline validation + approval |

Prefect управляет зависимостями, retries и статусами. Повторные задачи — идемпотентны. На ошибки внешнего API — timeout, экспоненциальный backoff и ограничение попыток; нет бесконечных loops.

## 3. Пирамида тестирования

- **Unit:** типы, границы времени, DST, null vs zero, idempotency, формулы.
- **Data contract/quality:** schema drift, пропуски, uniqueness, outliers, joins, reconciliation, задержка публикации; воспроизводимость EDA и покрытие ML labels.
- **Integration:** ZIP → Bronze → Silver → Gold, registry → inference → DB, DB → API; тестовая PostgreSQL в контейнере.
- **ML:** leakage, time split, baseline, ablation, train/serve parity, метрики и error slices, доступность признаков на origin, фиксация test и воспроизводимость.
- **Optimization:** ограничения capacity, nonnegative transfer, no-action baseline, симуляционный сценарий.
- **API/Frontend:** контракт JSON, фильтры, актуальность, корректный период, исключения, карта и mobile.
- **E2E:** контрольный набор с известным истинным ответом проходит source → UI.
- **Load/recovery/security:** p95/p99, memory, restart, restore backup, rollback, SQL injection, secrets, auth.

**Независимый контроль:** проверить ключевой показатель отдельным расчётом по первичным данным, а не только тем же helper, что использует Gold. Сохранить evidence, команду запуска и артефакт теста.

**Качество кода:** небольшие типизированные модули, явные контракты, разделение I/O и расчётов; единая production-логика без копий в notebooks/API. Изменения покрывать крайними случаями и регрессионными тестами; сначала измерять, затем оптимизировать.

## 4. CI/CD gates

```text
PR: formatting/lint/type checks + unit + data-contract fixtures
main: integration + Docker build + frontend tests
staging: migrations + smoke + selected E2E + performance smoke
release: approval + versioned deploy + smoke + rollback route
```

На PR запускать быстрые контракты, unit/ML fixtures, lint/type checks. Многолетний EDA, tuning и полный backtest — отдельно при изменениях данных/признаков/модели или по расписанию; каждый запуск имеет ограниченный бюджет, versioned evidence и фиксирует ошибки.

## 5. Мониторинг и инциденты

| Слой | Метрики/сигналы | Поведение при сбое |
|---|---|---|
| Ingestion | Источник, delay, bytes, checksum, failures | Retry, alert; никакой фиктивной свежести |
| Data | Null rate, duplicates, schema change, row-count drift | Quarantine/block publish |
| ML | Feature drift, forecast validity, позднее MAE/bias | Candidate review; fallback при необходимости |
| API | p50/p95/p99 latency, error-rate, DB pool, throughput | Rate limit/degrade gracefully |
| DB/storage | disk, connections, backup age, object counts | Alert; restore test |

Каждый опубликованный результат несёт `data_as_of`, `created_at`, `source/model_version`, `quality_status` и при необходимости `valid_until`. Исторические и live данные не смешивать. Инцидент: определить impacted partitions → остановить публикацию → диагностировать → восстановить/пересчитать → верифицировать → записать разбор.

## 6. Защита и восстановление

- Секреты только env/secret storage; `.env` не коммитить. HTTPS, least privilege, CORS по списку, лимиты запросов; административные интерфейсы закрыты.
- Бэкап PostgreSQL и критичных метаданных + отдельные immutable копии raw/model artifacts; версионированная конфигурация.
- Регулярно **восстанавливать** backup в изолированной среде. Без восстановления backup не считается проверенным.
- Rollback включает код, миграции и совместимую версию модели; сохранённую предыдущую Gold-версию не удалять при неуспешной публикации.
- Логи не должны содержать секретов и чувствительных данных.

## 7. Определение готовности (release decision)

**BLOCKER:** ошибка центральной метрики, leakage, несовместимая схема, публикация недостоверных данных, нарушения доступа или нерабочее восстановление.  
**MUST FIX:** устойчивые сильные ошибки по важным станциям, сломанная фильтрация, неприемлемая задержка, отсутствие fallback/явной ошибки.  
**CAN DEFER:** косметические улучшения, неиспользуемые optional views.

Целевые p95 API ≤300 мс, first-screen ≤3 с, batch forecast ≤5 мин — **гипотезы**, пока нет нагрузочных тестов на заданном окружении.

Полезные команды и стандарты: [knowledge_base/06_TESTING_OPERATIONS.md](knowledge_base/06_TESTING_OPERATIONS.md).
