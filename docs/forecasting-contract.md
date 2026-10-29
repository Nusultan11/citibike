# F4: targets, availability and experiment contract

Версия `f4.0.0`. Нормативные источники: ML_SPEC, DATA_CONTRACTS, METRICS.
Это исполняемое уточнение эксперимента, не изменение исходных спецификаций.

## Population / targets

Два отдельных target: известные station IDs, departures по started_at UTC,
arrivals по ended_at UTC. Это наблюдаемые опубликованные поездки, не весь спрос.
Grain: station_id × origin_utc × target_hour_utc × horizon_h × prediction_type.
Horizon 1..24: floor(origin, UTC hour) + horizon hours, полуоткрытый target hour.
Backtest origins ежедневные 00:00 UTC; календарь target — America/New_York.
Два отдельных глобальных count models (по типу), horizon входит в features.

Целевой ноль допустим только при подтверждённом городском source-hour для
соответствующего типа и station ID, наблюдаемом не далее 7 дней с обеих сторон.
Bracketing после target используется только для проверки label population;
никогда не входит в features. Он доказывает ноль опубликованных событий внутри
наблюдаемого station envelope, не режим работы станции. Пропуск source-hour,
неизвестная станция, длинный station gap остаются missing. Known station ID не
означает, что станция сейчас работает. Source coverage и unknown IDs сохраняются.

Real bounded cohort: 48 станций из Oct 2024 (16 low / middle / high, deterministic
hash selection внутри terciles) после доступности всех зависимых source months;
ещё до 16 новых IDs с deterministic enrollment при первом доступном source month.
Seed cohort требует >=14 наблюдённых дней seed-month; новые IDs — >=7 дней
своего первого месяца. Это фильтр по уже доступному source month, не будущему score.
Ограничение вычислений — 64 stations; city coverage измеряется, не экстраполируется.
Для CI есть отдельный явно controlled_fixture режим, не real-data evidence.

## Point-in-time gate

У исторических архивов нет доказанной первой даты публикации. retrieved_at,
Last-Modified и дата F2/F3 обработки не заменяют эту дату.
Все исторические таблицы качества помечаются **approximate retrospective**.
Консервативная экспериментальная гипотеза: source month доступен через 45 суток
после его NY month end. Это предположение, не установленный факт оператора.
Каждый origin видит только месяцы с assumed_available_at <= origin.
Тот же gate действует на training labels относительно fold fit_as_of.
Strict inference дополнительно требует фактическую доступность immutable F2/F3,
feature dataset и модели <= origin. Их текущие локальные created_at подтверждают
только доступность этой версии сейчас. Их нельзя переносить назад в историю.

Catalog: station_id:string (known as-of ID); target UTC/local hour, weekday,
month, weekend, DST, federal holiday flag (known calendar); horizon:int;
seasonal_naive:float (последний наблюдённый доступный station/weekday/local-hour
count); profile_mean:float и station_mean:float (expanding means только по
доступным месяцам); profile_count:int. Source cutoff и max available_at обязательны.
Никаких текущих координат/capacity, weather actuals, GBFS backfill или short lags.
Calendar rule `us-federal-observed-1` использует федеральные даты и переносы,
не утверждает закрытие станции: [OPM](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/).

Feature catalog (код `features/contracts.py`, `features/dataset.py`, version
`f4.0.0`; фактический code hash и schema hash в dataset/model manifests):

| Имя | Тип | Формула / источник | Availability / максимальная задержка |
|---|---|---|---|
| station_id | string | ID из проверенного Gold, frozen cohort/enrollment | Не раньше seed/first source dependencies; историческая задержка UNKNOWN, hypothetical +45d |
| horizon_h | int | 1..24 | Known calendar, delay 0 |
| utc_hour | int | hour(target UTC) | Known calendar, delay 0 |
| local_hour | int | hour(target America/New_York) | Known calendar, delay 0 |
| weekday | int | NY weekday, Monday=0 | Known calendar, delay 0 |
| month | int | NY month 1..12 | Known calendar, delay 0 |
| weekend | int | weekday>=5 | Known calendar, delay 0 |
| holiday | int | Federal regular/observed calendar rule | Known rule, delay 0; не полный список разовых закрытий |
| dst | int | NY UTC offset=-4 hours | Known calendar, delay 0 |
| seasonal_naive | float | Последний валидный count той же station/type/NY weekday/hour | Только source dependencies available<=origin; historical delay UNKNOWN, hypothesis +45d |
| profile_mean | float | sum(available counts)/count(available labels) по station/type/weekday/hour | Только доступные source months; до 24 месяцев истории, не live lag |
| station_mean | float | sum(available counts)/count(available labels) по station/type | То же as-of правило; scaler/encoding fit только на train |
| profile_count | int | Число валидных available history labels | То же as-of правило; missing не превращается в zero |

