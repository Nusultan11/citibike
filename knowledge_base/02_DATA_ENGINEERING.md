# Шпаргалка 02 — Data Engineering, Polars, DuckDB и Parquet

**Когда открывать:** ingestion, EDA, preprocessing, качество, incremental rebuild, оптимизация RAM. Контракт: [../DATA_CONTRACTS.md](../DATA_CONTRACTS.md).

## 1. Быстрые правила

- Сначала узнай **grain**, ключ, период, схему и размер данных. EDA — не «только несколько графиков», а набор воспроизводимых запросов и проверок по источникам/месяцам.
- Не читай весь многолетний CSV в память ради двух колонок; переходи на столбцовое чтение и агрегаты.
- Raw immutable, Silver — данные + flags; Gold — согласованные агрегаты, а не копия исходника.
- Не создавать тысячи мелких Parquet-файлов по одной станции. Разбиение по времени выбирать после benchmarks; не partition by station по умолчанию.
- Полный проход по всем файлам нужен для критических точных checks, но визуализации можно строить на агрегатах/репрезентативной выборке.

## 2. Полезные шаблоны (адаптировать к фактической схеме)

```python
import polars as pl
lf = pl.scan_parquet('data/silver/trips/**/*.parquet')
by_hour = (
    lf.filter(pl.col('start_station_id').is_not_null())
      .group_by(['start_station_id', 'hour_utc'])
      .agg(pl.len().alias('departures'))
      .collect()
)
```

*Важно:* `hour_utc` здесь — **заранее подтверждённое поле Silver/Gold**, а не поле официального CSV. Для агрегации arrivals по окончанию использовать `ended_at_utc` и отдельный час.

```sql
-- DuckDB: контролируемые агрегаты и чтение только нужных колонок
SELECT start_station_id, COUNT(*) AS departures
FROM read_parquet('data/silver/trips/**/*.parquet')
WHERE started_at_utc >= TIMESTAMP '2026-01-01 00:00:00'
  AND started_at_utc <  TIMESTAMP '2026-02-01 00:00:00'
GROUP BY 1;
```

При чтении ZIP обрабатывать все CSV, сохранять hash и исходный archive ID; повреждённый архив не публиковать. Для больших файлов — потоковый HTTP download во временный файл, затем rename после checksum/проверки.

## 3. Минимальная матрица EDA и качества

| Аспект | Срезы | Проверяемая аномалия |
|---|---|---|
| Rows/schema | файл, месяц, версия | внезапный schema drift, исчезнувший CSV |
| Nulls | поле × месяц/тип велосипеда | всплеск отсутствующих станций |
| Identity | ride_id, source object | дубли, конфликтующие версии |
| Time | duration, interval, UTC/NY, DST | end < start, неверный месячный bucket |
| Geography | stations, coords, time | изменение станции, невозможная география |
| Categories | type × period | неизвестные/новые значения |
| GBFS | station × snapshot, received/source timestamp | gaps, stale, невозможные counts |
| Joins | trips→station history, weather as-of | orphan/many-to-many, будущие прогнозы |

Делать разбор «какие записи пострадали» и долей, а не только абсолютных чисел. Для проблемы выводить `evidence → downstream impact → решение → автотест`.

## 4. Версионирование и пересчёт

- Manifest `{source_key, version, checksum, received_at, state}`; pipeline version и список затронутых партиций.
- При изменении старого архива пересчитать зависимые Silver/Gold-периоды, а не только текущий день.
- Писать результаты сначала в staging; сравнить количество строк, ключи и суммы метрик, потом атомарно переключить read pointer.
- Хранить rejected/quarantine с причинами; решение «удалить» должно быть обратимым от raw.

## 5. Скорость: checklist

1. Сначала benchmark: wall time, peak RSS, bytes scanned, выходной размер, rows/sec.
2. Projection/filter pushdown и lazy scanning; chunk/stream когда поддерживается.
3. Оценить joins/глобальную сортировку — они могут стать memory bottleneck.
4. Контролировать размер файлов и количество маленьких объектов.
5. Сверять быстрый результат с независимым SQL/fixture.
6. Не внедрять Spark, если односерверная обработка укладывается в измеренные требования.

## Официальные материалы

- Polars lazy execution — https://docs.pola.rs/user-guide/lazy/
- Polars parquet — https://docs.pola.rs/user-guide/io/parquet/
- DuckDB Parquet overview — https://duckdb.org/docs/stable/data/parquet/overview
- DuckDB Performance Guide — https://duckdb.org/docs/stable/guides/performance/overview
- Apache Parquet — https://parquet.apache.org/docs/
- Arrow — https://arrow.apache.org/docs/python/
- Pandera validation — https://pandera.readthedocs.io/en/stable/
- Great Expectations — https://docs.greatexpectations.io/
