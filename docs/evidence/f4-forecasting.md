# F4 — Forecasting: real experiment / independent evidence

2026-10-29. Машинные результаты: [f4-forecasting.json](f4-forecasting.json); контракт и catalog: [forecasting-contract](../forecasting-contract.md). Все таблицы качества ниже — **approximate retrospective**, не operational uplift. Неизвестную историческую первую публикацию архивов нельзя восстановить из retrieved_at/LastModified.

## Population / provenance

F2 snapshot `5c4f166d01265d219f7026db9195f3d4cdb362e1f65c1625713628aa55a26cea`, F3 Gold `38f4a4bfaf9d05837c63f31c98e615ee121e197440fa1cba19b49922c30a2e91`, F4 dataset `d9b87571006b8367d0d286fd77ec9b2e7cac0900a773eae599fc7c3813bc12e5`. 64 stations; 1726199 eligible, 51001 missing-target, 182736 not-known examples. Cohort: 2356614 departures / 2352323 arrivals (2.5608% / 2.5623% city known-ID counts). Это ограниченный cohort, не citywide forecast. 48 legacy IDs по прошлой activity tercile, 16 новых наблюдаемых IDs; первое наблюдение ID не доказывает открытие станции. Zero labels требуют city source-hour и station bracket в том же NY source month; неизвестные gaps не заполняются нулями.

Availability hypothesis: +45 суток после конца самого позднего зависимого source month (включая следующий архив на границах). Canonical F2 snapshot — hindsight, не историческая публикация. Historical strict replay до фактического создания immutable inputs недоступен. Production batch проверяет настоящие timestamps доступности текущих source/dataset/model, но это не доказывает историческую operational accuracy. Случайный split не используется.

## Frozen protocol

Seed 42, 2 threads, train-only encodings/scalers, не более 80000 train rows/тип/fold, hash sampling целых station/origin blocks; evaluation не сэмплируется. Метки train доступны до fit_asof, target windows отделены >=24h embargo. Все 7 configurations имеют одинаковый evaluation_rows_hash внутри type/fold. Отдельные departures/arrivals models.

| Fold | Origins UTC [start,end) | Train rows (оба типа) | Evaluation rows (оба типа) | Max train target |
| --- | --- | --- | --- | --- |
| 0 | 2025-06-01T00:00:00+00:00 / 2025-06-15T00:00:00+00:00 | 99120 | 34710 | 2025-03-01 00:00:00+00:00 |
| 1 | 2025-09-01T00:00:00+00:00 / 2025-09-15T00:00:00+00:00 | 159984 | 37778 | 2025-06-01 00:00:00+00:00 |
| 2 | 2025-12-01T00:00:00+00:00 / 2025-12-15T00:00:00+00:00 | 159984 | 41776 | 2025-09-01 00:00:00+00:00 |
| 3 | 2026-03-01T00:00:00+00:00 / 2026-03-15T00:00:00+00:00 | 159984 | 40348 | 2025-12-01 00:00:00+00:00 |

Selection `0b60c5b235f66ed81118427616f2fa20e7b80e74bd43b3c0ad13901e07984a28` создан `2026-10-29T10:42:00.162160+00:00` до final test. Final origins [2026-09-01,2026-10-01), единственное открытие после selection. Параметры/выбор после test не менялись. Final оценивает заранее зафиксированное объединение выбранных configurations для двух типов; дополнительные CatBoost scores не используются для selection.

## Validation — approximate retrospective

MAE ниже — macro mean по четырём окнам (не pooled MAE). Все quality tables: approximate retrospective.

| Type | Configuration | Mean MAE | Wins | Degraded segments >20% (n>=100) | Max fit s |
| --- | --- | --- | --- | --- | --- |
| departures | catboost_bounded | 1.5245 | 3/4 | 14 | 32.3765 |
| departures | catboost_default | 1.5199 | 3/4 | 10 | 94.4409 |
| departures | lightgbm_bounded | 1.5665 | 3/4 | 12 | 0.9358 |
| departures | lightgbm_default | 1.5960 | 3/4 | 12 | 0.6745 |
| departures | poisson_bounded | 1.7683 | 1/4 | 41 | 0.2499 |
| departures | poisson_default | 2.0736 | 1/4 | 64 | 1.2028 |
| departures | seasonal_naive | 1.7544 | baseline | — | 0.0192 |
| arrivals | catboost_bounded | 1.4957 | 3/4 | 14 | 31.5277 |
| arrivals | catboost_default | 1.5067 | 3/4 | 11 | 105.8990 |
| arrivals | lightgbm_bounded | 1.5684 | 3/4 | 16 | 0.9449 |
| arrivals | lightgbm_default | 1.5709 | 3/4 | 15 | 0.9588 |
| arrivals | poisson_bounded | 1.7821 | 1/4 | 48 | 0.2357 |
| arrivals | poisson_default | 2.0973 | 1/4 | 67 | 0.2560 |
| arrivals | seasonal_naive | 1.7479 | baseline | — | 0.0281 |

## Untouched final test — approximate retrospective

| Type | Configuration | N | MAE | WAPE (ratio) | RMSE | Bias | Peak MAE |
| --- | --- | --- | --- | --- | --- | --- | --- |
| departures | seasonal_naive | 44773 | 1.9903 | 0.6505 | 3.7805 | 0.0535 | 2.8944 |
| departures | catboost_bounded | 44773 | 1.6108 | 0.5265 | 3.0009 | 0.1390 | 2.3319 |
| departures | catboost_default | 44773 | 1.6031 | 0.5240 | 2.9894 | 0.1676 | 2.3449 |
| departures | lightgbm_bounded | 44773 | 1.5778 | 0.5157 | 2.9432 | -0.1228 | 2.2997 |
| departures | poisson_bounded | 44773 | 2.0796 | 0.6797 | 4.1594 | 0.3249 | 3.2536 |
| arrivals | seasonal_naive | 44773 | 1.9610 | 0.6422 | 3.7583 | 0.0657 | 2.8044 |
| arrivals | catboost_bounded | 44773 | 1.6512 | 0.5407 | 3.0548 | 0.2648 | 2.3543 |
| arrivals | catboost_default | 44773 | 1.6693 | 0.5467 | 3.0890 | 0.2376 | 2.4845 |
| arrivals | lightgbm_bounded | 44773 | 1.5988 | 0.5236 | 2.9919 | -0.0170 | 2.3003 |
| arrivals | poisson_bounded | 44773 | 2.0952 | 0.6861 | 4.3056 | 0.3298 | 3.2430 |

## Horizon error — approximate retrospective, final test

| Horizon | departures seasonal_naive | departures lightgbm_bounded | departures catboost_default | arrivals seasonal_naive | arrivals lightgbm_bounded | arrivals catboost_bounded |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2.1273 | 1.5154 | 1.5133 | 2.1679 | 1.6055 | 1.6379 |
| 2 | 1.9164 | 1.4873 | 1.4933 | 1.9325 | 1.4056 | 1.4386 |
| 3 | 1.6132 | 1.2337 | 1.2598 | 1.5867 | 1.1980 | 1.2102 |
| 4 | 1.2100 | 0.8423 | 0.8518 | 1.1975 | 0.9198 | 0.9328 |
| 5 | 0.7365 | 0.5995 | 0.5910 | 0.8164 | 0.6778 | 0.6843 |
| 6 | 0.5497 | 0.4632 | 0.4715 | 0.5545 | 0.4990 | 0.5265 |
| 7 | 0.3842 | 0.3685 | 0.3820 | 0.4107 | 0.4202 | 0.4588 |
| 8 | 0.3711 | 0.3539 | 0.3661 | 0.3581 | 0.3736 | 0.4161 |
| 9 | 0.5656 | 0.5466 | 0.5488 | 0.4995 | 0.5244 | 0.5433 |
| 10 | 1.0949 | 0.9778 | 0.9682 | 0.9834 | 0.9340 | 0.9297 |
| 11 | 1.9679 | 1.6092 | 1.5598 | 1.7243 | 1.4831 | 1.4943 |
| 12 | 2.5859 | 2.1771 | 2.1329 | 2.4077 | 2.1081 | 2.1295 |
| 13 | 2.4439 | 1.8518 | 1.8680 | 2.3235 | 2.0032 | 2.0277 |
| 14 | 2.0979 | 1.7230 | 1.7385 | 2.0920 | 1.7789 | 1.8145 |
| 15 | 2.3522 | 1.8855 | 1.9311 | 2.3169 | 1.9395 | 1.9994 |
| 16 | 2.3889 | 1.8974 | 1.9685 | 2.3862 | 2.0125 | 2.0756 |
| 17 | 2.5636 | 2.0331 | 2.0993 | 2.5524 | 2.0456 | 2.1485 |
| 18 | 2.7149 | 2.1373 | 2.1599 | 2.7854 | 2.1578 | 2.2171 |
| 19 | 2.9450 | 2.3729 | 2.3849 | 2.9440 | 2.3099 | 2.4341 |
| 20 | 2.8928 | 2.4172 | 2.5379 | 2.8053 | 2.3307 | 2.4369 |
| 21 | 3.2288 | 2.6618 | 2.7265 | 3.3141 | 2.7465 | 2.7868 |
| 22 | 3.3631 | 2.6853 | 2.7486 | 3.2808 | 2.5880 | 2.7188 |
| 23 | 2.9664 | 2.1745 | 2.2879 | 3.0027 | 2.3804 | 2.5139 |
| 24 | 2.5938 | 1.7796 | 1.8089 | 2.5286 | 1.8561 | 1.9760 |

