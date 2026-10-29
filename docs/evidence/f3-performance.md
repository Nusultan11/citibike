# F3: real-data validation and performance evidence

Источник: `f3-performance.json`; независимая сверка: `f3-analytics.json`; Gold hashes/gates: `f3-gold.json`; browser QA: `f3-ui.json`; F1: `f3-f1-regression.json`.

Publication: `38f4a4bfaf9d05837c63f31c98e615ee121e197440fa1cba19b49922c30a2e91`. F2: `5c4f166d01265d219f7026db9195f3d4cdb362e1f65c1625713628aa55a26cea`. Data mode: `real_official`. Verified at: 2026-10-29T09:16:06.295328+00:00.

## Проверенный охват

24 месяца F2, 92 076 567 accepted Silver rows. Default NYC [2024-10-01,2026-10-01): **92 076 054** starts; 513 starts до окна сохранены в дополнительном event month. Arrivals фильтруются независимо по ended_at.
325 Silver→Gold independent mart/month group comparisons: 0 mismatches. KPI, monthly series, station sample, route rankings/durations, categories, separate departure/arrival peaks и HTML сверены с независимыми SQL непосредственно по Silver. Три top route pairs за весь период дополнительно пересчитаны по Silver. HTTP/HTML count = **92 076 054**, reconciliation mismatches = **0**. F1 fresh raw→UI = **3 860 371**; F1/F3 cross-pins отвергаются с 422.

## Среда и метод

Host: Windows, AMD Ryzen 7 5800HS, 8 cores / 16 logical processors, 16 541 597 696 RAM bytes. Docker Desktop: 16 CPUs / 6 211 846 144 memory bytes. PostgreSQL 17 (точная version string в JSON). DuckDB workers: 2GB engine cap / 2 threads, один тяжёлый worker за раз; cap не равен OS RSS.
Python 3.12.13; DuckDB 1.5.6; psycopg 3.3.6, httpx 0.28.1, fastapi 0.162.0, pydantic 2.14.1, uvicorn 0.54.0. Frontend: Next.js 16.4.3, Node 24.18.0, npm 11.16.0.
Sequential HTTP localhost → Docker API → PostgreSQL, один persistent HTTP client, без concurrent load. Каждый workload: 30 запросов; first приведён отдельно, warm quantiles по следующим 29. p95/p99 — nearest observed upper order statistic из ограниченной выборки, не нагрузочный SLA. Начало нового соединения не означает cold OS/DB cache.
**Cold isolated OS cache: NOT VERIFIED** — shared host/DB, воспроизводимого изолированного сброса не выполнялось. EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) и три same-connection SQL timings сохранены в JSON.

## HTTP latency, ms

| Workload | First | Warm p50 | Warm p95 | Warm p99 |
|---|---:|---:|---:|---:|
| kpi_24_months | 62.70 | 69.32 | 85.31 | 88.13 |
| stations_24_months | 194.83 | 197.29 | 223.04 | 245.12 |
| routes_24_months | 80.50 | 70.35 | 84.84 | 97.18 |
| monthly_series_24_months | 84.72 | 95.00 | 104.22 | 115.16 |
| station_day | 24.80 | 26.31 | 30.57 | 32.24 |
| routes_month | 27.58 | 24.80 | 26.90 | 27.04 |
| routes_month_vehicle | 2251.54 | 2120.81 | 2413.46 | 2691.32 |
| routes_24_months_vehicle | 86.09 | 71.55 | 87.74 | 88.99 |
| station_routes | 31.64 | 29.09 | 33.11 | 45.32 |
| categories | 80.81 | 72.07 | 82.16 | 90.99 |
| geo_day | 28.69 | 27.23 | 34.46 | 35.53 |

## Построение и размеры

- Source contributions: retained successful worker costs 729.924s. Event-month build + independent audits: 2199.731s. Первый Gold invocation: 2394.5s, 38 453.36 logical input rows/s; он уже использовал подготовленные contributions и не является cold end-to-end. Failed rename attempt исключён из retained cost sums и не замаскирован успехом.
- Gold worker OS peak RSS: **1 830 723 584 bytes**; Gold 13 marts × 25 months: **1 869 578 193 Parquet bytes**. Real unchanged Gold repeat: 0 rebuilt, 174.172s (checksum/cache validation).
- First PostgreSQL COPY/index/reconciliation всех 25 months: **2801.422s**. GBFS-only publication reuse всех trip months: **3.094s**.
- Exact route cube: **12 845 939 rows**, build **113.750s**, worker OS peak RSS **382 775 296 bytes**, Parquet **105 797 909 bytes**. Все 16 независимых срезов на каждом из 32 disjoint shards совпали. COPY/reconciliation/indexing cube: **267.594s**; весь publish вместе с monthly rank indices и validation: **503.438s**. Это разные scope measurements, их нельзя складывать как независимые end-to-end времена.
- PostgreSQL database: **94867961523 bytes**; F3 table/index/TOAST relations: **94562467840 bytes**. Включены retained immutable partitions, serving cube и indices; F1 и metadata входят в общий размер БД. Это фактический disk cost нескольких serving grains, не размер только одного endpoint.

## Измеренные исправления и ограничения

До исправления: routes24 HTTP 503 на 5228.14ms (SQL timeout); April base routes 1994.55ms; April electric 2578.12ms. Это отдельные диагностические single requests, не сопоставимый benchmark distribution. После исправления полный период и monthly unique grain используют rank index scans: EXPLAIN actual execution и buffers в JSON. Ориентир 300ms не стал обещанным SLA.
Первая попытка глобального route hash aggregation получила DuckDB OOM на лимите 2GB; лимит и concurrency не повышались. Использованы 32 disjoint hash shards с независимыми grain/slice gates. Применённые 001/002/003 migrations не переписывались.
Месячный route query только с одним category filter требует точной агрегации пар через вторую категорию и точного total_groups. Его p95 остаётся **2413.46ms**; цель 300ms не достигнута для этого workload. Exact full-period cube покрывает все 16 category states; дополнительные monthly partial-filter cubes не создавались из-за роста disk/build cost. Остальные city-wide ranges ограничены 31 днём; длинные требуют station filter; unsupported windows возвращают 422 без усечения.
GBFS: только семь собранных collections 2026-10-28, 473 valid status intervals, cohort 2521. Observed coverage около 0.0512%; stale / insufficient ratios = N/A. Это не availability сети за 24 месяца. District mapping отсутствует, используется versioned event-coordinate WGS84 grid.

## Воспроизведение

Из корня репозитория после README setup и запуска Docker:

```powershell
uv run --locked python -m citibike.analytics build
uv run --locked python -m citibike.analytics publish
docker compose build api web
docker compose up -d --wait --wait-timeout 120
uv run --locked python -m citibike.analytics verify
uv run --locked python -m citibike.analytics benchmark --output docs/evidence/f3-performance.json
```

Сохранить тот же F2 snapshot, pipeline/lockfiles и publication ID. Build/publish допускают явные --snapshot/--gold; сырые данные и generated Parquet остаются локально. Позднейшие прогоны могут отличаться по cache state и ресурсам.
