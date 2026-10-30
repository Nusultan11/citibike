# F5 — Risk & Optimization: PARTIAL до corrective CI

DoD10 **VERIFIED для recorded-as-of real_official replay**,3/64 eligible. Это не live-риск на время исполнения. Актуальная корректирующая запись и ограничения — в последнем разделе; нижеследующее исходное evidence checkpoint4e6af30 сохранено как история.

## Исходный checkpoint4e6af30 — PARTIAL

CLI/симуляция/оптимизация проверены на **controlled_fixture**. **Real E2E NOT VERIFIED**: оба официальных GBFS status feeds stale по собственному TTL. Реальный риск и эффект политик не рассчитывались; unavailable не равен нулевому риску.

Источник требований: ROADMAP §2/§§3–4, ML_SPEC §§2,5–7, METRICS §§3,5, DATA_CONTRACTS §§2–4, OPERATIONS §§2–4,7. Пользователь принял F2/F3/F4; operational promotion F4 остаётся NOT VERIFIED. F6–F8 не начинались.

## Реальные inputs / as-of gate

Forecast `1e204ac4a944381529e00b500586a00a82c7b8eca31cd8f4c978b1c282d81810`: действительная PostgreSQL публикация и точное совпадение rows/manifest. Accepted F2/F3/F4 inputs сохранены. Выполнены только два bounded официальных collections в отдельном ignored `data/f5/live-inputs`; F2 GBFS pointers не менялись.

| Collection / as-of | Status feed age at as-of / TTL | Coverage | Outcome |
|---|---|---|---|
| `20261030T053639553419-cc295a0c` / `2026-10-30T05:36:43.811279+00:00` | 86.811279s /60s | 0/64 | unavailable, exit2, risk=null |
| `20261030T053836644030-f8d09555` / `2026-10-30T05:38:38.386706+00:00` | 81.386706s /60s | 0/64 | unavailable, exit2, risk=null |

Проверка независима от adapter: original JSON checksums/last_updated/ttl + received_at; expiry=min(updated+TTL,received+60s), без расширения TTL и future backfill. Station last_reported/capacity/flags/mapping проверяются отдельно. JSON evidence содержит все hashes, причины исключений, commands и gate oracle. Первый snapshot — stale status, второй — stale information и status. Будущие данные/исторический GBFS не подставлялись.

## Модель и ограничения

- SCENARIO ONLY: Poisson attempts use published observed trip forecasts as means, not latent-demand estimates.
- Independent station/hour/type counts; returns are exogenous, not linked to simulated rentals. Fleet is an open subsystem.
- Uniform within-hour events or all departures/arrivals in the first half-hour; demand scales are assumptions, not fitted intervals.
- No demand is modeled between as-of and the next complete forecast hour; lead-in hold may be optimistic.
- GBFS operating flags and capacity remain fixed after as-of; disabled/unknown slots capacity-bikes-docks stay blocked.
- GBFS TTL validates the INITIAL snapshot only, not future observations over the simulation horizon.
- All transport departs at as-of; one direct loaded trip per vehicle, no fleet reuse, depot routing or operator cost claims.
- Haversine/specified speed plus handling gives scenario travel times, not measured street routes.
- Delivery wins exact-time ties, then station/type/sequence; undeliverable cargo remains on its vehicle to horizon end.
- Optimizer is optimal only for L1 inventory-target + weighted movement-cost surrogate; simulated losses can worsen.
- Risk is ever-empty/full fraction including initial state among assumed draws; Monte Carlo Wilson intervals are not calibration.
- No field validation/operational uplift/economic saving claim; Seasonal Naive/unavailable F4 fallback retained.

Целевая функция MILP: `Σ|bikes−out+in−target| + λ×relative_movement_cost`; target=`clip(usable_capacity/2+ΣD−ΣA,0,usable_capacity)`. При нулевом спросе target=initial stock. Это surrogate, не доказанная оптимизация реального hidden demand. Целочисленные x/y ограничены запасом, исходными доками, fleet×capacity и временем; замкнутых маршрутов/повторного использования транспорта нет. Независимая эвристика использует только thresholds0.25/0.75 и ближайшие пары. Каждая политика получает один и тот же сохранённый draw; планы не используют его будущие попытки.

[SciPy MILP / HiGHS](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html) уже доступен в locked ML stack; новые зависимости не добавлены. Неоптимальный solver status/лимит даёт unavailable plan и PARTIAL, не скрытый success.