## Segment error — approximate retrospective, final test

MAE; N/A означает отсутствие подходящих observations, не нулевую ошибку. Все сезоны представлены validation; final содержит только autumn. Cold weather не выдумывается в сентябре. Полные n/MAE/WAPE/RMSE/bias для каждого сегмента и fold — в JSON.

| Segment | departures seasonal_naive | departures lightgbm_bounded | departures catboost_default | arrivals seasonal_naive | arrivals lightgbm_bounded | arrivals catboost_bounded |
| --- | --- | --- | --- | --- | --- | --- |
| activity:low | 0.6078 | 0.5271 | 0.5344 | 0.5875 | 0.5241 | 0.5550 |
| activity:middle | 1.4058 | 1.0992 | 1.1342 | 1.4523 | 1.1449 | 1.1869 |
| activity:high | 3.4903 | 2.7100 | 2.7986 | 3.3803 | 2.7480 | 2.8912 |
| activity:new | 2.4515 | 1.9729 | 1.9390 | 2.4200 | 1.9750 | 1.9634 |
| new_known | N/A | N/A | N/A | N/A | N/A | N/A |
| weekday | 1.9501 | 1.5429 | 1.5660 | 1.9248 | 1.5542 | 1.5958 |
| weekend | 2.0994 | 1.6727 | 1.7039 | 2.0593 | 1.7201 | 1.8016 |
| holiday | 1.8869 | 1.8023 | 1.7799 | 1.9821 | 1.8711 | 1.8877 |
| dst_transition | N/A | N/A | N/A | N/A | N/A | N/A |
| weather:cold | N/A | N/A | N/A | N/A | N/A | N/A |
| weather:heat | N/A | N/A | N/A | N/A | N/A | N/A |
| weather:heavy_rain | 2.0924 | 1.7260 | 1.8424 | 2.0443 | 1.8004 | 1.9675 |

## Champion / error analysis

Для **departures и arrivals champion = Seasonal Naive**, отдельные model IDs. LightGBM/CatBoost выигрывают 3/4 validation windows, однако деградируют >20% на ключевых horizons/high-activity/new-known сегментах; на первом летнем окне профиль из зимней доступной истории ограничивает перенос. У Poisson только 1/4 побед. Resource gates проходят, strict historical availability evidence отсутствует. Все gates обязательны, лучшие test scores не назначают champion. В том числе corrected weather gates не меняют это решение. Residuals, station/hour predictions и все отвергнутые параметры сохранены локально.

| Type | Largest baseline station MAE, final | N | MAE | Bias (approximate retrospective) |
| --- | --- | --- | --- | --- |
| departures | 6230.02 | 720 | 5.7500 | 1.2722 |
| departures | 6753.08 | 720 | 5.4264 | 0.8319 |
| departures | 6224.06 | 720 | 5.3111 | -2.5972 |
| departures | 6072.16 | 720 | 5.0139 | -0.1222 |
| departures | 5703.13 | 720 | 4.9028 | 0.3028 |
| arrivals | 6230.02 | 720 | 5.6972 | 1.2083 |
| arrivals | 6753.08 | 720 | 5.4889 | 0.9222 |
| arrivals | 6072.16 | 720 | 4.9583 | 0.0000 |
| arrivals | 6224.06 | 720 | 4.9472 | -2.5056 |
| arrivals | 5703.13 | 720 | 4.2708 | -0.1236 |

Observed counts не означают весь спрос: capacity/closures/GBFS/weather forecast неизвестны. Низкая station coverage, delayed history и межсезонный сдвиг ограничивают baseline; на высокоактивных станциях абсолютная ошибка выше. Причинные выводы не делаются.

## Weather diagnostic correction

