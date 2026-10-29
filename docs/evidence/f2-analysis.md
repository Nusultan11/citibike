# F2 — аналитический разбор реальной истории

Охват NYC: 2024-10 — 2026-09; 24 полных месяцев.
Вход: 92,085,994; канонических поездок: 92,076,567.
Детальные машинные профили — локальные артефакты pipeline; компактное evidence содержит hashes, версии, исключения, drift и GBFS timestamps.

| Архив NYC | CSV | Вход | Канонические | Исключения | Пропущенные часы |
|---|---:|---:|---:|---:|---:|
| 2024-10 | 6 | 5150054 | 5150054 | 0 | 0 |
| 2024-11 | 4 | 3710134 | 3706077 | 4057 | 2 |
| 2024-12 | 3 | 2311171 | 2311171 | 0 | 0 |
| 2025-01 | 3 | 2124475 | 2124475 | 0 | 0 |
| 2025-02 | 3 | 2031257 | 2031257 | 0 | 0 |
| 2025-03 | 4 | 3168271 | 3168271 | 0 | 0 |
| 2025-04 | 4 | 3724596 | 3724596 | 0 | 0 |
| 2025-05 | 5 | 4325553 | 4325553 | 0 | 0 |
| 2025-06 | 5 | 4759345 | 4759345 | 0 | 0 |
| 2025-07 | 5 | 4988053 | 4988053 | 0 | 0 |
| 2025-08 | 6 | 5121533 | 5121533 | 0 | 0 |
| 2025-09 | 6 | 5287447 | 5287447 | 0 | 0 |
| 2025-10 | 5 | 4731900 | 4731900 | 0 | 0 |
| 2025-11 | 4 | 3417052 | 3412223 | 4829 | 2 |
| 2025-12 | 3 | 2091620 | 2091620 | 0 | 0 |
| 2026-01 | 2 | 1816391 | 1816391 | 0 | 18 |
| 2026-02 | 2 | 1219444 | 1219444 | 0 | 32 |
| 2026-03 | 3 | 2945753 | 2945753 | 0 | 0 |
| 2026-04 | 4 | 3860371 | 3860342 | 29 | 0 |
| 2026-05 | 5 | 4694878 | 4694366 | 512 | 0 |
| 2026-06 | 6 | 5384468 | 5384468 | 0 | 0 |
| 2026-07 | 5 | 4993137 | 4993137 | 0 | 0 |
| 2026-08 | 6 | 5246236 | 5246236 | 0 | 0 |
| 2026-09 | 5 | 4982855 | 4982855 | 0 | 0 |

Режим данных: real_official.
Максимальный входной объём: 2026-06; минимальный: 2026-02.
Границы длительности: 4.966 — 877764.51 секунд; пики сохранены с флагами.
Покрытие маршрутов на базовом canonical слое: 91767226 / 92077108.
Календарный пик объёма (NY weekday/hour, Sunday=0): [2, 17]; пропущенных UTC hour buckets: 54.
Raw null rates: {'ride_id': 0.0, 'rideable_type': 0.0, 'started_at': 0.0, 'ended_at': 0.0, 'start_station_name': 0.0005290381075758383, 'start_station_id': 0.0005290381075758383, 'end_station_name': 0.00272980709748325, 'end_station_id': 0.0029532938527003358, 'start_lat': 0.0005290381075758383, 'start_lng': 0.0005290381075758383, 'end_lat': 0.0029477338323567427, 'end_lng': 0.0029477338323567427, 'member_casual': 0.0}.

Сезонность и покрытие

Объёмы по месяцам, NY-дням и часам недели измерены в профилях; различия не объявляются причинным эффектом. Пики длительности и поездки вне широкого описательного NYC bounding box не удаляются автоматически.
Station/route coverage ограничено отсутствующими ID и координатами. Доли категорий сравниваются только в современной сопоставимой схеме; новые категории явно перечислены в drift.
Временные пробелы считаются отсутствием наблюдений. Публикационная задержка по Last-Modified может отражать переиздание, поэтому не доказывает исходную train-time availability.

Качество и независимость