## Controlled comparison — не официальные показатели

Result `3403a0eb33544546f5892476900bfb62e4bf06cf074c79cd07c554c0b9ce90b0`. As-of `2026-10-30T05:52:24.069798+00:00`, start `2026-10-30 06:00:00+00:00`; 4 станции, 4 полных forecast hours, 32 repeats/cell, seed42, scales0.5/1/1.5 и три orders. Это synthetic GBFS + настоящий pipeline controlled F4 fixture, не citywide точность. Initial blocked slots и operating flags сохранены. Полный provenance и параметры — в JSON.

| Scale | Event order | No-action unserved attempts | Heuristic | Optimizer | Optimizer gain vs no-action |
|---|---|---:|---:|---:|---:|
| 0.5 | uniform | 3.34375 | 0.62500 | 0.62500 | 2.71875 |
| 0.5 | departures_first | 4.18750 | 0.93750 | 0.93750 | 3.25000 |
| 0.5 | arrivals_first | 3.28125 | 0.53125 | 0.53125 | 2.75000 |
| 1 | uniform | 7.37500 | 2.75000 | 2.90625 | 4.46875 |
| 1 | departures_first | 9.84375 | 4.37500 | 4.68750 | 5.15625 |
| 1 | arrivals_first | 7.12500 | 2.78125 | 2.81250 | 4.31250 |
| 1.5 | uniform | 11.65625 | 7.21875 | 7.56250 | 4.09375 |
| 1.5 | departures_first | 16.84375 | 11.78125 | 12.62500 | 4.21875 |
| 1.5 | arrivals_first | 11.78125 | 7.50000 | 7.62500 | 4.15625 |

Знаменатель — 32 исхода одинаковых draws в каждой cell, метрика = модельные rejected rentals + returns; это не число потерянных реальных поездок. Optimizer лучше no-action в9/9, совпадает с эвристикой в3/9 и хуже неё в6/9. Не подбирались параметры, чтобы скрыть этот результат. Scale1/uniform: no-action7.375, heuristic2.75, optimizer2.90625; baseline-relative gain4.46875 не является фактической экономией.

| Policy (scale1/uniform) | Rejected rentals | Rejected returns | Moved bikes | Relative cost | Loss+λcost |
|---|---:|---:|---:|---:|---:|
| no_action | 4.53125 | 2.84375 | 0 | 0.000000 | 7.375000 |
| heuristic | 1.40625 | 1.34375 | 6 | 2.667170 | 3.016717 |
| optimizer | 1.71875 | 1.18750 | 5 | 2.555975 | 3.161848 |

Все controlled outcomes: constraints0, undelivered0. Отдельный negative case воспроизводит заполнение доков до доставки: груз остаётся в машине, conservation сохраняется, не исчезает при clip. Closed attempts считаются отдельно; risk недействующей функции null. JSON содержит per-station risk counts/fractions, Monte Carlo SE/Wilson95 и empirical p05/p95 losses. Wilson precision не учитывает неправильность Poisson/forecast/operating assumptions; calibration и реальные отказанные попытки неизвестны.

## Независимая сверка / ресурсы

Oracle: **288 paired draws /864 outcomes /50958 ledger checks**, 0 mismatches. Отдельный алгоритм использует bikes/free docks/blocked/cargo, сверяет mass balance после каждого события, независимо считает Haversine/стоимость/отказы/summary/risk intervals. Production simulator/optimizer helpers не вызываются. Source audit читает original GBFS JSON; целочисленный optimum дополнительно сравнен с exhaustive маленькой задачей. Tampered outcome с пересчитанным checksum обнаружен.

Новый F5 CLI **3.1698552s**; internal simulation/plan loop **1.5274921s**, OS peak RSS **146337792B**. Repeat того же CLI **1.0900016s**, тот же ID, без пересимуляции. Oracle **0.2993761s**. Политики имеют отдельные planning/simulation wall times; memory — общий process high-water, не marginal RAM. Измерения shared-host/warm filesystem; не cold benchmark, citywide или concurrent SLA.

Immutable inputs/plans/events/outcomes/manifests — ignored data/f5. Git содержит только код и агрегированные evidence. Real active pointer отсутствует; controlled pointer отдельный. Сбой/partial/unavailable не активирует недостоверный результат.

## Проверки / DoD