UNKNOWN не заменяется обещанием максимального срока публикации. В strict режиме
проверяется фактическая доступность текущей immutable версии до origin;
freshness budget source event window / station evidence — 62 суток. Он не является
доказательством исторического publication SLA. `data_as_of` в прогнозе означает
конец upstream event window, что явно задаёт `data_as_of_kind`; long-term profile
может использовать более ранние месяцы по консервативной availability policy.

Delayed Seasonal Naive — обязательный baseline. Если точного доступного seasonal
count нет, профиль того же station/weekday/hour; при отсутствии профиля —
unavailable, без искусственного нуля. Все кандидаты оцениваются на одной общей
eligible population; исключённые строки и причины считаются отдельно.

## Splits / experiments / promotion

Train origins с Jan 2025, expanding labels с подтверждённой в режиме эксперимента
доступностью до fold fit. Validation origins: June/Sep/Dec 1–15 2025, March 1–15
2026. Final test: Sep 1–Oct 1 2026. Между train и validation — минимум 24h embargo;
target intervals не пересекаются. Идентичные fold/eligible rows для всех моделей.
Train budget: максимум 80000 строк на тип/fold, deterministic hash sampling целых
station/origin blocks с сохранением всех 24 horizons; evaluation не сэмплируется.
Seed 42, 2 threads, последовательные fits. Baseline → default Poisson → LightGBM
→ CatBoost; затем по одному ограниченному варианту параметров, без grid search.
CatBoost default 1000 trees — ограниченный верхний бюджет, early stopping по test
не применяется. Smoke config использует малые явные бюджеты отдельно.

MAE основная; WAPE только при sum(actual)>0, RMSE, bias, peak-hour MAE.
Peak: weekday local 07–09 / 16–19 (заранее, не по test). Segments: activity
tercile на прошлых доступных данных, new-known station <=90 дней с первого
доступного месяца, season, weekday/weekend, holiday/DST-transition, horizon1..24.
Для cold/heat/heavy-rain evaluation slices используется отдельный ERA5 reanalysis
через Open-Meteo (NYC grid proxy, не station measurement). Hourly UTC агрегируется
в NY day с 23/24/25 часами: min<=0°C, max>=32°C, precipitation>=10mm.
Осадки относятся к предшествующему часу: сумма в timestamp местной полуночи
принадлежит предыдущему NY day; порог суммируется через Decimal. Температура
относится к timestamp. Неполный день по любой переменной исключается. Эти outcome
labels присоединяются только ПОСЛЕ prediction и не входят в feature dataset или
model matrix. Для controlled fixtures сеть не используется.
[Источник и ERA5 resolution](https://open-meteo.com/en/docs/historical-weather-api).

Selection фиксируется до final test. Gate: MAE лучше baseline в большинстве
validation windows, ни один ключевой достаточно большой сегмент не хуже >20%,
finite outputs, приемлемые ресурсы, доступные признаки. Неизвестный исторический
publication timeline не доказывает operational uplift; promotion в operational
champion требует strict availability evidence. Иначе champion — seasonal baseline,
даже если retrospective candidate score лучше. Departures/arrivals выбираются отдельно.

## Artifacts / batch / serving

Immutable content-addressed artifacts в ignored data/f4, checksums и run manifests.
MLflow Registry automation относится к F7 по ROADMAP; здесь локальный versioned
champion manifest, не заявление об implemented MLflow Registry.
Prediction intervals не реализуются без калибровки. Point predictions finite >=0.
Source event end старше 62 дней, future/mismatched schema/model inputs дают
unavailable с причиной; такая ошибка не активирует фиктивную публикацию.
issued_at, origin, data_as_of, source/dataset/feature/model versions, quality,
freshness и valid_until сохраняются. Повтор тех же входов возвращает ту же версию.
PostgreSQL publication — одна transaction, immutable versions, failure injection,
rollback на прошлую версию. HTTP только читает serving; не импортирует ML runtime.
Daily batch 5 минут — измеряемая гипотеза, не SLA.

API алгоритмов проверены по первичным источникам:
[PoissonRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.PoissonRegressor.html),
[LightGBM](https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.LGBMRegressor.html),
[CatBoost Poisson / Exponent](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor_predict).
