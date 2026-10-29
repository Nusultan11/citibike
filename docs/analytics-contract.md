# F3 — исполняемая аналитическая матрица

Реализация `METRICS.md` и `DATA_CONTRACTS.md`, не изменение нормативных определений.
Population: канонические строки выбранного опубликованного F2 snapshot. API default
ограничивает **начала** календарным NYC окном source inventory. Accepted rows вне
этого окна сохраняются в Gold и отдельно учитываются в reconciliation.

| Показатель | Формула / population | Источник и grain | Время / фильтры | Ограничение |
|---|---|---|---|---|
| completed_published_rides | count started events | Silver → trip hourly/category cube | started UTC in [start,end), NYC dates; start station, vehicle/member | Unknown station включён в city count |
| departures_s | count known start station | station × hour_utc; отдельный category cube | started UTC, те же trip categories | Не включает unknown start |
| arrivals_s | count known end station | station × hour_utc | ended UTC, категории исходной поездки | Не ограничивать start временем запроса |
| net_trip_flow_s | arrivals_s − departures_s | station/hour/day | Event-specific bounds | Не inventory и не availability |
| average_duration | sum duration microseconds / valid count / 1e6 | Start population, sum/count на всех уровнях | Start filters | Средние не усредняются; пики не удаляются |
| trip_category_share | known category n / comparable-known n | Start population by vehicle/member | После time/start/category filters | unknown count отдельно, не в known denominator |
| route_volume | count both-known station pairs | fact_route_daily: local date × from × to; category auxiliary cube | NYC start dates, from/to/categories | Coverage universe: time/categories/from before destination selection/pagination |
| temporal peaks | argmax starts / arrivals отдельно | Hour/day/month | UTC key + NYC label/offset | Source gaps не становятся нулевым спросом |
| period change | (current−baseline)/baseline | Одинаковые filters/population | Equal NYC days, hours и weekday histogram | Gap/несопоставимость/zero baseline → N/A |
| geography | count reported event coordinates per grid | day × event side × cell × station/category | Historical trip endpoint coordinates | WGS84 angular grid 0.01°, floor, boundary clamp; не районы |
| rentable_bikes / returnable_docks | source counts only with operation flags | GBFS station × valid observation interval | Received time to earliest TTL/last_reported expiry/next receipt | No historical trip backfill; stale excluded |
| observed_coverage | union valid seconds / station-window seconds | GBFS observed station universe | UTC arbitrary bounded interval | Gaps excluded, not zero availability |
| empty/full time share | eligible zero seconds / operational observed seconds | Time-weighted GBFS intervals | At least 2 fresh updates, ≥1 refresh cycle, ≥95% target coverage | Otherwise unavailable with reason; threshold is conservative F3 policy |
| active_stations | operating stations at requested instant | Valid GBFS interval | installed and renting or returning | Insufficient network coverage → unavailable, not trip-station count |

## Время, null и география

Технический час — UTC. Дневные/месячные ключи — NYC local date/month. Повторный
час DST имеет два UTC ключа; отсутствующий local hour не создаётся. Интервалы
полуоткрытые. City source-hour без опубликованных start observations остаётся
unknown; фильтр по категории не превращает source gaps в нулевой спрос.

Для координат используется только точка соответствующего события Silver. Ячейка
`floor(latitude*100), floor(longitude*100)` после DECIMAL(12,8) quantization имеет
версию `trip-wgs84-grid-001` (устраняет binary-float ошибки на границе ячейки);
точные верхние границы 90/180 относятся к последней допустимой ячейке. Нет
distance/area claims. Отсутствующие координаты не заполняются GBFS. Borough/district
mapping unavailable: подтверждённых исторических polygon boundaries нет.

GBFS использует bounded step validity, без интерполяции через gaps. TTL=0 не
разрешает положительную длительность удержания. Для исторических ratio требуется
≥95% покрытия, минимум два разных source updates и наблюдаемая длительность хотя
бы одного source refresh cycle: это явно объявленный консервативный quality gate,
а не статистическая гарантия. Семь F2 collections не являются сетью за 24 месяца.
Версия interval policy `f3-ttl-step-2` включает подтверждённые non-operating
статусы в observed coverage. Их rent/return counts остаются null; знаменатели
operating time считаются отдельно. `active_stations` доступен только при полном
покрытии station cohort в выбранный момент; подтверждённый остановленный cohort
может иметь active_stations=0.
Operating target для каждой функции = весь station-window target минус
подтверждённое non-operating время этой функции; gaps остаются неизвестными
и из target не вычитаются. Помимо общего status coverage явно передаются
rent/return operating coverage; ratio gate применяется к своему знаменателю.

## Версии, serving и ограничения запросов

Gold contribution зависит от конкретной Silver partition/checksum. Event-month
partition зависит от всех contributions, затрагивающих её start/end events. Новый
F2 snapshot меняет только затронутые contributions и event months, включая старые
и новые boundary effects. PostgreSQL хранит immutable month partitions и membership
publication → partitions; active pointer переключается транзакционно. F1 tables,
publication и `/v1/*` сохраняют отдельную population.

HTTP читает только агрегаты PostgreSQL. Date filters — NYC midnight bounds. Hourly
series и route/geographic scans ограничиваются документированным диапазоном;
pagination ограничена. Каждый ответ закреплён одним publication_id. Исторические
trip filters не применяются молча к GBFS operational population.
