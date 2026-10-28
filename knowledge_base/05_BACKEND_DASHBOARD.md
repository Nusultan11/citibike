# Шпаргалка 05 — API, Web, Dashboard и производительность

**Когда открывать:** FastAPI endpoints, serving-таблицы, интерактивные карты/графики, frontend контракт, оптимизация UI. Структура модулей — [../ARCHITECTURE.md](../ARCHITECTURE.md).

## 1. Паттерн выдачи данных

`Scheduled processing → validated versioned serving tables → FastAPI → typed fetch → charts/map`. Ни notebook, ни модель не вызываются непосредственно при каждом запросе браузера. Каждая выдача должна иметь `data_as_of`, фильтр, единицу, timezone и `quality_status`.

**Базовые endpoint-группы (пример):**

```text
GET /api/v1/analytics/overview
GET /api/v1/analytics/timeseries?start=...&end=...&area=...
GET /api/v1/stations?bbox=...
GET /api/v1/stations/{station_id}
GET /api/v1/forecasts?station_id=...
GET /api/v1/risks?level=...
GET /api/v1/recommendations?scenario_id=...
GET /api/v1/system/freshness
```

API: Pydantic request/response schemas; pagination; query param validation; consistent 400/404/422/503; parameterized SQL; OpenAPI; `X-Request-ID`/logs. Значения прогноза не безымянные: origin, horizon, model version, unit.

## 2. Макет информационной архитектуры

1. **Executive** — главные показатели и актуальные риски, явная свежесть.
2. **Deep Analytics** — время, география, маршруты, категории, погода, сравнения.
3. **Stations** — карта/кластеризация → карточка станции → история/status.
4. **Forecasting** — hourly departures/arrivals, горизонты, ошибки, интервалы.
5. **Operations** — приоритеты, симуляционные сценарии, ограничения.

Фильтры по времени/району/типу велосипеда должны затрагивать **только поддерживаемые метрики**. Не давать выбирать «тип велосипеда» для риск-модели, если прогноз не обучен на соответствующий разрез.

## 3. Скорость и масштабирование

| Проблема | Практика |
|---|---|
| Медленный тренд | Aggregate заранее + фильтр на backend |
| Таблица 100k строк | Server pagination, сортировка, limit |
| Карта 1k+ точек | MapLibre source/layers, clustering, viewport bounding box |
| Повторные запросы | TanStack Query staleTime, caching по версии результата |
| Долгая initial load | Lazy page/tab fetch, разделение JS bundle |
| Рост p95 | EXPLAIN ANALYZE, фактические индексы, профили запросов |
| Устаревшие данные | Контракт freshness, label «нет обновлений», не выдавать old как live |

**Тестировать** UI на мобильном разрешении, карте с большой сетью, пустом ответе, отсутствии прогноза, неполных периодах, DST и отказе API.

## Материалы

- FastAPI — https://fastapi.tiangolo.com/
- FastAPI Testing — https://fastapi.tiangolo.com/tutorial/testing/
- Pydantic — https://docs.pydantic.dev/latest/
- Next.js App Router — https://nextjs.org/docs/app
- TypeScript — https://www.typescriptlang.org/docs/
- TanStack Query — https://tanstack.com/query/latest/docs/framework/react/overview
- Apache ECharts — https://echarts.apache.org/en/option.html
- MapLibre GL JS — https://maplibre.org/maplibre-gl-js/docs/
- PostgreSQL EXPLAIN — https://www.postgresql.org/docs/current/using-explain.html
