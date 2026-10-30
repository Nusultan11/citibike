# F5 — Risk & Optimization: PARTIAL

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
