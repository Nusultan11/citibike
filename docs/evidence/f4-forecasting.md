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

Real strict batch `6de557fca70088054b3a27a6cb2fe143a5f4be0970f46f78d5938593775bb445`: origin `2026-10-29T11:19:03.380944+00:00`, issued `2026-10-29T11:19:09.402804+00:00`, data_as_of `2026-10-01T04:00:00+00:00` = source event window end. 64×24×2 = 3072 rows: 3024 finite nonnegative + 48 unavailable (одна stale-history station). Feature/source/model/dataset versions и availability проверены; unavailable не замаскирован нулём. Полная сверка artifact→PG→HTTP: 3072/3072/3072, 0 mismatches. Повтор publish сохраняет ID; в disposable PostgreSQL проверены COPY/activation failure, идемпотентность, retained version pin и rollback. CHECK NULL defect воспроизведён и исправлен новой migration005 без изменения применённой004. API runtime не содержит ML libraries.

| Phase | Wall/elapsed s | OS peak RSS bytes |
| --- | --- | --- |
| dataset | 90.0630 | 1461096448 |
| validation | 971.5000 | 590106624 |
| final_test | 247.7340 | 518615040 |
| inference | 6.0470 | 201498624 |

Полный CLI predict+publish на втором acceptance batch: **8.2651219s** (первый batch 11.7243199s). Замер включает запуск двух CLI, не training/backtest. Оба <300s для данного cohort; это не citywide SLA. RSS — process high-water measurement, не изолированная estimator memory. Shared host / sequential runs, concurrent-load SLA — NOT VERIFIED.

Fresh F1 raw→UI regression: [f4-f1-regression.json](f4-f1-regression.json), все 8 stage values 3860371. Final local tests **92 passed**, один существующий StarletteDeprecationWarning; Ruff/format/mypy (92 files), locked sync, package build, frontend npm ci/lint/typecheck/build, Docker/HTTP проходят. Фактические SHA и CI фиксируются в ROADMAP §6 после push. MLflow Registry automation — F7; здесь immutable local manifests, не Registry. Prediction intervals не заявлены; F5–F8 не реализуются.