Для каждого CSV сохранён итоговый статус каждой строки; отдельный CSV/ZoneInfo/SQLite расчёт сверяет row-decision SHA и метрики. Межмесячные решения дополнительно сверяются по независимым raw fingerprints. Exact duplicates канонизируются, конфликтующие ID изолируются.
Итоговые dispositions: {'accepted': 92076567, 'invalid': 8886, 'conflicting_id': 58, 'duplicate_exact': 483}. Cross-source: {'cross_source_ride_ids': 512, 'changed_row_counts': {'duplicate_exact': 483, 'conflicting_id': 58}, 'independent_row_mismatches': 0, 'elapsed_seconds': 143.172}.
Reason flags могут пересекаться; их сумма не равна числу исключённых строк.
Межархивная коррекция accepted rows: 2026-04: 3860371 → 3860342; 2026-05: 4694878 → 4694366. Числа в таблице относятся к source partitions, не к витрине F3.

GBFS и исторические соединения

Настоящих collections: 7; station-history join: {'station_history_available': True, 'matched_start_rows': 0, 'multiple_matches': 0, 'tested_rows': 92076567, 'rule': 'trip station alias plus event timestamp within [valid_from,valid_to); no present-day backfill'}.
История начинается с реально полученных наблюдений и не достраивается в прошлое; TTL gaps и stale выделены. Характеристики текущих станций не присваиваются поездкам прошлых месяцев.
Weather отсутствует: соединение не выполнялось. F4 использует отдельные retrospective experiments и ERA5 outcome labels; историческая публикационная availability остаётся неизвестной.

Измерения и ограничения

Публикация: {'elapsed_seconds': 1693.906, 'peak_rss_bytes': 2114560000, 'rebuilt_partitions': 24, 'output_bytes': 19579714879}. Архивные workers отдельно записывают wall time, OS peak RSS и output bytes. Повторы и incremental runs записаны в evidence.
Первый запуск включал failed attempts и cached recovery; чистый cold end-to-end benchmark не измерен. RSS относится к отдельным процессам; общий concurrent RAM не измерялся. Две ранние подготовки не имеют отдельного base-phase RSS; сохранён RSS соответствующих attempts. Engine memory limit не равен OS peak RSS.
Результаты относятся к выбранным источникам и наблюдённым GBFS-сборам; они не доказывают скрытый спрос, круглосуточную доступность или production SLA.