ERA5 city-grid reanalysis — только outcome labels ПОСЛЕ prediction, не feature input. [Official semantics](https://open-meteo.com/en/docs/historical-weather-api): hourly precipitation — preceding-hour sum. Первоначальная policy1 относила его к дню timestamp; policy2 относит к NY дню интервала и суммирует Decimal. В evaluation изменились rain labels 2025-09-07 (10.2→9.3 mm) и 2026-03-06 (10.0→5.9 mm). Для 2025-12-14 корректная сумма 10.0 mm остаётся heavy rain; float rounding не меняет threshold. Cold/heat, все predictions/actuals, основные scores, параметры и frozen selection неизменны. Исходные immutable runs сохраняют legacy flags; независимый verifier проверяет их арифметику и отдельно публикует исправленные weather metrics в evidence JSON. Это posthoc исправление auxiliary diagnostic labels, не повторный tuning/test selection. Новая immutable weather policy2 для следующих запусков создана из того же сохранённого raw response.

## Independent verification / serving / resources

Silver label oracle: 1046111 nonzero groups, 4708937 events, 0 mismatches/0 lost-positive groups. Независимые range joins: 128 feature samples, 0 mismatches. Stdlib metric oracle пересчитал 1082284 validation + 447730 final prediction rows: 0 metric mismatches/0 PIT fold violations; residuals/calendar и одинаковые populations проверены.

Real strict batch `1e204ac4a944381529e00b500586a00a82c7b8eca31cd8f4c978b1c282d81810`: origin `2026-10-29T11:46:44.472185+00:00`, issued `2026-10-29T11:46:50.027325+00:00`, data_as_of `2026-10-01T04:00:00+00:00` = source event window end. 64×24×2 = 3072 rows: 3024 finite nonnegative + 48 unavailable (одна stale-history station). Feature/source/model/dataset versions и availability проверены; unavailable не замаскирован нулём. Полная сверка artifact→PG→HTTP: 3072/3072/3072, 0 mismatches. Повтор publish сохраняет ID; в disposable PostgreSQL проверены COPY/activation failure, идемпотентность, retained version pin и rollback. CHECK NULL defect воспроизведён и исправлен новой migration005 без изменения применённой004. API runtime не содержит ML libraries.

| Phase | Wall/elapsed s | OS peak RSS bytes |
| --- | --- | --- |
| dataset | 90.0630 | 1461096448 |
| validation | 971.5000 | 590106624 |
| final_test | 247.7340 | 518615040 |
| inference | 5.5630 | 199872512 |

Полный CLI predict+publish на финальном batch из прошедшего CI code SHA `1c5d5d753bf4c817728aa9e8c8b8e691fda8871b`: **7.7726736s** (предыдущие batches 11.7243199s и 8.2651219s). Замер включает запуск двух CLI, не training/backtest. Оба <300s для данного cohort; это не citywide SLA. RSS — process high-water measurement, не изолированная estimator memory. Shared host / sequential runs, concurrent-load SLA — NOT VERIFIED.

Fresh F1 raw→UI regression: [f4-f1-regression.json](f4-f1-regression.json), все 8 stage values 3860371. Final local tests **92 passed**, один существующий StarletteDeprecationWarning; Ruff/format/mypy (92 files), locked sync, package build, frontend npm ci/lint/typecheck/build, Docker/HTTP проходят. Фактические SHA и CI фиксируются в ROADMAP §6 после push. MLflow Registry automation — F7; здесь immutable local manifests, не Registry. Prediction intervals не заявлены; F5–F8 не реализуются.

## Постаудитное исследование A/B — 2026-11-01

Отдельная validation-only работа F4, без повторной реализации этапа и без перехода
к F6. Опубликованный контракт, selection, champion, forecast/PG/F5 и сентябрьский
final не менялись. September 2026 уже раскрыт и не является новым independent test.
Machine-readable результаты и ссылки на immutable artifacts —
[f4-feature-research.json](f4-feature-research.json).

**Решение: для обоих targets остаётся Seasonal Naive; research candidate нет.**
Владелец проекта принял работу как **exploratory validation-only результат с
ограничениями** (`ACCEPT WITH LIMITATIONS`). Это не полное выполнение исходного DoD и
не prospective validation.

Ни один из 10 вариантов не прошёл правило решения: везде есть критическая деградация
сегментов. Все четыре проверенных варианта gate дают Seasonal Naive: правило протокола,
правило первого протокола, без станций и peak-hour, без станций, peak-hour и weather. Это
проверка только этих вариантов, выполненная постфактум на уже известных результатах. Она
**не устраняет post-hoc selection bias**: другие возможные правила не проверялись, а само
правило решения написано после появления результатов.

Лучший по macro MAE — CatBoost+A в обеих populations: его MAE ниже Seasonal Naive на
15.6–16.9% (Seasonal Naive выше CatBoost+A на 18.4–20.3%). Это не основание для выбора:
в fold0 CatBoost+A хуже Seasonal Naive на 8.6–10.5%, а его выигрыш над CatBoost base
составляет 1.2–2.9%, держится в основном на fold0 и не устойчив по folds.
Operational uplift не заявляется. Отсутствие улучшения здесь — результат, а не сбой.

### Протокол

Immutable protocol `a28aed18d7d2332728be83bd63d5ef88dc3729ec553b08b0065e19c05e484e07`
(`f4-feature-research-2`) сохранён 2026-11-01T15:13:00.126928Z по локальным часам, за
23 секунды до первого из своих trials. Он фиксирует hashes
source/datasets/features/executor/environment, обе populations, четыре прежних
validation folds, seed42/2 threads, 11 variants, бюджеты, гипотезы, правило решения и
1859 защищённых файлов F4/F5. Выполнено 176 trials: 2 populations × 4 folds × 2 targets
× 11 variants. Нового tuning нет: при прежних bounded hyperparameters проверяется
гипотеза о признаках.

**`a28aed18` не является слепой предрегистрацией.** Почти все его результаты к моменту
заморозки уже существовали, а гипотезы, правило решения и weather-gate написаны, когда
эти файлы были на диске:

| Происхождение чисел | Trials | Когда появились впервые |
|---|---:|---|
| Повтор frozen F4 (база cohort64) | 32 | 2026-10-29 |
| Первый расчёт под `69a184b5…` | 136 | 2026-10-30 |
| Действительно новые (Poisson base cohort128) | 8 | 2026-11-01 |

Первый протокол `69a184b5…` (outcome `8ded0159…`, 160 trials) заменён потому, что аудит
кода нашёл: Poisson+A не с чем сравнить, из gate выпали прежние `weather:*` сегменты,
нет правила решения, а верификатор входит в hash fit-кода. Его MAE по каждому trial были
напечатаны в локальный лог той сессии (`.cache/f4fr-run.log`, 160 строк). Фраза «before
any metric was read» в плане `a28aed18` и в JSON неточна и закреплена хешем: она верна
только для исполнителя замены, и подтверждает это лишь транскрипт сессии. Артефакты
сохранены. Предсказания 160 из 160 общих trials совпали по hash: это детерминированный
повтор, а не независимое подтверждение.

Хешами доказано, что каждый trial содержит id плана, а outcome — hash каждого trial.
Внешне доказано только, что результаты существовали не позже CI на `37db522`
(2026-11-01T16:42:08Z). Порядок «протокол → trials» опирается на локальные часы.

Группы признаков (формула, dtype, lineage, dependencies, available_at, max lag,
missing policy и parity каждой колонки — в catalog, schema `b3ee8e1b…`):

- **A:** sin/cos NY hour/weekday/month, ISO week-of-year, weekday AM/PM peak flags,
  holiday×peak. Известный target calendar, включая DST.
- **B:** latest/previous observed published-month mean и n, trend на календарный
  month gap, expanding target-month seasonal mean/n. Каждая dependency доступна на
  `min(origin, fold fit_as_of)`. Пропущенный месяц не становится нулём, отсутствующая
  seasonal history остаётся NULL. Trees используют native NaN; Poisson — train-only
  median + missing indicator (это model input, не наблюдение).

Availability **+45d — approximate-retrospective гипотеза**, не исторический факт.
Правило применяется и к файлам-зависимостям месяца, поэтому фактическая задержка больше:
в 21 из 24 месяцев метки считаются доступными на 28–31 день позже, примерно через 73–76
дней после конца месяца. Ни одна дата не раньше правила, то есть задержка консервативна,
но она усиливает сдвиг: март 2025 при чистых +45d попал бы в train fold0, а фактически
доступен 2025-06-15, позже `fit_as_of` 2025-06-01. Strict historical PIT — NOT VERIFIED:
дата первой публикации архивов неизвестна, использована их текущая редакция.
ERA5 входит только в post-prediction gate/diagnostics, не в признаки.

Ограничение истории: `seasonal_month_mean` полностью отсутствует в train folds0–2 и в
validation folds0–1 обеих populations. В validation fold2/fold3 missing 25.30%/11.88%
(cohort64) и 21.49%/18.28% (cohort128), в train fold3 — 84.10%/82.51%. Годовой
профиль в этих данных почти не проверен; его отдельная бесполезность не доказана.

### Причины ошибок (RCA на frozen predictions)

`actual>=20` — post-hoc outcome slice; он описывает, где сосредоточена ошибка, но не
доказывает operational miscalibration. Причинность не установлена. RCA выполнен на тех же
folds, на которых затем сравнивались группы, поэтому гипотезы не проверялись на новых
данных.

| Fold | Цели train | Validation | Level ratio V/T | Bias SN → CatBoost, dep | Новые станции: rows/IDs |
|---|---|---|---:|---:|---:|
| 0 | 2025-01…02 | Jun 2025 | 2.401 | −1.231 → −1.943 | 1333 / 4 |
| 1 | 2025-01…05 | Sep 2025 | 1.806 | −0.675 → −0.561 | 2218 / 7 |
| 2 | 2025-01…08 | Dec 2025 | 0.731 | +1.291 → +0.489 | 1652 / 5 |
| 3 | 2025-01…11 | Mar 2026 | 0.665 | −0.141 → +0.158 | 0 / 0 |

- Отложенные метки оставляют в train только прошлые сезоны: в folds0–2 ни один
  календарный месяц validation не встречается в train. Знак bias следует за level
  ratio; ранговая корреляция ratio и bias CatBoost по станциям −0.68…−0.95 (ассоциация).
- `actual>=20` — 2.1–2.8% rows и 19–22% абсолютной ошибки летом/осенью, но 0.4–0.5%
  rows и около 4% ошибки зимой/весной. Top-5 станций дают 20–35% ошибки.
- Predefined peak-hour: 21% rows, 38–41% спроса, 31–37% абсолютной ошибки.
- Horizon-профиль повторяет час суток (origins всегда 00:00 UTC: MAE 0.25 на h8 и
  3.29 на h22), а не рост ошибки с горизонтом. Короткого лага в модели нет.
- `6753.08` в fold0 имеет 0 eligible train rows: ID виден с 2025-01-16, known_at
  2025-04-15, targets ещё недоступны на fit_as_of 2025-06-01. `6230.02`: 1060 eligible
  rows, train mean 6.89 против validation 14.09. Summer underprediction сменяется
  winter overprediction. Это следствие двух clocks, а не случайного sampling.

### Результаты validation

Macro MAE: base / A / B / A+B. Seasonal Naive и Poisson base в той же строке.

| Population, target | Seasonal Naive | LightGBM | CatBoost | Poisson base / A |
|---|---:|---|---|---|
| cohort64 departures | 1.7544 | 1.5665 / 1.6097 / 1.5695 / 1.6006 | 1.5245 / **1.4812** / 1.5492 / 1.5054 | 1.7683 / 1.7570 |
| cohort64 arrivals | 1.7479 | 1.5684 / 1.6035 / 1.5678 / 1.5927 | 1.4957 / **1.4529** / 1.5104 / 1.5012 | 1.7821 / 1.7615 |
| cohort128 departures | 1.7202 | 1.5157 / 1.5997 / 1.5067 / 1.5964 | 1.4602 / **1.4433** / 1.4638 / 1.4701 | 1.9003 / 1.9231 |
| cohort128 arrivals | 1.7211 | 1.4980 / 1.6206 / 1.5044 / 1.6220 | 1.4681 / **1.4379** / 1.4601 / 1.4476 | 1.9199 / 1.9281 |

Populations не смешиваются: cohort128 — другая выборка, а не улучшение cohort64.

Парные разности на идентичных rows (left − right, отрицательное лучше). Интервал —
95% crossed station × NY day bootstrap, 500 replicates, seed42, exploratory, без
поправки на множественность. Эффект заявляется, только если интервал не содержит
ноль и тот же знак есть в большинстве folds.

| Сравнение | Population, target | ΔMAE | 95% | ΔMAE по folds0–3 | Вердикт |
|---|---|---:|---|---|---|
| CatBoost A − base | 64 dep | −0.0433 | [−0.0772, −0.0168] | −0.186, +0.009, −0.057, +0.061 | inconclusive |
| | 64 arr | −0.0428 | [−0.0725, −0.0186] | −0.175, −0.024, +0.010, +0.017 | inconclusive |
| | 128 dep | −0.0168 | [−0.0398, +0.0044] | −0.159, +0.042, +0.024, +0.025 | inconclusive |
| | 128 arr | −0.0302 | [−0.0534, −0.0107] | −0.167, −0.017, +0.047, +0.016 | inconclusive |
| CatBoost B − base | 64 dep | +0.0247 | [+0.0099, +0.0409] | +0.059, −0.017, +0.055, +0.002 | **degrades** |
| | 64 arr | +0.0147 | [+0.0011, +0.0328] | +0.036, −0.010, +0.047, −0.014 | inconclusive |
| | 128 dep | +0.0037 | [−0.0083, +0.0184] | +0.030, +0.019, −0.037, +0.003 | inconclusive |
| | 128 arr | −0.0080 | [−0.0192, +0.0033] | +0.018, −0.016, −0.040, +0.007 | inconclusive |
| CatBoost A+B − A | 64 arr | +0.0484 | [+0.0279, +0.0753] | +0.035, +0.009, +0.143, +0.005 | **degrades** |
| | 128 dep | +0.0268 | [+0.0164, +0.0403] | +0.005, −0.034, +0.135, +0.001 | **degrades** |
| LightGBM A − base | 128 dep | +0.0840 | [+0.0554, +0.1150] | −0.192, −0.033, +0.540, +0.020 | inconclusive |
| | 128 arr | +0.1226 | [+0.0859, +0.1613] | −0.178, −0.025, +0.688, +0.005 | inconclusive |
| Poisson A − base | 128 dep | +0.0228 | [−0.0400, +0.0822] | −0.048, +0.380, +0.131, −0.372 | inconclusive |

Все 44 сравнения групп — в `group_effects` и `paired_comparisons` JSON: 40 inconclusive,
3 degrades, 1 improves. Единственный improves — A+B относительно B у CatBoost на 64 dep,
то есть снова вклад A. Положительного вердикта для добавления группы к base нет, поэтому
эффекты A и B не заявляются. Для сравнения CatBoost A − base интервал не содержит ноль в
трёх ячейках, но знак совпадает только в 2 из 4 folds, и по правилу это inconclusive.

Gate кандидата CatBoost+A: 3/4 wins в обеих populations, ресурсы в норме, деградации
10/5 (cohort64) и 6/5 (cohort128) для departures/arrivals. Во всех четырёх ячейках
среди них fold0 `activity:new`, `new_known`, `station:6230.02`, `station:6753.08`;
в departures ещё отдельные horizons и в cohort64 `3:weather:heavy_rain`. Base имел
17/17 и 15/11, так что A уменьшает число деградаций, но не устраняет их.

### Проверка гипотез

Ожидания записаны в протоколе, но уже при наличии результатов на диске и на тех же
folds. Статусы ниже описывают согласие с этими данными, а не prospective подтверждение.

| Гипотеза | Ожидание в протоколе | Наблюдение | Статус |
|---|---|---|---|
| H1 сдвиг уровня | A не экстраполирует неизвестный месяц; fold0 самый трудный | CatBoost fold0 bias −1.94 → −1.67 с A, но MAE 1.95 хуже SN 1.77; underprediction остаётся | Согласуется частично: A смягчает, но не снимает |
| H2 редкие станции | B помогает new/low сильнее A | `activity:new` с B −0.01…−0.06 во всех 8 ячейках trees, но A сопоставим; low почти без изменений; интервала для этого среза нет | Направление есть, эффект мал, NOT VERIFIED |
| H3 хвост | Ни одна группа не снимает bias `actual>=20` | MAE 11.3–14.7 у всех tree-вариантов и SN | Согласуется |
| H4 peak-hour | A снижает predefined peak-hour MAE | CatBoost лучше во всех 4 ячейках (2.525→2.464, 2.458→2.370), LightGBM хуже во всех 4; интервала для среза нет | Не согласуется как общее правило |
| H5 устаревший тренд | B меняет знак по folds, стабильного выигрыша нет | Знак B меняется в folds; 1 degrades, 7 inconclusive для B − base | Согласуется |

Отрицательные результаты: по правилу B не дал выигрыша ни одной tree-модели. Macro MAE
A+B у CatBoost выше, чем у A, во всех 4 ячейках, но по правилу это 2 degrades и 2
inconclusive. У LightGBM A повышает macro MAE во всех 4 ячейках за счёт fold2 (Dec): там
MAE растёт на 0.36–0.69, bias почти удваивается, хотя folds0–1 лучше; по правилу все 4
сравнения inconclusive. Это совместимо с тем, что деревья продолжают `week_of_year` за
пределы train, но механизм отдельно не проверялся. Poisson нестабилен (std до 0.63), хуже
SN в cohort128 и деградирует в десятках сегментов.

### Расширение cohort

Cohort128: 32 low/32 middle/32 high/32 new, 4.77% known-ID events против 2.56%;
весь cohort64 входит как подмножество. Eligible validation rows есть у 127 станций,
в folds их 99/105/117/122. Train budget тот же (80000 rows на type), поэтому на
станцию приходится меньше строк. Без eligible rows осталась `5348.06_old` (last event
2025-09-29). В new tier вошёл похожий на служебный ID `1234.56` (3248 departures,
4418 arrivals за 16 месяцев наблюдений): он есть в проверенном Gold и не исключался,
но его природа в этой работе не проверялась.

| Срез cohort128, departures (pooled MAE) | Rows | Stations | SN | CatBoost base | CatBoost A | A − SN, macro по folds, 95% |
|---|---:|---:|---:|---:|---:|---|
| overlap64 | 77306 | 64 | 1.7516 | 1.4894 | 1.4764 | −0.2571 [−0.3383, −0.1877] |
| added64 | 67707 | 63 | 1.6647 | 1.3644 | 1.3519 | −0.3000 [−0.3916, −0.2134] |

Arrivals: overlap64 1.7451/1.4844/1.4597, added64 1.6734/1.3866/1.3613; A − SN
−0.2678 и −0.3012. Выигрыш trees над SN переносится на добавленные станции, но gate
не проходит и там. Те же станции при обучении на большей cohort: CatBoost base
−0.0094 [−0.0265, +0.0070] dep, +0.0148 [−0.0020, +0.0364] arr; CatBoost+A arrivals
хуже на +0.0273 [+0.0074, +0.0488]. Расширение точность не улучшило, что может быть
следствием меньшего числа train-строк на станцию.

### Независимая сверка и ресурсы

- Stdlib oracle пересчитал **4 891 018** строк предсказаний: 0 расхождений метрик.
- Feature oracle из `dense.parquet`: **26 618 336** значений A/B, 0 расхождений
  PIT/значений относительно правила доступности; 0 строк final window во входах.
  Двусторонний EXCEPT ALL подтвердил те же rows.
- Frozen replay: base-варианты cohort64 воспроизвели frozen F4 validation predictions
  **точно**, 8/8 target×model, разница 0.0 на 77306 строках каждой.
- Детерминизм и parity: все 16 input files пересобраны полным batch и one-origin
  запросом в обратном порядке строк, 0 различающихся колонок. Saved-model parity — в
  каждом trial. Production train/serve parity — NOT VERIFIED: у research-признаков нет
  serving-пути. Повторы `protocol` и `run` дали те же id без новых fits.
- 176 success, 0 failed, 0 not_run. Max fit 38.8s, inference 0.28s, worker wall
  43.2s, child RSS 378MB, cumulative worker 3075.5s из 5400s, warnings 0. Это
  shared-host high-water, не OS quota и не SLA. Все 1859 protected hashes целы.

Покрытие и разбивка по station/activity/horizon/season — в JSON и в immutable
`feature_audits/3aafa4f8…`. Доля нулей validation: 40–56% по folds.

Ограничения:

- Prospective pre-registration — FAIL (DoD 2). Production parity и strict historical PIT —
  NOT VERIFIED.
- Те же 4 folds уже применялись в F4, в корректирующем исследовании и в RCA, поэтому это
  исследование внутри validation, а не новая проверка.
- Folds — 4 сезона по 15 дней. Большинство 3/4 не является статистическим доказательством.
- Один seed: variance только между folds, а разброс обучения не измерен, и эффекты
  0.01–0.04 могут быть в его пределах.
- Bootstrap учитывает станции и дни внутри этих окон, но не разброс между сезонами. При 500
  replicates границы шумные, поправки на множественность нет.
- Eligibility зависит от позже наблюдаемых событий (окно 7 дней): это не признак и одинаково
  для всех моделей, но метрики относятся только к наблюдаемым часам.
- Cohort64/128 не citywide, cohort128 содержит cohort64 и использует те же окна. Новый
  independent period и forward/shadow evidence отсутствуют; candidate мог бы быть только
  validation-only.

Promotion требует ML_SPEC gate, strict PIT и независимого forward/shadow evidence,
которых этот шаг не создаёт.

Воспроизведение (локальные Gold inputs, из корня repo):

```sh
uv run --locked --group ml python -m citibike.forecasting.feature_research protocol
uv run --locked --group ml python -m citibike.forecasting.feature_research run --protocol data/f4/feature_protocols/a28aed18d7d2332728be83bd63d5ef88dc3729ec553b08b0065e19c05e484e07
uv run --locked --group ml python -m citibike.forecasting.feature_research verify --result data/f4/feature_outcomes/06288a0089cf4c049253febfd2c6c045215410907a9faa7cbdd4656a8bbf24c0
```

## ML Research & Decision Analytics — 2026-11-02

Отдельная validation-only работа F4. Опубликованный прогноз, champion, selection,
F5 и сентябрьский final не менялись и не использовались. Машинные результаты —
[f4-decision-research.json](f4-decision-research.json); полные trials, predictions,
модели и inputs лежат в ignored `data/decision_research/`.

**Решение: research candidate нет, Seasonal Naive остаётся для обоих targets.**
Все древесные варианты выиграли 4/4 holdout-окна с family-wise интервалом ниже нуля,
но у каждого есть >20% деградация ключевых сегментов. Правило записано и опубликовано
до fits, поэтому этот исход — его прямое применение, а не выбор по результату.

### Протокол и происхождение

- Протокол `31be44f7cf38d6cdbbc81305e4b52c93c839e42078908c2cc91af64e77ae7e04`
  создан 2026-11-02T05:33:37Z и опубликован в commit `6bd5d52` (CI 41394759177
  success) **до первого fit**. Run принимает протокол только при `HEAD = origin/main`
  и id в закоммиченном evidence; outcome хранит этот proof.
- В отличие от `a28aed18`, внешняя отметка (push + CI) предшествует результатам.
  Это всё ещё не слепая предрегистрация: гипотезы написаны после прежних F4 работ, а
  T0–T3 уже трижды оценивались.
- Folds: T0–T3 — прежние окна, только для отбора признаков и параметров. H0–H3 —
  `2025-10-20…11-03`, `2026-01-15…29`, `2026-04-15…29`, `2026-07-15…29`: ни одна
  модель их раньше не оценивала, но они входят в период F2 EDA и в train прежних final
  моделей. Это **unseen validation, не independent test**. Новый независимый период —
  NOT VERIFIED: его нет.
- 872 trials: 312 новых fits на holdout, 400 на reused-окнах, 152 повтора прежних
  исследований на тех же окнах, 8 повторов baseline. Failed 0, not_run 0. Worker
  20 909 s из 32 400, max fit 162.7 s, child RSS ≤ 1.32 GB, 3 workers × 2 threads.

### Readiness и preprocessing

| Cohort | Known-ID events | TV к decile города | Missing dep/arr labels | Suspect closure runs |
|---|---:|---:|---:|---:|
| 64 | 2.56% | 0.188 | 100006 / 98725 | 208 (41 станция) |
| 128 | 4.77% | 0.150 | 293139 / 290647 | 346 (85) |
| 256 | 9.16% | 0.125 | 702762 / 697827 | 615 (180) |

Cohorts вложены (64 ⊂ 128 ⊂ 256), утечек будущих признаков и дублей grain 0. Сборка
cohort256 — 314 s, 2.27 GB RSS. Расширение улучшает состав, но не делает cohort
репрезентативным: доля станций 2-го дециля 19% против 10%.

Правила preprocessing действуют только на train и сравнивались на тех же evaluation
rows: аномальные ID и statistically suspect closures (нулевой run ≥24 h с ожидаемыми
≥13.8 событиями). Ни одно не принято: изменение MAE в пределах 0.25% у LightGBM, хуже
у CatBoost. Пики не обрезаются, NULL не превращается в ноль.

Два дефекта найдены по ходу работы:

1. До заморозки audit показал, что моё первое правило ID относит `6242.1` и `JC032`
   к аномальным. Правило сужено. Позже аналитика показала, что многие `dddd.d` —
   вероятно, отрезанный хвостовой ноль: 105 из 108 коротких ID имеют пару `dddd.d0`,
   на них 1 338 355 прибытий против 265 582 отправлений. Это подозрение на артефакт
   источника, raw-сверка NOT VERIFIED; принятый Silver не менялся.
2. **D1 (после run, независимым verifier):** `cell_std` = 0 вместо NULL при пустой
   истории ячейки, потому что `greatest()` в DuckDB пропускает NULL. Затронуто
   22 779 из 6 904 577 строк, только группа P. P не вошла в выбранные LightGBM и CatBoost;
   hurdle (ACPS) затронут. Исправление меняет хеш кода и требует нового протокола.

### Модели и признаки

Отбор по T0–T3: LightGBM — A+C+S, CatBoost — A, hurdle — A+C+P+S, Poisson — base.
Search (20 LightGBM, 6 CatBoost, 8 hurdle, 5 Poisson α) улучшил LightGBM и Poisson;
CatBoost departures остался bounded.

Holdout H0–H3, macro MAE, Seasonal Naive и лучшие варианты:

| Cohort | Target | Seasonal Naive | LightGBM sel | CatBoost sel | Hurdle | Poisson sel |
|---|---|---:|---:|---:|---:|---:|
| 64 | dep | 1.9990 | 1.3282 | 1.3398 | 1.3157 | 1.5606 |
| 64 | arr | 2.0030 | 1.3317 | 1.3507 | 1.3011 | 1.5426 |
| 128 | dep | 1.9339 | 1.3287 | 1.3313 | 1.3138 | 1.6102 |
| 128 | arr | 1.9412 | 1.2934 | 1.3379 | 1.2961 | 1.6150 |
| 256 | dep | 1.8745 | 1.3084 | 1.2714 | 1.2817 | 1.7499 |
| 256 | arr | 1.8853 | 1.2629 | 1.2894 | 1.2684 | 1.7590 |

Парные ΔMAE против Seasonal Naive, 28 первичных сравнений, Bonferroni α=0.00179,
4000 crossed station×day replicates. Hurdle cohort64 dep −0.683, arr −0.702;
LightGBM −0.671/−0.671; CatBoost+A −0.659/−0.680, family-wise интервалы ниже −0.34.
Poisson: интервалы содержат ноль. Fold sign-flip p = 0.125 — минимум для 4 folds.
Holm-скорректированные bootstrap p деревьев против baseline ≤ 0.006 (ни одного
replicate с другим знаком). Выигрыш над baseline устойчив; разница между деревьями —
нет: hurdle − LightGBM −0.013/−0.031, selected − reference −0.125…+0.007, большинство
интервалов содержат ноль.

Блокеры gate почти всегда одни и те же: H2 (апрель 2026) horizons 5–9, это 01–05 ч
ночи с MAE около 0.3, где +20% — сотые поездки; `weather:heavy_rain` в H2;
`activity:low` в departures. По folds деградаций H2 79, H0 27, H3 21, H1 0 (cohort64).
Правило n≥100 не взвешивает абсолютную величину: это свойство прежнего gate, оно
не ослаблялось.

Ablation на holdout, LightGBM selected: без S +0.021 [+0.008, +0.037] / +0.021
[+0.010, +0.039]; без A +0.052/+0.075 (интервалы >0); без C и с P — inconclusive.
CatBoost: изменения групп inconclusive. Seeds 43/44: размах ≤0.020 у LightGBM,
≤0.007 у CatBoost; hurdle без subsample детерминирован, seed не меняет fit. Если
обнулить P/S для пятой части станций, MAE на них вырастает в 1.1–2.0 раза:
модели зависят от этих колонок, деградация при пропусках сильная.

| Гипотеза | Наблюдение | Статус |
|---|---|---|
| H1 cyclic | T2: LightGBM+A 1.765, +C 1.134 dep; на holdout удаление C inconclusive | Частично |
| H2 profile P | P не выбран; ACPS − ACS −0.013/−0.005, интервалы с нулём; D1 | NOT VERIFIED |
| H3 shrinkage S | Удаление S ухудшает LightGBM на holdout в обоих targets | Согласуется |
| H4 season level | Bias SN −0.23, деревья −0.18…+0.16; season_index NULL в большей части train | Inconclusive |
| H5 hurdle | Хуже LightGBM на `activity:low` и `actual:zero`, лучше на new/peaks | Не согласуется |
| H6 preprocess | LightGBM ≤0.14%, CatBoost dep +0.78% хуже | Частично |
| H7 tuning | LightGBM selected vs reference −2.0%/−1.4%, интервалы с нулём | Согласуется |
| H8 year-over-year | Больше всего деградаций в H2 | Не согласуется |

Cohort256 с 4× train budget: LightGBM 1.247/1.268 против 1.308/1.263 при обычном
бюджете — данных на станцию больше, выигрыш только у departures.

### Decision analytics (fact / prediction / simulation)

Все расчёты из Gold `38f4a4bf…`, независимо сверены с Silver (408 проверок, 0
расхождений) и stdlib. Новых официальных KPI нет.

- Опубликованные поездки: 46 697 958 (2024-10…2025-09) → 45 378 096 (2025-10…2026-09).
  YoY по месяцам −40% (февраль 2026) … +13% (июнь); январь и февраль несопоставимы
  по наблюдённым source-hours, поэтому показан и rate на час.
- Пик будней — 17:00 во всех сезонах; выходные 13–15 ч. Зима ниже лета в 2.4 раза.
- Концентрация: топ-10% ID дают 40–41% событий, Gini 0.60–0.61. 1928 из 2674 ID
  есть во всех 24 месяцах.
- Будние пики: 1350 станций с утренним оттоком и вечерним притоком, 688 наоборот.
  **Все 40 станций топа |net trip flow| за последние 12 месяцев — пары
  `dddd.d`/`dddd.d0`**, и их net почти взаимно гасится (`5980.1` +44 522,
  `5980.10` −43 673). Сырой станционный дисбаланс без склейки таких ID нельзя
  использовать для решений; строки помечены `suspected_id_split`. Маршрутное
  покрытие 99.66%.
- Electric bike 69.6% → 72.3%, member 82.1% → 82.7%; unknown 0. Средняя длительность
  казуальных поездок в 1.5–2.2 раза выше member.
- Ошибки против потоков, Spearman: WAPE~|net share| ρ=0.38 (SN), 0.43 (CatBoost),
  n=64; MAE~events ρ=0.99 — масштаб. Ассоциация, не причинность.
- F5 `99f703b…` (3/64 станции, recorded as-of): средние rejected событий пересчитаны
  из 864 outcomes, 27 проверок без расхождений. Только simulation scope.

### Независимая сверка

Stdlib oracle 21 544 412 строк предсказаний; признаки пересчитаны отдельным pandas
путём на 124 282 386 значений (0 расхождений, кроме измеренного D1); EXCEPT ALL rows,
train availability/embargo, final window 0, saved-model parity в каждом trial;
selection chain пересчитан по правилам; frozen F4 replay 24/24 точно; 3970 protected
hashes целы. Регрессии: F1 3860371, F3 verify, F5 verify — PASS.

Воспроизведение:

```sh
uv run --locked --group ml python -m citibike.decision_research readiness
uv run --locked --group ml python -m citibike.decision_research freeze --readiness data/decision_research/f4/readiness/98a9b692d9a0d1187fb309d5a232f131e1faa2543e283b3a8e75d947a10609d6
uv run --locked --group ml python -m citibike.decision_research run --protocol data/decision_research/f4/protocols/31be44f7cf38d6cdbbc81305e4b52c93c839e42078908c2cc91af64e77ae7e04 --jobs 3
uv run --locked --group ml python -m citibike.decision_research analytics --result data/decision_research/f4/outcomes/b883cb3b06da94567dcc659032e2a8abdae2053242de849e83cc1c2ed860214e
uv run --locked --group ml python -m citibike.decision_research verify --result data/decision_research/f4/outcomes/b883cb3b06da94567dcc659032e2a8abdae2053242de849e83cc1c2ed860214e --analytics data/decision_research/f4/analytics/4082a2fa4cf4545f25a8096f14ae4e910981ba5bb94be80aff3c9ef5e2347c10
```

`run` откажет, если протокол не на `origin/main` или дерево изменено. Повтор
переиспользует immutable trials.

Ограничения: holdout не independent test; fold-level p ≥ 0.125; один cohort-набор
внутри одного города; D1 и ID-split не исправлены; production parity, strict
historical PIT и forward/shadow — NOT VERIFIED; `analytics.py` и `verify.py` вне
code hash протокола и правились после run (D1-учёт, флаг ID-split), их hashes — в JSON.

### Корректирующий остаток: D1 и station ID-split — 2026-11-02

Прежние протокол `31be44f7…`, outcome `b883cb3b…` и audit `726ba3d8…` (evidence
SHA256 `b9f45d2b0c77…`) не менялись. Новый результат записан отдельными immutable
artifacts, JSON выше теперь описывает его и ссылается на прежний audit в `supersedes`.

**D1 исправлен.** `cell_std` снова NULL без истории ячейки, наблюдаемая нулевая
вариация остаётся 0. Схема признаков стала новой версией; модели старой схемы читаются
только без группы P. Корректирующий протокол `ca3b2e63…` опубликован commit
`a29beaf` до fits, CI 41422810574 success. Run `7d15e361…`: 872 success / 0 failed;
588 trials без P воспроизведены сохранёнными моделями с тем же hash предсказаний,
232 trials с P переобучены. Строгий verifier: 0 допущенных расхождений, `cell_std`
NULL в 22 779 строках и истинный 0 в 597 305.

**Влияние D1 на результаты: нулевое.** Все 232 переобученных trials с P дали те же
hash предсказаний, 218 оценок macro MAE не изменились, отбор признаков и параметров,
деградации gate и решение те же. Причина: все 22 779 строк — станции без опубликованной
истории (`history:none`) только в holdout evaluation; в train и в evaluation tuning-окон
NULL нет. Модели учились на тех же данных и отнесли NULL туда же, куда 0. Это
эмпирический факт этого прогона, а не свойство признака: на других данных D1 мог бы
влиять.

**Holdout в train tuning.** В train окна T3 (fit 2026-03-01) 8 166 строк имеют target
внутри H0 (59 станций). Модели, оценки которых на T3 использовались для отбора, частично
обучались на метках H0, поэтому H0 не полностью независим от процедуры отбора. H1–H3
в train tuning-окон не попадают. Новый independent test — NOT VERIFIED.

**Station ID-split: причина в источнике.**

- В raw ZIP строка 161 864 `202410-citibike-tripdata_2.csv`: `end_station_id` =
  `5980.1`, «E 17 St & Broadway», 40.737006/−73.990134. В том же файле есть и
  `5980.10` той же станции.
- Silver читает CSV как `all_varchar`: в 0 строках raw-значение отличается от
  нормализованного. Gold совпадает с Silver по всем 210 ID.
- По всем 105 парам `dddd.d`/`dddd.d0`: одно название станции, координаты расходятся
  меньше чем на 1 м, GBFS регистрирует только `dddd.d0`. Raw ZIP проверен stdlib для
  10 крупнейших пар, 10/10 содержат короткую форму.
- Вывод: опубликованный архив пишет один ID в двух форматах, ошибки преобразования
  нет. Тождество подтверждено lineage, а не похожестью строк, но оператором не
  подтверждено, поэтому Silver, схемы и API не менялись.

Последствия для аналитики. Обе формы всех пар (202 ID в последних 12 месяцах)
исключены из рейтингов дисбаланса, пиков и маршрутов; их фактические значения и
причина — в `excluded_stations`, суммы пар — только диагностика. Исключено 5.76%
отправлений и 5.78% прибытий окна, 5 031 254 поездки по маршрутам. Новый топ
дисбаланса — обычные станции: `3902.06` −3 825, `5730.08` +3 659, `4611.03` −3 564
(дефицит или профицит во всех 12 месяцах). Число наблюдаемых ID в сентябре 2026
2501 → 2336, во всех 24 месяцах 1928 → 1843; Gini 0.613 → 0.611. Тренды, часовые
паттерны и категории не затронуты: city-level суммы не зависят от формы ID. Spearman
WAPE~|net share| 0.376 → 0.389 (SN). F5 и его 3 станции не затронуты.

Последствия для ML. В cohort64 есть `7111.10` и `7910.10`, в cohort128 — 5 ID, в
cohort256 — 15. Их arrivals разделены между двумя формами. Без этих станций выигрыш
деревьев над SN на holdout сохраняется: cohort64 LightGBM −0.684/−0.693, CatBoost
−0.673/−0.670, hurdle −0.697/−0.724, интервалы ниже −0.43. Решение не меняется:
**Seasonal Naive для обоих targets**.

```sh
uv run --locked --group ml python -m citibike.decision_research lineage
uv run --locked --group ml python -m citibike.decision_research freeze --readiness data/decision_research/f4/readiness/98a9b692d9a0d1187fb309d5a232f131e1faa2543e283b3a8e75d947a10609d6 --corrects data/decision_research/f4/outcomes/b883cb3b06da94567dcc659032e2a8abdae2053242de849e83cc1c2ed860214e
uv run --locked --group ml python -m citibike.decision_research run --protocol data/decision_research/f4/protocols/ca3b2e6341fcf253591be448bbb524a3261df33796247a47d7468d6c38726755 --jobs 3
uv run --locked --group ml python -m citibike.decision_research analytics --result data/decision_research/f4/outcomes/7d15e3615cf647a274d07136769a65495a658e845f89bf105c6faa18b67b422a --lineage data/decision_research/f4/lineage/5b58f2efcf834dd2a8eabb1461f26a23464e364fbb06040abeda27813137cded
uv run --locked --group ml python -m citibike.decision_research verify --result data/decision_research/f4/outcomes/7d15e3615cf647a274d07136769a65495a658e845f89bf105c6faa18b67b422a --analytics data/decision_research/f4/analytics/c9f1d7387a29cc496691d710a282669db6b2ad375716073f8fa4f618a404774e
```

`checked_protocol` прежнего `31be44f7` на HEAD теперь отказывает намеренно: code hash
изменился. Его код воспроизводится из `checkpoint/F4-20261102-4840536`.

## Корректирующий validation-only protocol

Protocol `8729cb7cc4aac8efbe3ddda02c6bc972804c69fbf365b9a9ce35093f2d1a6050`, saved 2026-10-30T04:00:01.602462+00:00 before new trials. Dataset `d9b87571006b8367d0d286fd77ec9b2e7cac0900a773eae599fc7c3813bc12e5`. Run `b6565ab502b14bcd7481dd25362ea6fabb63a39caaf99364e6f65389c1fb8618`; mode **approximate retrospective**.

Предыдущие разделы — прежний frozen evaluation. Этот раздел содержит только новые validation trials; Sep2026 final scores не участвовали в новом сравнении. Existing selection/model/forecast IDs и serving сохранены.

Outcomes: `{'success': 138, 'failed': 0, 'not_run': 0}`. Resources: `{'invocation_seconds': 2288.375, 'cumulative_worker_seconds': 2286.05700000003, 'parent_peak_rss_bytes': 398503936}`. Per-trial native process RSS/wall/parameters/status/error/seed/input versions сохранены в immutable trials; компактные ссылки/checksums и результаты находятся в F4 JSON.

### Bounded search и ablation

Macro metrics — невзвешенное среднее по validation folds. N по каждому fold явен в JSON; пропущенный/нулевой actual denominator WAPE остаётся N/A. Counts0/1–4/5–19/20+ заданы до fits по принятому F2 EDA; weekday peak-hour и count>=20 — разные диагностические определения.

| Target | Configuration | Macro MAE | WAPE | RMSE | Bias | Wins | >20% degraded key slices |
|---|---|---:|---:|---:|---:|---:|---:|
| departures | seasonal_naive | 1.7544 | 0.8062 | 3.3839 | -0.1891 | 0/4 | 0 |
| departures | poisson_default | 2.0736 | 0.9241 | 3.7020 | 0.0541 | 1/4 | 64 |
| departures | lightgbm_default | 1.5960 | 0.7096 | 3.0166 | -0.4003 | 3/4 | 12 |
| departures | catboost_default | 1.5199 | 0.6646 | 2.9129 | -0.5455 | 3/4 | 10 |
| departures | poisson_bounded | 1.7683 | 0.7807 | 3.5431 | -0.3183 | 1/4 | 41 |
| departures | lightgbm_bounded | 1.5665 | 0.6919 | 2.9478 | -0.4356 | 3/4 | 12 |
| departures | catboost_bounded | 1.5245 | 0.6684 | 2.8980 | -0.4643 | 3/4 | 14 |
| departures | poisson_bounded__without_calendar | 3.2509 | 1.5353 | 6.2064 | 1.9562 | 0/4 | 118 |
| departures | poisson_bounded__without_station_identity | 1.7801 | 0.7692 | 3.3562 | -0.6223 | 2/4 | 42 |
| departures | poisson_bounded__without_demand_profile | 2.0898 | 0.9110 | 3.8677 | -0.7231 | 1/4 | 72 |
| departures | lightgbm_bounded__without_calendar | 1.7784 | 0.8162 | 3.3689 | -0.1591 | 2/4 | 22 |
| departures | lightgbm_bounded__without_station_identity | 1.5932 | 0.7064 | 2.9807 | -0.4016 | 3/4 | 15 |
| departures | lightgbm_bounded__without_demand_profile | 1.5933 | 0.6980 | 3.0782 | -0.5249 | 3/4 | 14 |
| departures | catboost_bounded__without_calendar | 1.7849 | 0.8209 | 3.3345 | -0.0061 | 2/4 | 19 |
| departures | catboost_bounded__without_station_identity | 1.5394 | 0.6733 | 2.9290 | -0.4895 | 3/4 | 15 |
| departures | catboost_bounded__without_demand_profile | 1.6176 | 0.7076 | 3.1324 | -0.5336 | 3/4 | 16 |
| arrivals | seasonal_naive | 1.7479 | 0.8048 | 3.4448 | -0.1864 | 0/4 | 0 |
| arrivals | poisson_default | 2.0973 | 0.9338 | 3.8516 | 0.0442 | 1/4 | 67 |
| arrivals | lightgbm_default | 1.5709 | 0.6960 | 3.0330 | -0.3858 | 3/4 | 15 |
| arrivals | catboost_default | 1.5067 | 0.6580 | 2.9616 | -0.5780 | 3/4 | 11 |
| arrivals | poisson_bounded | 1.7821 | 0.7846 | 3.6279 | -0.3582 | 1/4 | 48 |
| arrivals | lightgbm_bounded | 1.5684 | 0.6941 | 2.9967 | -0.4100 | 3/4 | 16 |
| arrivals | catboost_bounded | 1.4957 | 0.6545 | 2.9051 | -0.4825 | 3/4 | 14 |
| arrivals | poisson_bounded__without_calendar | 3.3204 | 1.5665 | 6.3092 | 1.9797 | 0/4 | 123 |
| arrivals | poisson_bounded__without_station_identity | 1.7955 | 0.7747 | 3.5039 | -0.6493 | 2/4 | 49 |
| arrivals | poisson_bounded__without_demand_profile | 2.1036 | 0.9168 | 3.9610 | -0.7216 | 1/4 | 77 |
| arrivals | lightgbm_bounded__without_calendar | 1.7816 | 0.8193 | 3.4500 | -0.1466 | 2/4 | 27 |
| arrivals | lightgbm_bounded__without_station_identity | 1.5878 | 0.7067 | 3.0275 | -0.3506 | 3/4 | 20 |
| arrivals | lightgbm_bounded__without_demand_profile | 1.5935 | 0.6996 | 3.1400 | -0.5187 | 3/4 | 11 |
| arrivals | catboost_bounded__without_calendar | 1.7787 | 0.8212 | 3.3902 | -0.0333 | 2/4 | 22 |
| arrivals | catboost_bounded__without_station_identity | 1.5172 | 0.6637 | 2.9507 | -0.4946 | 3/4 | 17 |
| arrivals | catboost_bounded__without_demand_profile | 1.6180 | 0.7110 | 3.1978 | -0.5076 | 3/4 | 15 |

Ablation сравнивает каждый bounded algorithm с тем же algorithm без одной группы на идентичных evaluation rows. horizon_h сохранён. Seasonal Naive независим: удаление определяющих profile/station/calendar связей меняет его семантику, поэтому baseline ablation неприменима. Profiles косвенно кодируют station/calendar; это сравнение колонок, не причинный вклад информации.

| Target | Removed group / model | ΔMAE по folds (ablated − full) |
|---|---|---|
| departures | poisson_bounded__without_calendar | +0.0230, +1.9463, +2.9521, +1.0089 |
| departures | poisson_bounded__without_station_identity | +0.0568, +0.1894, -0.0322, -0.1668 |
| departures | poisson_bounded__without_demand_profile | +0.2721, +0.6301, +0.2933, +0.0905 |
| departures | lightgbm_bounded__without_calendar | -0.0035, +0.0731, +0.5063, +0.2715 |
| departures | lightgbm_bounded__without_station_identity | +0.0611, -0.0287, +0.0576, +0.0169 |
| departures | lightgbm_bounded__without_demand_profile | -0.1088, +0.2308, -0.0798, +0.0651 |
| departures | catboost_bounded__without_calendar | -0.0108, +0.0888, +0.7951, +0.1685 |
| departures | catboost_bounded__without_station_identity | +0.0325, +0.0244, +0.0297, -0.0271 |
| departures | catboost_bounded__without_demand_profile | -0.0907, +0.3102, +0.0643, +0.0887 |
| arrivals | poisson_bounded__without_calendar | +0.0210, +2.0329, +3.0571, +1.0423 |
| arrivals | poisson_bounded__without_station_identity | +0.0491, +0.1900, -0.0357, -0.1500 |
| arrivals | poisson_bounded__without_demand_profile | +0.2404, +0.6363, +0.2894, +0.1195 |
| arrivals | lightgbm_bounded__without_calendar | +0.0016, +0.0649, +0.5135, +0.2727 |
| arrivals | lightgbm_bounded__without_station_identity | +0.0453, -0.0476, +0.0390, +0.0411 |
| arrivals | lightgbm_bounded__without_demand_profile | -0.1180, +0.2350, -0.0823, +0.0658 |
| arrivals | catboost_bounded__without_calendar | +0.0030, +0.0680, +0.8758, +0.1853 |
| arrivals | catboost_bounded__without_station_identity | +0.0433, +0.0141, +0.0379, -0.0093 |
| arrivals | catboost_bounded__without_demand_profile | -0.1167, +0.3408, +0.1500, +0.1151 |

### Stress, ошибки и стабильность

Stress candidate выбран только по validation majority/resource gates и macro MAE среди прежних bounded configurations; parameter search не расширялся. Train уменьшен до половины фактических полных station/origin blocks. Evaluation population неизменна.

| Target | Candidate | Fold | Train full/half | MAE full/half | ΔMAE |
|---|---|---:|---:|---:|---:|
| departures | catboost_bounded | 0 | 49560/24768 | 2.1389/2.1398 | +0.0008 |
| departures | catboost_bounded | 1 | 79992/39984 | 1.6360/1.6715 | +0.0355 |
| departures | catboost_bounded | 2 | 79992/39984 | 1.2531/1.2434 | -0.0097 |
| departures | catboost_bounded | 3 | 79992/39984 | 1.0699/1.0375 | -0.0325 |
| arrivals | catboost_bounded | 0 | 49560/24768 | 2.1437/2.1731 | +0.0294 |
| arrivals | catboost_bounded | 1 | 79992/39984 | 1.5907/1.6182 | +0.0275 |
| arrivals | catboost_bounded | 2 | 79992/39984 | 1.1917/1.2277 | +0.0360 |
| arrivals | catboost_bounded | 3 | 79992/39984 | 1.0567/1.0235 | -0.0332 |

### Zero/nonzero, rare и peak errors

Pooled metrics ниже пересчитаны из сумм ошибок, квадратов ошибок и actual по четырём folds; это другой знаменатель, чем macro scores выше. Zero WAPE не определена. Показаны baseline и validation score leader.

| Target | Model | Slice | N | MAE | WAPE | RMSE | Bias |
|---|---|---|---:|---:|---:|---:|---:|
| departures | catboost_default | actual:zero | 37217 | 0.5416 | N/A | 1.0264 | 0.5416 |
| departures | catboost_default | actual:nonzero | 40089 | 2.3764 | 0.5337 | 4.0416 | -1.4428 |
| departures | catboost_default | actual:rare_1_4 | 28053 | 1.2168 | 0.6430 | 1.6966 | -0.1865 |
| departures | catboost_default | actual:middle_5_19 | 10984 | 4.1759 | 0.4692 | 5.1901 | -3.4110 |
| departures | catboost_default | actual:peak_ge20 | 1052 | 14.5110 | 0.5522 | 16.2622 | -14.3929 |
| departures | seasonal_naive | actual:zero | 37217 | 0.5887 | N/A | 1.5636 | 0.5887 |
| departures | seasonal_naive | actual:nonzero | 40089 | 2.8312 | 0.6359 | 4.4814 | -0.7960 |
| departures | seasonal_naive | actual:rare_1_4 | 28053 | 1.7529 | 0.9263 | 2.7405 | 0.1149 |
| departures | seasonal_naive | actual:middle_5_19 | 10984 | 4.7145 | 0.5297 | 5.9338 | -2.1367 |
| departures | seasonal_naive | actual:peak_ge20 | 1052 | 11.9240 | 0.4538 | 14.0500 | -11.0856 |
| arrivals | catboost_bounded | actual:zero | 37594 | 0.5225 | N/A | 1.0144 | 0.5225 |
| arrivals | catboost_bounded | actual:nonzero | 39712 | 2.3624 | 0.5268 | 4.0827 | -1.3137 |
| arrivals | catboost_bounded | actual:rare_1_4 | 27870 | 1.2552 | 0.6728 | 1.7787 | -0.1394 |
| arrivals | catboost_bounded | actual:middle_5_19 | 10735 | 3.9958 | 0.4457 | 5.0570 | -3.0226 |
| arrivals | catboost_bounded | actual:peak_ge20 | 1107 | 14.3987 | 0.5342 | 16.4411 | -14.3061 |
| arrivals | seasonal_naive | actual:zero | 37594 | 0.5680 | N/A | 1.5231 | 0.5680 |
| arrivals | seasonal_naive | actual:nonzero | 39712 | 2.8595 | 0.6377 | 4.6041 | -0.7843 |
| arrivals | seasonal_naive | actual:rare_1_4 | 27870 | 1.7261 | 0.9252 | 2.7280 | 0.0861 |
| arrivals | seasonal_naive | actual:middle_5_19 | 10735 | 4.8134 | 0.5369 | 6.1003 | -1.9950 |
| arrivals | seasonal_naive | actual:peak_ge20 | 1107 | 12.4472 | 0.4618 | 14.5676 | -10.9566 |

### Крупные station/hour ошибки

Top groups относятся к одному указанному validation fold, не ко всей городской сети и не к pooled station ranking.

| Target | Model | Field/key | Fold | N | MAE | Bias |
|---|---|---|---:|---:|---:|---:|
| departures | catboost_default | station_id:6753.08 | 0 | 336 | 11.3028 | -11.2796 |
| departures | catboost_default | station_id:6230.02 | 0 | 336 | 8.6364 | -8.5027 |
| departures | catboost_default | station_id:5703.13 | 0 | 336 | 6.1165 | -5.9768 |
| departures | catboost_default | local_hour:18 | 0 | 728 | 4.3758 | -4.2044 |
| departures | catboost_default | local_hour:17 | 0 | 728 | 4.2170 | -4.0196 |
| departures | catboost_default | local_hour:19 | 0 | 728 | 3.4596 | -3.2896 |
| departures | seasonal_naive | station_id:6753.08 | 0 | 336 | 6.9732 | -6.4196 |
| departures | seasonal_naive | station_id:6753.08 | 2 | 336 | 6.5417 | 5.6369 |
| departures | seasonal_naive | station_id:7288.04 | 2 | 336 | 6.3631 | 5.9881 |
| departures | seasonal_naive | local_hour:18 | 0 | 728 | 3.6470 | -3.1580 |
| departures | seasonal_naive | local_hour:18 | 2 | 883 | 3.6149 | 2.8924 |
| departures | seasonal_naive | local_hour:17 | 2 | 883 | 3.6002 | 2.8822 |
| arrivals | catboost_bounded | station_id:6753.08 | 0 | 336 | 11.7084 | -11.6866 |
| arrivals | catboost_bounded | station_id:6230.02 | 0 | 336 | 8.9746 | -8.8568 |
| arrivals | catboost_bounded | station_id:5703.13 | 0 | 336 | 6.7881 | -6.5829 |
| arrivals | catboost_bounded | local_hour:18 | 0 | 728 | 4.8099 | -4.6360 |
| arrivals | catboost_bounded | local_hour:17 | 0 | 728 | 4.6614 | -4.4545 |
| arrivals | catboost_bounded | local_hour:19 | 0 | 728 | 3.6522 | -3.4692 |
| arrivals | seasonal_naive | station_id:6753.08 | 0 | 336 | 6.8690 | -6.4940 |
| arrivals | seasonal_naive | station_id:6753.08 | 2 | 336 | 6.4940 | 5.6667 |
| arrivals | seasonal_naive | station_id:7288.04 | 2 | 336 | 6.3095 | 6.1488 |
| arrivals | seasonal_naive | local_hour:17 | 2 | 883 | 3.9241 | 3.2854 |
| arrivals | seasonal_naive | local_hour:18 | 2 | 883 | 3.8211 | 3.2118 |
| arrivals | seasonal_naive | local_hour:18 | 1 | 793 | 3.7503 | -1.9142 |

Validation JSON содержит zero/nonzero/rare/peak, low/high/new-known slices, top station/hour MAE/bias и largest residuals для каждого trial; fold MAE/bias/min/max/std и coverage, count-distribution TV между validation окнами. Cohort vs F2 city monthly TV — разные populations и длины окон; это composition diagnostic, не causal drift и не citywide точность. Отсутствующие slices не заполнены нулями.

| Target | Lowest-score configuration | Passed research gates | Operational |
|---|---|---|---|
| departures | catboost_default | seasonal_naive | Seasonal Naive fallback; promotion NOT VERIFIED |
| arrivals | catboost_bounded | seasonal_naive | Seasonal Naive fallback; promotion NOT VERIFIED |

### Проверяемые выводы

- departures: lowest validation macro MAE 1.5199 (catboost_default) против baseline 1.7544; fold range [1.0650,2.1118], std 0.4075. Majority 3/4; degraded key slices 10. Research gate candidate: seasonal_naive; это не смена опубликованного champion.
- departures actual:zero: leader MAE 0.5416, baseline 0.5887, N=37217; leader bias +0.5416. Положительный bias означает overprediction, редкий peak slice не доказывает переносимость на всю сеть.
- departures actual:peak_ge20: leader MAE 14.5110, baseline 11.9240, N=1052; leader bias -14.3929. Положительный bias означает overprediction, редкий peak slice не доказывает переносимость на всю сеть.
- arrivals: lowest validation macro MAE 1.4957 (catboost_bounded) против baseline 1.7479; fold range [1.0567,2.1437], std 0.4225. Majority 3/4; degraded key slices 14. Research gate candidate: seasonal_naive; это не смена опубликованного champion.
- arrivals actual:zero: leader MAE 0.5225, baseline 0.5680, N=37594; leader bias +0.5225. Положительный bias означает overprediction, редкий peak slice не доказывает переносимость на всю сеть.
- arrivals actual:peak_ge20: leader MAE 14.3987, baseline 12.4472, N=1107; leader bias -14.3061. Положительный bias означает overprediction, редкий peak slice не доказывает переносимость на всю сеть.

Research gate требует majority wins, отсутствие >20% деградации значимых key segments, ресурсы и fit warnings. Низкий средний score сам по себе не проходит эти gates. Strict publication availability и forward/shadow evidence отсутствуют: operational uplift не доказан. Baseline/unavailable fallback сохранён; новые final fits/reselection и переключение опубликованного champion не выполнялись.

### Независимая сверка / воспроизводимость

Independent audit: `{'prediction_rows': 2663114, 'diagnostic_groups': 2760, 'pooled_slice_groups': 20, 'metric_mismatches': 0, 'row_membership_mismatches': 0, 'PIT_mismatches': 0, 'saved_model_parity_sample_rows_per_trial': 32, 'frozen_files_unchanged': True}`; repeat fits: `[{'prediction_type': 'departures', 'exact_prediction_values_repeat': True}, {'prediction_type': 'arrivals', 'exact_prediction_values_repeat': True}]`.
Метрики пересчитаны stdlib oracle по всем prediction rows; station/hour diagnostics — отдельным SQL; row membership — двусторонним EXCEPT ALL; PIT/embargo — SQL; сохранённые модели заново загружены и сверены с prediction samples. Все failures сохраняют error/status и input hashes. Controlled failed/timeout/changed-population tests — отдельные fixture доказательства механизма, не вымышленные failed real fits.

```sh
uv run --locked --group ml python -m citibike.forecasting protocol
uv run --locked --group ml python -m citibike.forecasting research --protocol data/f4/research_protocols/8729cb7cc4aac8efbe3ddda02c6bc972804c69fbf365b9a9ce35093f2d1a6050
uv run --locked --group ml python -m citibike.forecasting verify --serving
```
`--protocol` принимает directory `data/f4/research_protocols/8729cb7cc4aac8efbe3ddda02c6bc972804c69fbf365b9a9ce35093f2d1a6050`. Повтор research переиспользует immutable outcomes, включая failures. Ресурсы — shared-host OS process high-water; RSS gate не OS memory quota и не production SLA.

Ограничения: Cohort64, about 2.56% known-ID events; not citywide; New validation diagnostics do not open/reselect by final test; F2 city station-hour and F4 daily-origin cohort populations differ; Source availability +45d is a retrospective assumption, not historical fact; Weather is outcome-only after prediction; no GBFS/capacity/short lag added.
