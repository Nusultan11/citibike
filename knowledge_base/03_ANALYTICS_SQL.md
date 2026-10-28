# Шпаргалка 03 — аналитическое моделирование и SQL

**Когда открывать:** Gold, вычисление KPI, SQL serving, геоаналитика, интерпретация графиков. Канонические формулы: [../METRICS.md](../METRICS.md). Здесь только практическая методика.

## 1. Сначала вопрос, затем таблица

Для каждого бизнес-вопроса запиши: **решение → показатель → охват → grain → источник → фильтр → сравнение → caveat**.

Пример: «на каких станциях утром чаще возникает нехватка велосипедов?» — недостаточно считать поездки. Нужны собственные GBFS snapshots, интервалы наблюдения и показатель availability, затем сравнение с аналогичными рабочими днями.

## 2. Рекомендуемые Gold-формы

| Таблица | Grain | Для какого вопроса |
|---|---|---|
| `fact_station_hourly` | station × hour_utc | Какие периоды наиболее загружены |
| `fact_station_daily` | station × local_date | Какие станции меняются по дням |
| `fact_route_daily` | date × from × to | Какие направления востребованы |
| `fact_station_availability` | station × observation interval | Когда станция была пустой/полной при допустимом покрытии |
| `dim_station_history` | station × valid_from | Что было известно о станции в тот период |
| `dim_calendar` | local date/time | Неделя, праздник, сезонность, DST |

Используй факт/измерение, но не строь ненужную сложную схему из десятков dimension-таблиц до реальных запросов.

## 3. Проблема grain и join-explosion

Нельзя напрямую join поездки за час (например 100 строк) со снимками GBFS за тот же час (например 12 строк): получите 1 200 строк. Сначала сведи источники к совместимому grain или используй as-of join.

```sql
-- Контроль уникальности Gold-ключа
SELECT station_id, hour_utc, COUNT(*) AS n
FROM fact_station_hourly
GROUP BY 1, 2
HAVING COUNT(*) > 1;
```

```sql
-- Сумма отправлений: сверить с Silver с тем же фильтром
SELECT SUM(departures) AS gold_departures
FROM fact_station_hourly
WHERE hour_utc >= ? AND hour_utc < ?;
```

При сверке учитывать unknown start_station_id и область NYC; иначе сравниваются разные populations.

## 4. Как не обмануться графиками

- Показать изменение поездок **в абсолютном значении и нормализованном** по количеству активных станций/наблюдений, если размер сети менялся.
- Сравнивать одинаковые дни недели и сопоставимые сезоны; не объявлять сезонность причинностью.
- Доля `member` по всему году может скрыть разные изменения внутри месяца — смотреть срезы.
- Дневной спрос — не внутридневной риск. Для операционного решения нужна более высокая частота.
- Доля пустого времени должна иметь знаменатель **наблюдаемое время**, а не «все минуты суток при пропусках».
- Географические границы и станции имеют исторические версии; карта «сегодняшних станций» не равна карте прошлых лет.

## 5. Быстрые способы ускорения queries

Готовые serving aggregates и индексы по `station_id`, времени; лимит на ответ и pagination; EXPLAIN ANALYZE до добавления новых индексов; параметризованные SQL; материализованные представления только для действительно популярных запросов. Клиенту отдавать не миллион поездок, а небольшой агрегат, границы и источники.

## Материалы

- Kimball dimensional modeling concepts — https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/
- DuckDB SQL — https://duckdb.org/docs/stable/sql/introduction
- PostgreSQL EXPLAIN — https://www.postgresql.org/docs/current/using-explain.html
- PostgreSQL indexes — https://www.postgresql.org/docs/current/indexes.html
- NYC DOT bike-share context — https://www.nyc.gov/html/dot/html/bicyclists/bikestats.shtml