Источники: [официальный inventory](https://s3.amazonaws.com/tripdata), [GBFS discovery](https://gbfs.citibikenyc.com/gbfs/2.3/gbfs.json), [GBFS 2.3](https://github.com/MobilityData/gbfs/blob/v2.3/gbfs.md), [Data Sharing Policy](https://citibikenyc.com/data-sharing-policy).

## Исследовательский EDA / ML-readiness

Версия f2-research-1; effective Silver snapshot `5c4f166d01265d219f7026db9195f3d4cdb362e1f65c1625713628aa55a26cea`. Event window NY: 2024-10…2026-09; 2674 наблюдаемых IDs; 1928 во всех месяцах.
Grain: station ID × UTC hour × departures/arrivals. IDs не равны установкам или active_stations. Все доли и квантили используют явно eligible labels.
Ноль: городской source-hour наблюдён, station event любого типа не далее 7 дней до и после target в том же NY месяце. Будущее bracketing применяется только к оценке label; features его не получают. Source gaps и длинные/краевые station gaps остаются missing; отсутствие поездок не доказывает закрытие станции.

| Месяц | Target | Eligible / candidate | Zero | Source gap | Station gap | Mean | Stable mean |
|---|---|---:|---:|---:|---:|---:|---:|
| 2024-10 | arrivals | 1651744 / 1989456 | 725774 | 0 | 337712 | 3.1084998643857644 | 3.1552467808890174 |
| 2024-10 | departures | 1651744 / 1989456 | 717740 | 0 | 337712 | 3.115576626886491 | 3.1850352745494654 |
| 2024-11 | arrivals | 1586556 / 1927954 | 790588 | 5348 | 336050 | 2.328616197600337 | 2.357026616796829 |
| 2024-11 | departures | 1586556 / 1927954 | 780876 | 5348 | 336050 | 2.333906272454297 | 2.3852152918268903 |
| 2024-12 | arrivals | 1592509 / 1989456 | 915503 | 0 | 396947 | 1.4465764400703545 | 1.4223761719659398 |
| 2024-12 | departures | 1592509 / 1989456 | 912692 | 0 | 396947 | 1.4505343454887853 | 1.4363226548335437 |
| 2025-01 | arrivals | 1624102 / 1989456 | 965561 | 0 | 365354 | 1.3054309396823598 | 1.299269228762005 |
| 2025-01 | departures | 1624102 / 1989456 | 956393 | 0 | 365354 | 1.307785471602153 | 1.3082737521809593 |
| 2025-02 | arrivals | 1468058 / 1796928 | 875241 | 0 | 328870 | 1.3803814290716034 | 1.3726694488235727 |
| 2025-02 | departures | 1468058 / 1796928 | 869317 | 0 | 328870 | 1.3832702795121175 | 1.386220354951557 |
| 2025-03 | arrivals | 1653364 / 1986782 | 887375 | 0 | 333418 | 1.911144188454569 | 1.9251558429360265 |
| 2025-03 | departures | 1653364 / 1986782 | 876335 | 0 | 333418 | 1.9154336250214714 | 1.945695966621681 |
| 2025-04 | arrivals | 1591362 / 1925280 | 794521 | 0 | 333918 | 2.3344103981369417 | 2.331003452539249 |
| 2025-04 | departures | 1591362 / 1925280 | 792775 | 0 | 333918 | 2.339908204418605 | 2.3633119590827443 |
| 2025-05 | arrivals | 1635043 / 1989456 | 762629 | 0 | 354413 | 2.6377453070041583 | 2.6037961388134847 |
| 2025-05 | departures | 1635043 / 1989456 | 757899 | 0 | 354413 | 2.644705980209695 | 2.6444136414322883 |
| 2025-06 | arrivals | 1568258 / 1925280 | 660077 | 0 | 357022 | 3.0239935010693393 | 2.9649247649815185 |
| 2025-06 | departures | 1568258 / 1925280 | 666629 | 0 | 357022 | 3.033457505078884 | 3.0119815768794203 |
| 2025-07 | arrivals | 1623457 / 1989456 | 676837 | 0 | 365999 | 3.0599233610745467 | 3.0192004073282517 |
| 2025-07 | departures | 1623457 / 1989456 | 682491 | 0 | 365999 | 3.0703424852028727 | 3.0601990648047153 |
| 2025-08 | arrivals | 1605452 / 1989456 | 658844 | 0 | 384004 | 3.182080186763603 | 3.120736209647498 |
| 2025-08 | departures | 1605452 / 1989456 | 665168 | 0 | 384004 | 3.1891716476107663 | 3.171343040176512 |
| 2025-09 | arrivals | 1559121 / 1925280 | 652696 | 0 | 366159 | 3.3829946489079425 | 3.3309248807295653 |
| 2025-09 | departures | 1559121 / 1925280 | 659426 | 0 | 366159 | 3.389754226900927 | 3.39478732167651 |
| 2025-10 | arrivals | 1608745 / 1989456 | 724654 | 0 | 380711 | 2.933290857158841 | 2.9165488852605184 |
| 2025-10 | departures | 1608745 / 1989456 | 725492 | 0 | 380711 | 2.93969212025523 | 2.9456869503827368 |
| 2025-11 | arrivals | 1556231 / 1927954 | 782210 | 5348 | 366375 | 2.1870082269277504 | 2.1933457229766447 |
| 2025-11 | departures | 1556231 / 1927954 | 781813 | 5348 | 366375 | 2.1911400042795703 | 2.221695625812293 |
| 2025-12 | arrivals | 1616413 / 1989456 | 975661 | 0 | 373043 | 1.2906175587550954 | 1.2889066167319265 |
| 2025-12 | departures | 1616413 / 1989456 | 973984 | 0 | 373043 | 1.2933322115078263 | 1.3105617374107057 |
| 2026-01 | arrivals | 1567824 / 1989456 | 997700 | 29414 | 392218 | 1.1529304309667412 | 1.1565127537194835 |
| 2026-01 | departures | 1553026 / 1989456 | 979416 | 48132 | 388298 | 1.168660408776157 | 1.1790294623533306 |
| 2026-02 | arrivals | 1389581 / 1796928 | 938748 | 48132 | 359215 | 0.8717635028112791 | 0.8790479870769577 |
| 2026-02 | departures | 1358823 / 1796928 | 906181 | 85568 | 352537 | 0.8967974489687031 | 0.9085183853581931 |
| 2026-03 | arrivals | 1649064 / 1986782 | 904411 | 0 | 337718 | 1.7806998394240612 | 1.7937454333931573 |
| 2026-03 | departures | 1649064 / 1986782 | 904600 | 0 | 337718 | 1.7850611013277835 | 1.819701494654853 |
| 2026-04 | arrivals | 1623049 / 1925280 | 812533 | 0 | 302231 | 2.3717041198386495 | 2.3987129403228047 |
| 2026-04 | departures | 1623049 / 1925280 | 812464 | 0 | 302231 | 2.3772560163001857 | 2.4485034350431842 |
| 2026-05 | arrivals | 1667336 / 1989456 | 760799 | 0 | 322120 | 2.8088519650508355 | 2.8615960890030676 |
| 2026-05 | departures | 1667336 / 1989456 | 765278 | 0 | 322120 | 2.814405134897825 | 2.904061701582258 |
| 2026-06 | arrivals | 1640220 / 1925280 | 686032 | 0 | 285060 | 3.2741077416444138 | 3.3776864354461122 |
| 2026-06 | departures | 1640220 / 1925280 | 692418 | 0 | 285060 | 3.2809909646266964 | 3.442292918220643 |
| 2026-07 | arrivals | 1719013 / 1989456 | 748761 | 0 | 270443 | 2.895822195643663 | 3.0443121061005574 |
| 2026-07 | departures | 1719013 / 1989456 | 755632 | 0 | 270443 | 2.9032305165813175 | 3.0883866778359352 |
| 2026-08 | arrivals | 1742524 / 1989456 | 755307 | 0 | 246932 | 3.002236411091038 | 3.1657953323336994 |
| 2026-08 | departures | 1742524 / 1989456 | 761750 | 0 | 246932 | 3.008546797633777 | 3.2183676760904123 |
| 2026-09 | arrivals | 1708737 / 1925280 | 788170 | 0 | 216543 | 2.9075738396254076 | 3.1146686675629627 |
| 2026-09 | departures | 1708737 / 1925280 | 792997 | 0 | 216543 | 2.9140757179132892 | 3.162499477405201 |

### distribution: Are eligible station-hours sparse and long-tailed?

Population/grain: All observed IDs x UTC hour x target within 24 NY months.
Расчёт: monthly/histograms: count zero, 1, 2-4, >=20; quantile_cont on eligible labels.
Результат: `[{"prediction_type": "departures", "eligible": 38602207, "zero": 19189766, "single": 6557047, "missing": 8246273}, {"prediction_type": "arrivals", "eligible": 38647763, "zero": 19240632, "single": 6647302, "missing": 8200717}]`.
Ограничения: Zeros are published-event labels, not station operating status or unmet demand; missing excluded from quantiles.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### churn: Does observed ID composition change?

Population/grain: ID x event month, either target.
Расчёт: station_month sets, first/last, adjacent set difference/Jaccard; activity quantiles.
Результат: `{"union_ids": 2674, "stable_ids": 1928, "activity": [{"prediction_type": "departures", "ids": 2573, "event_quantiles": [1.0, 4763.0, 14909.0, 49981.0, 134610.2, 208605.64000000048, 333392.0], "events": 92027342}, {"prediction_type": "arrivals", "ids": 2674, "event_quantiles": [1.0, 4513.75, 14035.5, 46906.0, 133306.55, 203537.50999999995, 334117.0], "events": 91804709}]}`.
Ограничения: First/last seen are window-censored; identity reuse/moves cannot be resolved as installations.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### weekly: Do observed rates differ across local weekday/hour?

Population/grain: Eligible target station-hours, NY ISO weekday 1..7; repeated DST hours both included.
Расчёт: sum(actual)/count(eligible hours), not unnormalised weekday totals.
Результат: `[{"prediction_type": "departures", "weekday": 2, "local_hour": 17, "eligible_hours": 233266, "events": 1450701, "mean": 6.219084650141898}, {"prediction_type": "arrivals", "weekday": 2, "local_hour": 18, "eligible_hours": 233187, "events": 1398491, "mean": 5.99729401724796}]`.
Ограничения: Eligibility mix changes; descriptive associations only, no commuter or weather causal claim.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### annual: Does seasonal rate variation persist in stable IDs?

Population/grain: 24 event months; full universe and IDs observed in all months.
Расчёт: monthly eligible mean + stable_mean; 12 year-on-year pairs, no partial months.
Результат: `[{"prediction_type": "departures", "lowest": {"period": "2026-02", "prediction_type": "departures", "candidate_hours": 1796928, "eligible_hours": 1358823, "source_gap_hours": 85568, "station_gap_hours": 352537, "zero_hours": 906181, "single_event_hours": 210843, "rare_2_4_hours": 171576, "peak_ge20_hours": 1347, "events": 1218589, "mean": 0.8967974489687031, "quantiles": [0.0, 0.0, 1.0, 5.0, 10.0, 66.0], "stable_eligible_hours": 1179308, "stable_mean": 0.9085183853581931}, "highest": {"period": "2025-09", "prediction_type": "departures", "candidate_hours": 1925280, "eligible_hours": 1559121, "source_gap_hours": 0, "station_gap_hours": 366159, "zero_hours": 659426, "single_event_hours": 262344, "rare_2_4_hours": 299440, "peak_ge20_hours": 50881, "events": 5285037, "mean": 3.389754226900927, "quantiles": [0.0, 1.0, 4.0, 16.0, 31.0, 201.0], "stable_eligible_hours": 1374188, "stable_mean": 3.39478732167651}}, {"prediction_type": "arrivals", "lowest": {"period": "2026-02", "prediction_type": "arrivals", "candidate_hours": 1796928, "eligible_hours": 1389581, "source_gap_hours": 48132, "station_gap_hours": 359215, "zero_hours": 938748, "single_event_hours": 211779, "rare_2_4_hours": 169438, "peak_ge20_hours": 1436, "events": 1211386, "mean": 0.8717635028112791, "quantiles": [0.0, 0.0, 1.0, 5.0, 10.0, 72.0], "stable_eligible_hours": 1205908, "stable_mean": 0.8790479870769577}, "highest": {"period": "2025-09", "prediction_type": "arrivals", "candidate_hours": 1925280, "eligible_hours": 1559121, "source_gap_hours": 0, "station_gap_hours": 366159, "zero_hours": 652696, "single_event_hours": 270954, "rare_2_4_hours": 302101, "peak_ge20_hours": 51843, "events": 5274498, "mean": 3.3829946489079425, "quantiles": [0.0, 1.0, 4.0, 16.0, 31.0, 155.0], "stable_eligible_hours": 1374188, "stable_mean": 3.3309248807295653}}]`.
Ограничения: Only two annual cycles, no trend/season separation; stable cohort has hindsight survivorship and is descriptive only.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### geography: Are endpoint event volumes spatially heterogeneous?

Population/grain: Canonical endpoint x 0.1 degree cell, both targets, event window.
Расчёт: floor(endpoint latitude*10), floor(longitude*10); broad box; unknown/outside explicit.
Результат: `{"largest_cells": [{"prediction_type": "departures", "cell": "407.0,-740.0", "n": 56428860, "ids": 891}, {"prediction_type": "arrivals", "cell": "407.0,-740.0", "n": 56258458, "ids": 890}], "coordinate_churn": {"ids": 2570, "multi_cell_ids": 2}}`.
Ограничения: Unequal cell area/station exposure, not borough rates; historical trip coordinates only, never current GBFS/capacity; no causal spatial effect.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### outliers: Are extreme station-hours retained?

Population/grain: Positive observed ID x UTC hour x target.
Расчёт: Tukey q3+1.5*IQR on positive hours; deterministic top20; daily extremes.
Результат: `[{"prediction_type": "arrivals", "q1_positive": 1.0, "q3_positive": 6.0, "positive_hours_above_tukey_fence": 1447550, "max_positive": 221}, {"prediction_type": "departures", "q1_positive": 1.0, "q3_positive": 6.0, "positive_hours_above_tukey_fence": 1441107, "max_positive": 216}]`.
Ограничения: Heavy-tailed counts are not automatically erroneous; no clipping/exclusion; anomaly causes unverified.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### shift: Do count distributions shift across months and matched seasons?

Population/grain: Eligible station-hour histograms, full observed universe; adjacent and year-on-year.
Расчёт: TV=0.5*sum(abs(bin_share_current-bin_share_previous)), fixed 0/1/2-4/5-9/10-19/20+ bins.
Результат: `[{"from": "2026-02", "to": "2026-03", "prediction_type": "arrivals", "comparison": "adjacent", "total_variation": 0.12712292071568748}, {"from": "2026-02", "to": "2026-03", "prediction_type": "departures", "comparison": "adjacent", "total_variation": 0.11833310630116733}, {"from": "2025-11", "to": "2025-12", "prediction_type": "arrivals", "comparison": "adjacent", "total_variation": 0.10096532197102215}, {"from": "2025-11", "to": "2025-12", "prediction_type": "departures", "comparison": "adjacent", "total_variation": 0.10018294311104473}, {"from": "2024-11", "to": "2024-12", "prediction_type": "departures", "comparison": "adjacent", "total_variation": 0.08182531505314022}, {"from": "2024-11", "to": "2024-12", "prediction_type": "arrivals", "comparison": "adjacent", "total_variation": 0.08009815955253952}, {"from": "2025-02", "to": "2026-02", "prediction_type": "arrivals", "comparison": "year_on_year", "total_variation": 0.07937225390547208}, {"from": "2025-02", "to": "2026-02", "prediction_type": "departures", "comparison": "year_on_year", "total_variation": 0.07473231287284701}]`.
Ограничения: Mix, season and source/eligibility gaps confounded; descriptive effect size, no significance/causality or model selection.
Проверка: Independent raw-Silver event-month totals, Python label oracle; frozen fixture expected values and grain/coverage tests. Geography/churn/TV additionally checked by independent calculation in corrective run..

### Источники labels и ограничения ML

Прямой подсчёт targets из Silver: `[{'prediction_type': 'arrivals', 'all_events': 92076567, 'unknown_id_events': 271858, 'known_id_events': 91804709}, {'prediction_type': 'departures', 'all_events': 92076054, 'unknown_id_events': 48712, 'known_id_events': 92027342}]`; события вне окна: `[{'prediction_type': 'departures', 'events': 513}]`.
Независимых расхождений по event-month: 0; Python label oracle: `{'method': 'Direct Silver counts + stdlib bisect; no label_grid/F4 helpers', 'sample_ids': ['6140.05', '6401.03', 'JC022'], 'tested_station_hours': 105120, 'mismatches': 0, 'scope': 'low/median/high total activity IDs, every hour/type/month'}`; дубликаты label grain: 0.
Исключения по event month / station распределениям — `research.station_exclusions`; по source month — `monthly.dispositions`. Reason flags пересекаются и не складываются. Детальные churn/weekly/daily/geo/histograms/shifts и исполнявшийся SQL — в существующем JSON.
F4 cohort read-only: `{'status': 'verified', 'dataset_id': 'd9b87571006b8367d0d286fd77ec9b2e7cac0900a773eae599fc7c3813bc12e5', 'coverage': {'stations': 64, 'city_starts': 92076054, 'city_ends': 92076567, 'unknown_start_ids': 48712, 'unknown_end_ids': 271858, 'cohort_departures': 2356614, 'cohort_arrivals': 2352323, 'departure_city_coverage': 0.025607759050565646, 'arrival_city_coverage': 0.02562311918008476}, 'join_duplicate_keys': 0, 'dense_label_zero_hours': 997718, 'dense_label_missing_hours': 198731, 'limitation': 'Cohort64 is bounded, not city representative; historical publication times unknown; +45d is an assumption, not verified production readiness. No capacity/current coordinates/historical GBFS/actual future weather features.'}`.
Историческая первая публикация архивов UNKNOWN. retrieved_at/Last-Modified не доказывают доступность на прошлом origin; +45d — гипотеза F4. Calendar известен заранее; actual weather — только outcome diagnostics; historical GBFS/capacity/availability не восстановлены из будущих снимков. Strict operational ML-readiness и причинные эффекты этим EDA не доказаны.
Новый EDA run: `{'elapsed_seconds': 711.485, 'peak_rss_bytes': 3346685952, 'threads': 2, 'duckdb_memory_limit': '2GB', 'cold_cache': False}`; полный report: `{'inventory_path': 'data\\inventory\\807194651725833df9977f9cee4a0635a0f9c37fdad4201d9e61e7143227be1f\\inventory.json', 'inventory_sha256': '362f5c07c17ecd3297260b569e033b5c648720d90dc82497827e56fbb57821e5', 'snapshot_path': 'data\\f2\\snapshots\\5c4f166d01265d219f7026db9195f3d4cdb362e1f65c1625713628aa55a26cea', 'report_code_sha256': 'e3f6874ea75d9e845b9e23478e7169a0e6364328a1b09b4ca77f4cf268aa68ab', 'verified_at': '2026-10-29T22:18:11.706652+05:00', 'elapsed_seconds': 726.234, 'peak_rss_bytes': 3346685952, 'scope': 'Fresh report/SQL checks; original ingest/audit measurements retain their original dates'}`.
Измерения wall/RSS — shared host, warm caches, OS process high-water, не citywide SLA. Исходные ingest/SQLite row audits выше сохраняют прежние даты; в этой задаче они checksum-bound, а не повторно исполненный CSV/SQLite проход.

Воспроизведение (без download/build/train):

```sh
uv run --locked --group ml python -m citibike.history verify --inventory data/inventory/807194651725833df9977f9cee4a0635a0f9c37fdad4201d9e61e7143227be1f/inventory.json
```

## Выводы корректирующего запуска

- departures: zero 19189766/38602207 (49.71% eligible); missing 8,246,273. Это разные populations, missing в квантили и среднее не входит.
- arrivals: zero 19240632/38647763 (49.78% eligible); missing 8,200,717. Это разные populations, missing в квантили и среднее не входит.
- departures: среднее 0.897 в 2026-02 и 3.390 в 2025-09; на постоянных IDs 0.909 и 3.395. Сезонный контраст сохраняется в стабильном составе; два цикла не разделяют сезонность, тренд и источник пропусков.
- arrivals: среднее 0.872 в 2026-02 и 3.383 в 2025-09; на постоянных IDs 0.879 и 3.331. Сезонный контраст сохраняется в стабильном составе; два цикла не разделяют сезонность, тренд и источник пропусков.
- Состав наблюдаемых IDs: 2281 → 2501; union 2674, во всех месяцах 1928. Это churn наблюдений/идентификаторов, не подтверждение открытия или закрытия станций.
- Недельный максимум нормализованного объёма: вторник 17:00 NY для departures (6.219/eligible station-hour), 18:00 для arrivals (5.997). Причина такого профиля не установлена.
- Географический decile-cell 407,-740 ([40.7,40.8) × [-74.0,-73.9)) содержит 56 428 860 departures и 56 258 458 arrivals. Это объём endpoint events, не район и не rate на площадь/станцию. Два start IDs встречаются в нескольких таких ячейках; ID не считается постоянной установкой.
- Самый большой adjacent TV — arrivals Feb→Mar 2026: 0.1271; максимальный year-on-year TV — Feb 2025→2026: 0.07937 arrivals / 0.07473 departures. Mix/сезонность/пропуски не отделены; это диагностика сдвига, не новый критерий выбора модели.
- Наблюдаемые station-hour maxima: 216 departures / 221 arrivals. Пики сохранены, Tukey fence описывает хвост и не служит автоматическим фильтром.
- ML-ограничение: cohort64 покрывает около 2.56% known-ID событий. Missing и неизвестная первая публикация архивов исключают заявление о доказанной operational readiness; F4 остаётся approximate retrospective.
- Самостоятельная сверка: 22 geo cells, 24 churn months, 336 weekday/hour groups, 70 TV comparisons; 0 расхождений. SQL и полный независимый Python script сохранены в `research.independent_review` существующего JSON.

Повтор самостоятельной сверки: до запуска verify извлеките `research.independent_review.reproduction_script` из JSON в `.cache/f2_independent.py`; затем `uv run --locked --group ml python .cache/f2_independent.py`. Основной verify воспроизводит core EDA и встроенный label oracle; независимый script добавляет эту проверку/выводы.