Locked sync/Ruff/format/mypy — PASS. Полный suite144 passed,170.05s, один existing Starlette warning. Controlled module, real CLI gates, independent oracle, fresh F1/F3/F4 и isolated forecast smoke — PASS. Build/package/diff/secrets/source-recipe audit — PASS:16 целевых файлов,55 local links; wheel202787B/sdist172779B без данных/кэша/env/models, migrations001–005 сохранены. Code/evidence `44fddad2b365c32116269d2763d82c8a8e37130f` pushed, [actual CI41011696935](https://github.com/Nusultan11/citibike/actions/runs/41011696935) completed/success: python/frontend/docker-smoke, включая новые F5 tests и прежние E2E. Итоговые docs SHA/CI/checkpoint/bundle проверяются после push и сообщаются в чате.

Fresh regressions: F1 все8 raw→UI значений3860371; F3 Silver/PG/HTTP/HTML92076054,0 mismatches; F4 labels1046111 groups/4708937 events, PIT/frozen validation/final и corrective2663114 rows PASS, serving3072/3072,0 mismatches. Real selection/model/final lock/forecasts, applied migrations и1361 protected hashes сохранены.

| Criterion | Status | Evidence |
|---|---|---|
| 1. Read-only F4 publication/lineage + GBFS raw/Silver adapter | PASS | Published manifest/rows exact; hashes, raw source oracle, schema/model checks; controlled PG integration |
| 2. One as-of / TTL / mapping / missing gates | PASS | Two real rejected collections + raw freshness oracle; stale/future/missing/duplicate/capacity tests |
| 3. Discrete inventory/capacity/operating/transport simulation | PASS | Per-event inventory+free docks+blocked+cargo ledger, partial delivery and closed tests |
| 4. Seed-based events / no false paired gain | PASS | 288 same materialized draws, 864 outcomes, shared event hashes; no policy sees evaluation events when planning |
| 5. Scenario empty/full fractions / uncertainty / coverage | PASS | Ever-boundary outcomes and Monte Carlo Wilson/SE independently checked; unavailable/non-operating risk null |
| 6. No-action / independent threshold heuristic / MILP | PASS | Three policies, integer stock/docks/fleet/direct-trip/time/cost constraints; no routing claim |
| 7. Independent constraints and cost oracle | PASS | 50958 ledger reconciliations, no mismatches; independent small exhaustive optimum and tampering negative |
| 8. Demand scale/order sensitivity and resources | PASS | 3 scales x3 orders x32 repeats; policy and total wall/process high-water, honest optimizer-vs-heuristic outcome |
| 9. Immutable CLI / repeat / partial failure | PASS | Exact repeat ID; isolated fixture pointer; failed/partial/unavailable keep last valid version; persisted failure request |
| 10. Compatible real-input F5 simulation E2E | NOT VERIFIED | Official status feeds stale at actual receipt/as-of; 0/64 eligible in both collections, risk=null; no real policy metrics |
| 11. Frozen F0-F4 behavior / contracts / E2E | PASS | 1361 protected fingerprints; fresh F1/F3/F4 and controlled PG/HTTP smoke |
| 12. Locked checks / build / evidence / CI delivery | PASS | 144 tests, locked checks/build/audit, code44fddad pushed, actual CI41011696935 all3 jobs success; финальные docs SHA/CI/checkpoint/bundle проверяются и сообщаются в отчёте |
| 13. Scientific limits / no operational or economy claims / no F6 | PASS | Explicit assumptions, uncalibrated fractions, published-trip means, F4 fallback and F6-F8 NOT RUN |

## Воспроизведение

```sh
uv run --locked --group ml python -m tests.integration.test_risk
uv run --locked --group ml python -m citibike.risk verify --result data/f5/results/3403a0eb33544546f5892476900bfb62e4bf06cf074c79cd07c554c0b9ce90b0
```

Controlled harness создаёт только собственную UUID database, публикует fixture туда, выполняет F5 CLI/repeat/oracle и удаляет DB в finally. Новый запуск имеет фактические новые origin/receipt/ID; exact repeat использует pinned as-of и immutable inputs. `verify` по сохранённому результату пересчитывает ledger без PostgreSQL. Реальные отрицательные команды с фактическими pinned GBFS paths/as-of находятся в JSON; exit2 для unavailable ожидаем.

Обязательный suite:

```sh
uv sync --locked --group ml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked --group ml pytest -q tests/unit tests/data_contracts tests/ml tests/integration --basetemp .cache/dt
uv build
uv run --locked --group ml python -m citibike.forecasting verify --serving --output .cache/f5-f4-regression.json
uv run --locked --group ml python -m citibike.verify --result data/processed/69d257248df121ef5273482991f61c5379ea8eebba7273dd92c6502943640284 --output .cache/f5-f1-regression.json --recompute-baseline
uv run --locked --group ml python -m citibike.analytics verify --output .cache/f5-f3-regression.json
```

Остаток: совместимый действительно свежий официальный GBFS + ещё действительная опубликованная F4 версия → real F5 run/independent verification. Если окно прогноза истечёт, потребуется отдельное разрешённое обновление F4 batch; принятая frozen selection не меняется. F4 operational promotion, calibration, hidden-demand inference, field/economic effect, real fleet/routing, production SLA — NOT VERIFIED/NOT RUN. F6 не начинать.

Следующий шаг: закрыть real E2E F5 при валидных inputs, затем независимая приёмка F5.

## Корректирующий DoD10 — официальный as-of replay

База: чистый main/remote `4e6af30a78400cf88234568b5a04a4e73fa0fbf1`, baseline actual CI41012236787 success, open PR0. Код/тесты/API/migrations/TTL/mapping/models/frozen selection не менялись. Прежние controlled metrics и оба rejected real records сохранены без пересчёта/переписывания.

### Диагноз и bounded collection

Две прежние status source ages при receipt86.156059s/81.234725s >TTL60; минимальные station ages при final receipt103.811279s/99.386706s. Независимый raw-epoch→UTC расчёт согласуется с сохранёнными metadata. Старые HTTP/cache headers отсутствуют, поэтому точная декомпозиция origin/cache/delivery и исторический NTP offset NOT VERIFIED.

Новые официальные responses показывают CloudFront Hit/Age при исходном no-cache request; network/header time<1.2s. Object source age может уже превышать60s до клиентской доставки; дополнительные station-report lags объясняют stale даже при fresh envelope. Current Date+Age согласуется с локальным UTC примерно до1s; это не измерение исторической clock calibration. Advertised gbfs_versions содержит v1.1/v2.3; использована подтверждённая2.3, альтернативные station URLs не подставлялись.

| New collection | Info/status ages at receipt,s | Quality valid/warning/stale | Current-asof run |
|---|---|---|---|
| `20261030T063504316999-557099fb` | 51.432184/49.239942 | 0/0/2520 | exit2,eligible0,`da9d0c8ac4e3` |
| `20261030T063651137970-27bd3fb5` | 38.148508/36.019378 | 179/0/2341 | exit2,eligible0,`c788904efd0d` |
| `20261030T063819980040-797db57a` | 66.675951/65.464916 | 0/0/2520 | exit2,eligible0,`8618653275e6` |
| `20261030T064047358350-50df3e4f` | 93.728608/32.570164 | 0/342/2178 | exit2,eligible0,`566c77f77edb` |
| `20261030T064408457670-fea5bb7a` | 55.570482/52.693812 | 0/0/2520 | exit2,eligible0,`d950e347b6ee` |

Всего5 новых collections (четыре planned +один дополнительный по observed cache overlap), затем сбор остановлен. Первый collection выполнен existing CLI --count1, остальные тем же collector с HTTP observation hooks. URLs/cache keys/TTL/timestamps/quality flags/mapping не изменялись. Каждый исход, exact args, source hashes и HTTP metadata — в JSON. Все current-asof попытки unavailable сохранены; поздний rejected run свежей при receipt коллекции не удалён.

### Согласованный реальный snapshot

Result **`99f703bf3372edad673fc40e43bc6ea6746ff2a44a263aea5e4983234983c83a`**, real_official, created `2026-10-30T06:46:16.251484+00:00`. As-of **`2026-10-30T06:36:54.163891+00:00`** взят из фактически записанного collection ended_at ПОСЛЕ получения raw и materialization Silver. Расчёт исполнен позже как **recorded-as-of replay**, не объявлен contemporaneous/live decision. Received_at≤asof, source/report TTL и PG publication≤asof проверены независимо; никаких future snapshots/backdating source timestamps нет.

F4 publication `1e204ac4a944381529e00b500586a00a82c7b8eca31cd8f4c978b1c282d81810`, published `2026-10-29T11:46:51.393101+00:00`, valid_until2026-10-30T12:00:00Z. Исходные3072 rows (3024ok) и24×2 coverage совпали с PostgreSQL; оба Seasonal Naive model IDs прежние. Никаких real fit/select/final/publish/activate.

Scenario target hours `2026-10-30T07:00:00+00:00`…`2026-10-30T11:00:00+00:00`,4 complete UTC hours, плюс23.097268min lead-in без модельного спроса. Coverage **3/64=4.6875%**, исключены61 ID; это доля F4 cohort, не сети NYC. Все excluded IDs/reasons и source versions/checksums в JSON и immutable inputs.

| Trip ID | GBFS ID | Capacity/bikes/docks/blocked | Last reported UTC | Age/slack,s |
|---|---|---|---|
| 3138.03 | `2022386456542683974` | 35/21/7/7 | 2026-10-30T06:35:55+00:00 | 59.163891/0.836109 |
| 6611.07 | `88c1303b-3349-4591-bf08-c7e40cf3537a` | 47/6/33/8 | 2026-10-30T06:35:57+00:00 | 57.163891/2.836109 |
| 7504.18 | `66de677e-0aca-11e7-82f6-3863bb44ef7c` | 27/25/0/2 | 2026-10-30T06:35:56+00:00 | 58.163891/1.836109 |

Все три installed/renting/returning=true, unique alias mapping. Station TTL slack очень мал:0.836109/1.836109/2.836109s по отсортированному clock-age. Гипотетический сдвиг asof+1/+2/+3s оставляет2/1/0 из принятых IDs; это sensitivity, не подмена фактического asof. Initial snapshot TTL не подтверждает будущую наблюдаемость. Current live status остаётся unavailable.

### Три политики, риск и oracle

| Scale | Order | No-action lost | Heuristic lost | Optimizer lost | Heuristic/optimizer loss+λcost |
|---|---|---:|---:|---:|---:|
| 0.5 | uniform | 0.78125 | 0.00000 | 0.00000 | 7.379325/7.379325 |
| 0.5 | departures_first | 0.78125 | 0.00000 | 0.00000 | 7.379325/7.379325 |
| 0.5 | arrivals_first | 0.78125 | 0.00000 | 0.00000 | 7.379325/7.379325 |
| 1 | uniform | 1.53125 | 0.00000 | 0.00000 | 7.379325/6.781048 |
| 1 | departures_first | 1.53125 | 0.00000 | 0.00000 | 7.379325/6.781048 |
| 1 | arrivals_first | 1.53125 | 0.00000 | 0.00000 | 7.379325/6.781048 |
| 1.5 | uniform | 2.25000 | 0.15625 | 0.15625 | 7.535575/5.640744 |
| 1.5 | departures_first | 2.25000 | 0.15625 | 0.15625 | 7.535575/5.640744 |
| 1.5 | arrivals_first | 2.25000 | 0.15625 | 0.15625 | 7.535575/5.640744 |

32 repeats/cell, seed42, identical materialized events для всех3 policies; планы не видят evaluation draws. В scale1/uniform no-action1.53125 модельных rejected returns, heuristic0/optimizer0. Heuristic12 bikes/2 vehicles, optimizer11/2:7504.18→6611.07,5.982771km proxy/33.931085min. Relative costs73.7932548/67.8104836 при λ0.1. **No-action имеет меньший evaluated loss+λcost во всех9 cells**: перевозки не рекомендуются по этой функции, несмотря на меньшее число модельных отказов. Это не operational saving и не доказанная citywide оптимизация.

Ever-full для7504.18=32/32=1 у всех policies, так как она уже full в initial snapshot; уменьшение rejected returns не равно уменьшению ever-full risk. Empty fractions=0/32, но Wilson95 upper≈0.10718, не нулевая реальная вероятность. Full interval≈[0.89282,1]. Остальные принятые station full fractions0; исключённые61 имеют null risks. Все per-station uncertainty и source states — JSON. Потоки exogenous, blocked slots/future flags постоянны по сценарию, не наблюдены в будущем.

Independent oracle **288 paired draws /864 outcomes /14526 ledger checks /0 mismatches**: source raw JSON freshness/IDs/state, независимый bikes/free/blocked/cargo balance, capacity/fleet/cost, losses/fractions/MC intervals. Constraints0, undelivered0. Full new CLI **2.0075832s**, loop **0.8068754s**, OS peak RSS **149565440B**; exact repeat **2.5889189s**, same ID. Shared-host/warm filesystem, не SLA или isolated policy RAM.

Поздняя current-asof попытка exit2 сохранила valid F5 active bytes; F4 publication не менялась. Replay result не обозначается свежим сейчас.

### Проверки и актуальный DoD

Locked sync/Ruff/format/mypy/build PASS; полный suite **144 passed,159.68s**, один existing Starlette warning. Fresh F1 independent raw→UI3860371, F3 Silver/PG/HTTP/HTML92076054/0 mismatch, F4 label/PIT/frozen validation/final/research и serving3072/3072/0 mismatch — PASS.2251 protected fingerprints PASS. Новых modules/tests/dependencies/API/migrations/инфраструктуры нет. Diff/secrets/package/source-recipe audit PASS:4 целевых файла,55 local links; wheel203103B/sdist173401B без datasets/cache/env/models. Git/actual CI/checkpoint/bundle ещё ожидают завершения delivery.

| DoD | Status | Current evidence |
|---|---|---|
| 1. Read-only F4 publication/lineage + GBFS raw/Silver adapter | PASS | Published manifest/rows exact; hashes, raw source oracle, schema/model checks; controlled PG integration |
| 2. One as-of / TTL / mapping / missing gates | PASS | Two real rejected collections + raw freshness oracle; stale/future/missing/duplicate/capacity tests |
| 3. Discrete inventory/capacity/operating/transport simulation | PASS | Per-event inventory+free docks+blocked+cargo ledger, partial delivery and closed tests |
| 4. Seed-based events / no false paired gain | PASS | 288 same materialized draws, 864 outcomes, shared event hashes; no policy sees evaluation events when planning |
| 5. Scenario empty/full fractions / uncertainty / coverage | PASS | Ever-boundary outcomes and Monte Carlo Wilson/SE independently checked; unavailable/non-operating risk null |
| 6. No-action / independent threshold heuristic / MILP | PASS | Three policies, integer stock/docks/fleet/direct-trip/time/cost constraints; no routing claim |
| 7. Independent constraints and cost oracle | PASS | 50958 ledger reconciliations, no mismatches; independent small exhaustive optimum and tampering negative |
| 8. Demand scale/order sensitivity and resources | PASS | 3 scales x3 orders x32 repeats; policy and total wall/process high-water, honest optimizer-vs-heuristic outcome |
| 9. Immutable CLI / repeat / partial failure | PASS | Exact repeat ID; isolated fixture pointer; failed/partial/unavailable keep last valid version; persisted failure request |
| 10. Compatible real-input F5 simulation E2E | PASS | Official GBFS at recorded actual asof06:36:54.163891 +published F4;3/64 eligible, numerical3-policy replay99f703b; independent288 draws/864 outcomes/14526 ledger checks,0 mismatch. NOT live-at-execution risk. |
| 11. Frozen F0-F4 behavior / contracts / E2E | PASS | 1361 protected fingerprints; fresh F1/F3/F4 and controlled PG/HTTP smoke |
| 12. Locked checks / build / evidence / CI delivery | PARTIAL | Current144 tests159.68s, locked checks/build, fresh F1/F3/F4 PASS; corrective audit/commit/push/actual CI/checkpoint/bundle pending. |
| 13. Scientific limits / no operational or economy claims / no F6 | PASS | Explicit assumptions, uncalibrated fractions, published-trip means, F4 fallback and F6-F8 NOT RUN |

### Exact reproduction

Повтор требует сохранённых ignored inputs и той же неизменённой F4 PG publication. Pinned asof replay не является запросом текущего live risk.

```powershell
uv run --locked --group ml python -m citibike.risk run --data-dir data --forecast data/f4/forecasts/1e204ac4a944381529e00b500586a00a82c7b8eca31cd8f4c978b1c282d81810 --gbfs data/f5/live-inputs/gbfs/silver/f2-gbfs-2/20261030T063651137970-27bd3fb5 --as-of 2026-10-30T06:36:54.163891+00:00 --config data/f5/config.json
uv run --locked --group ml python -m citibike.risk verify --result data/f5/results/99f703bf3372edad673fc40e43bc6ea6746ff2a44a263aea5e4983234983c83a
```

Все actual child argv/exit codes и HTTP hooks приведены в JSON; старые controlled/rejected evidence остались неизменными. Field calibration/hidden demand/operator money/production SLA и strict F4 promotion не доказаны. F6–F8 NOT RUN.

Следующий шаг после delivery — независимая приёмка F5 с явно указанным recorded-as-of scope; F6 не начинать.
