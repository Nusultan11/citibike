import Link from "next/link";
import {
  bestPolicies,
  getRisk,
  message,
  type RiskData,
  readFilters,
  withParams,
} from "../../lib/operations";
import Chart from "../chart";
import {
  Icon,
  Kind,
  nyTime,
  PageHead,
  percent,
  RiskBar,
  SiteFooter,
  SiteHeader,
  StatePanel,
  Status,
  short,
  show,
  Term,
} from "../ui";

export const dynamic = "force-dynamic";
const POLICY = {
  no_action: "Ничего не делать",
  heuristic: "Эвристика",
  optimizer: "MILP",
} as const;
const POLICY_NOTE = {
  no_action: "База сравнения: стартовое состояние без перевозок.",
  heuristic: "Пороги заполненности и ближайшие пары станций.",
  optimizer: "Ограниченная оптимизация inventory-target surrogate.",
} as const;
const POLICIES = ["no_action", "heuristic", "optimizer"] as const;

export default async function ScenariosPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const { params, error } = readFilters(input);
  const query = new URLSearchParams();
  for (const key of ["scale", "event_order", "station_id"]) {
    const value = input[key];
    if (typeof value === "string" && value) query.set(key, value);
  }
  let data: RiskData | null = null;
  let failure: { state: "unavailable" | "error"; text: string } | null = error
    ? { state: "error", text: error }
    : null;
  if (!failure) {
    try {
      data = await getRisk(query);
    } catch (cause) {
      failure = message(cause);
    }
  }
  const pub = data?.publication ?? null;
  const config = (pub?.config ?? {}) as Record<string, number | number[] | string[]>;
  const coverage = (pub?.coverage ?? {}) as Record<string, number>;
  const best = data ? bestPolicies(data.outcomes) : new Map<string, string[]>();
  const noActionBest =
    best.size > 0 && [...best.values()].every((policies) => policies.includes("no_action"));
  const cells = [...new Set(data?.outcomes.map((o) => `${o.scale}|${o.event_order}`) ?? [])];
  const excluded = data?.stations.filter((s) => !s.eligible) ?? [];
  const reasons = new Map<string, number>();
  for (const s of excluded)
    for (const r of s.reasons) {
      const key = r.split(":").slice(0, 2).join(":");
      reasons.set(key, (reasons.get(key) ?? 0) + 1);
    }
  const archival = data?.status === "archival_replay";
  const selectedKey = data ? `${data.filters.scale}|${data.filters.event_order}` : "";
  const selected =
    data?.outcomes.filter((o) => `${o.scale}|${o.event_order}` === selectedKey) ?? [];
  const scales = (config.scales as number[] | undefined) ?? [];
  const orders = (config.orders as string[] | undefined) ?? [];
  const provenance = (pub?.provenance ?? {}) as Record<string, string>;
  const verification = (pub?.verification ?? {}) as Record<string, string | number>;
  const cellHref = (scale: string | number, order: string) =>
    withParams("/scenarios", query, { scale: String(scale), event_order: order });
  return (
    <>
      <SiteHeader current="/scenarios" params={params} />
      <main id="content" className="product-page">
        <PageHead
          eyebrow={
            <>
              <Icon name="scenarios" size={13} /> Сценарии F5 · симуляция
            </>
          }
          title="Сценарии, а не распоряжения"
          lead="Read-only результат проверенной симуляции: «ничего не делать», пороговая эвристика и ограниченная MILP-оптимизация на одном потоке случайностей. Не оперативные распоряжения Citi Bike и не доказанная экономия."
        >
          <Kind kind="simulation" />
        </PageHead>

        {failure ? (
          <StatePanel
            state={failure.state}
            title={failure.state === "error" ? "Запрос не подходит" : "Сценарии недоступны"}
          >
            <p data-scenario-state={failure.state}>{failure.text}</p>
          </StatePanel>
        ) : !data || data.status === "empty" ? (
          <StatePanel state="empty" title="Сценарии не опубликованы">
            <p data-scenario-state="empty">{data?.message ?? "Нет активной F5 публикации."}</p>
          </StatePanel>
        ) : (
          <>
            <section
              className={`callout ${archival ? "tone-warning" : "tone-info"}`}
              data-scenario-state={data.status}
              data-scenario-id={String(pub?.publication_id)}
            >
              <span className="callout-icon">
                <Icon name={archival ? "clock" : "info"} size={17} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="card-head" style={{ marginBottom: 6 }}>
                  <h2>
                    {archival
                      ? "Архивный recorded-as-of replay — не текущая рекомендация"
                      : "Сценарий в пределах TTL начального состояния"}
                  </h2>
                  <Status tone={archival ? "warning" : "success"}>
                    {archival ? "архив" : "текущий"}
                  </Status>
                </div>
                <p>{data.currency?.reason}</p>
                <div className="meta-row" style={{ marginTop: 10 }}>
                  <span className="chip">
                    as-of{" "}
                    <b data-scenario-as-of={String(pub?.data_as_of)}>{nyTime(pub?.data_as_of)}</b>
                  </span>
                  <span className="chip">
                    возраст <b>{show((data.currency?.as_of_age_seconds ?? 0) / 3600)} ч</b> / TTL{" "}
                    {data.currency?.ttl_seconds} с
                  </span>
                  <span className="chip">
                    покрытие{" "}
                    <b data-scenario-coverage={`${coverage.eligible}/${coverage.requested}`}>
                      {coverage.eligible}/{coverage.requested}
                    </b>{" "}
                    станций ({percent(coverage.fraction)})
                  </span>
                </div>
              </div>
            </section>

            <section
              className={`callout ${noActionBest ? "tone-success" : "tone-info"}`}
              style={{ marginTop: 12 }}
              data-scenario-action={noActionBest ? "no_action" : "compare_only"}
              data-scenario-cells={cells.length}
            >
              <span className="callout-icon">
                <Icon name={noActionBest ? "check" : "scenarios"} size={17} />
              </span>
              <div>
                <h2>
                  {noActionBest
                    ? "Перевозки не рекомендуются"
                    : "Только сравнение: устойчивой пользы перевозок не показано"}
                </h2>
                <p>
                  {noActionBest
                    ? `«Ничего не делать» не хуже по loss + λ·cost во всех ${cells.length} показанных ячейках масштаба × порядка событий. Перемещения эвристики и MILP ниже — только сценарные альтернативы.`
                    : `Победа по objective в отдельных из ${cells.length} ячеек сама по себе не подтверждает устойчивую пользу и не разрешает перевозки.`}
                </p>
              </div>
            </section>

            <div className="section-title">
              <div>
                <h2>
                  Политики в выбранной ячейке · <Term name="objective">loss + λ·cost</Term>
                </h2>
                <p>Меньший objective лучше; ничьи отмечаются все.</p>
              </div>
              <div className="meta-row">
                {scales.length > 0 && (
                  <nav className="segmented" aria-label="Масштаб спроса">
                    {scales.map((s) => (
                      <Link
                        key={s}
                        href={cellHref(s, String(data?.filters.event_order))}
                        aria-current={s === data?.filters.scale ? "true" : undefined}
                      >
                        ×{s}
                      </Link>
                    ))}
                  </nav>
                )}
                {orders.length > 0 && (
                  <nav className="segmented" aria-label="Порядок событий">
                    {orders.map((o) => (
                      <Link
                        key={o}
                        href={cellHref(String(data?.filters.scale), o)}
                        aria-current={o === data?.filters.event_order ? "true" : undefined}
                      >
                        {o}
                      </Link>
                    ))}
                  </nav>
                )}
              </div>
            </div>
            <div className="policy-grid">
              {POLICIES.map((p) => {
                const o = selected.find((x) => x.policy === p);
                const winner = best.get(selectedKey)?.includes(p);
                return (
                  <article key={p} className={`card policy${winner ? " best" : ""}`}>
                    <div className="card-head" style={{ marginBottom: 0 }}>
                      <div>
                        <h3>{POLICY[p]}</h3>
                        <p className="card-sub">{POLICY_NOTE[p]}</p>
                      </div>
                      {winner ? (
                        <Status tone="success">лучший</Status>
                      ) : (
                        <Status tone="neutral">альтернатива</Status>
                      )}
                    </div>
                    {o ? (
                      <>
                        <div className="metric">
                          <span>Objective (среднее)</span>
                          <strong className="big">
                            {show(o.totals.scenario_objective.mean, 3)}
                          </strong>
                        </div>
                        <div className="policy-metrics">
                          <div className="metric">
                            <span>
                              <Term name="simulated_unserved">Unserved</Term> (p05–p95)
                            </span>
                            <strong>{show(o.totals.unserved_total.mean, 3)}</strong>
                            <span>
                              {show(o.totals.unserved_total.p05, 3)}–
                              {show(o.totals.unserved_total.p95, 3)}
                            </span>
                          </div>
                          <div className="metric">
                            <span>Перемещено / машин</span>
                            <strong>
                              {o.moved_bikes} / {o.vehicles}
                            </strong>
                          </div>
                          <div className="metric">
                            <span>Movement cost</span>
                            <strong>{show(o.movement_cost, 3)}</strong>
                          </div>
                          <div className="metric">
                            <span>Нарушения ограничений</span>
                            <strong>{o.constraint_violations}</strong>
                          </div>
                        </div>
                        {o.gain_vs_no_action != null && p !== "no_action" && (
                          <p className="note">
                            Меньше simulated unserved, чем у no-action:{" "}
                            {show(o.gain_vs_no_action, 3)} — до учёта стоимости перемещений; итог по
                            objective выше. Не фактическая экономия.
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="body-text">Нет результата для этой ячейки.</p>
                    )}
                  </article>
                );
              })}
            </div>

            <section className="card" style={{ marginTop: 16 }}>
              <div className="card-head">
                <div>
                  <h2>Objective по всем ячейкам</h2>
                  <p className="card-sub">
                    {cells.length} ячеек масштаб × порядок; столбцы одной ячейки получили один и тот
                    же поток случайностей.
                  </p>
                </div>
                <Kind kind="simulation" />
              </div>
              <Chart
                label="Scenario objective по политикам для каждой ячейки"
                spec={{
                  kind: "bar",
                  unit: "",
                  categories: cells.map((c) => c.replace("|", " · ")),
                  links: cells.map((c) => {
                    const [scale, order] = c.split("|");
                    return cellHref(scale, order);
                  }),
                  series: POLICIES.map((p) => ({
                    name: POLICY[p],
                    values: cells.map(
                      (c) =>
                        data?.outcomes.find(
                          (o) => `${o.scale}|${o.event_order}` === c && o.policy === p,
                        )?.totals.scenario_objective.mean ?? null,
                    ),
                  })),
                }}
              />
              <details className="disclosure">
                <summary>Таблица всех исходов · {data.outcomes.length}</summary>
                <div className="table-scroll">
                  <table data-scenario-outcomes={data.outcomes.length}>
                    <thead>
                      <tr>
                        <th>Масштаб · порядок</th>
                        <th>Политика</th>
                        <th className="num">Unserved (p05–p95)</th>
                        <th className="num">Перемещено / машин</th>
                        <th className="num">Movement cost</th>
                        <th className="num">Objective</th>
                        <th className="num">Нарушения</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.outcomes.map((o) => {
                        const key = `${o.scale}|${o.event_order}`;
                        const winner = best.get(key)?.includes(o.policy);
                        return (
                          <tr key={`${key}-${o.policy}`} data-best={winner ? "1" : "0"}>
                            <td>
                              <Link href={cellHref(o.scale, o.event_order)}>
                                ×{o.scale} · {o.event_order}
                              </Link>
                            </td>
                            <td>{POLICY[o.policy]}</td>
                            <td className="num">
                              {show(o.totals.unserved_total.mean, 3)} (
                              {show(o.totals.unserved_total.p05, 3)}–
                              {show(o.totals.unserved_total.p95, 3)})
                            </td>
                            <td className="num">
                              {o.moved_bikes} / {o.vehicles}
                            </td>
                            <td className="num">{show(o.movement_cost, 3)}</td>
                            <td className="num">
                              {show(o.totals.scenario_objective.mean, 3)}
                              {winner && <span className="badge good">✓ лучший</span>}
                            </td>
                            <td className="num">{o.constraint_violations}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </details>
            </section>

            <div className="grid" style={{ marginTop: 16 }}>
              <section className="card span-5">
                <div className="card-head">
                  <div>
                    <h2>Перемещения при масштабе ×{data.filters.scale}</h2>
                    <p className="card-sub">
                      Расстояния и время — сценарные допущения (Haversine proxy), не маршруты улиц.
                    </p>
                  </div>
                  <Kind kind="simulation" />
                </div>
                {data.transfers.length === 0 ? (
                  <div className="empty-state" style={{ minHeight: 120 }}>
                    <strong>Нет перемещений</strong>
                  </div>
                ) : (
                  <ul className="list-clean">
                    {data.transfers.map((t) => (
                      <li
                        key={`${t.policy}-${t.source}-${t.destination}`}
                        className="row-link"
                        style={{ alignItems: "flex-start" }}
                      >
                        <span>
                          <span className="badge info" style={{ marginLeft: 0 }}>
                            {POLICY[t.policy as keyof typeof POLICY]}
                          </span>
                          <span className="mono" style={{ display: "block", marginTop: 4 }}>
                            {t.source} → {t.destination}
                          </span>
                        </span>
                        <span style={{ textAlign: "right" }}>
                          <b>{t.bikes} вел.</b>
                          <span className="cell-sub">
                            {t.vehicles} маш. · {show(t.distance_km, 2)} км · {show(t.minutes, 1)}{" "}
                            мин
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              <section className="card span-7">
                <div className="card-head">
                  <div>
                    <h2>
                      <Term name="empty_full_risk">Риск станций</Term> · ×{data.filters.scale} ·{" "}
                      {data.filters.event_order}
                    </h2>
                    <p className="card-sub">
                      Полоса — доля розыгрышей, рамка — Wilson 95% (точность Монте-Карло, не
                      калибровка).
                    </p>
                  </div>
                  <Kind kind="simulation" />
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Станция</th>
                        <th>Политика</th>
                        <th>Пустая</th>
                        <th>Полная</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.station_outcomes
                        .filter((o) => o.status === "ok")
                        .map((o) => (
                          <tr key={`${o.station_id}-${o.policy}`}>
                            <td>
                              <Link
                                className="mono"
                                href={withParams("/stations", new URLSearchParams(), {
                                  station_id: o.station_id,
                                })}
                              >
                                {o.station_id}
                              </Link>
                            </td>
                            <td>{POLICY[o.policy as keyof typeof POLICY]}</td>
                            <td>
                              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                <RiskBar
                                  value={o.empty_fraction}
                                  low={o.empty_low}
                                  high={o.empty_high}
                                />
                                <span className="cell-sub" style={{ display: "inline" }}>
                                  {percent(o.empty_fraction, 0)} ({percent(o.empty_low, 0)}–
                                  {percent(o.empty_high, 0)})
                                </span>
                              </span>
                            </td>
                            <td>
                              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                <RiskBar
                                  value={o.full_fraction}
                                  low={o.full_low}
                                  high={o.full_high}
                                />
                                <span className="cell-sub" style={{ display: "inline" }}>
                                  {percent(o.full_fraction, 0)} ({percent(o.full_low, 0)}–
                                  {percent(o.full_high, 0)})
                                </span>
                              </span>
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="card span-4">
                <div className="card-head">
                  <h2>Исключённые станции · {excluded.length}</h2>
                </div>
                <ul className="list-clean">
                  {[...reasons].map(([reason, count]) => (
                    <li key={reason} className="row-link">
                      <span className="mono" style={{ fontSize: 12 }}>
                        {reason}
                      </span>
                      <b>{count}</b>
                    </li>
                  ))}
                </ul>
                <details className="disclosure">
                  <summary>Все исключения</summary>
                  <ul className="bullets small">
                    {excluded.map((s) => (
                      <li key={s.station_id}>
                        <span className="mono">{s.station_id}</span>: {s.reasons.join(", ")}
                      </li>
                    ))}
                  </ul>
                </details>
              </section>
              <section className="card span-4">
                <div className="card-head">
                  <h2>Параметры и lineage</h2>
                </div>
                <dl className="facts">
                  <dt>λ (cost_weight)</dt>
                  <dd>{String(config.cost_weight)}</dd>
                  <dt>Парк</dt>
                  <dd>
                    {String(config.vehicles)} × {String(config.vehicle_capacity)} велосипедов;{" "}
                    {String(config.speed_kmh)} км/ч + {String(config.handling_minutes)} мин
                  </dd>
                  <dt>Пороги эвристики</dt>
                  <dd>
                    {String(config.low_threshold)} / {String(config.high_threshold)}
                  </dd>
                  <dt>Повторов · seed</dt>
                  <dd>
                    {String(config.repeats)} · {String(config.seed)}
                  </dd>
                  <dt>Горизонт</dt>
                  <dd>
                    {nyTime(pub?.horizon_start)} → {nyTime(pub?.horizon_end)}
                  </dd>
                  <dt>Создан</dt>
                  <dd>
                    {nyTime(pub?.created_at)} · {String(pub?.mode)} · {String(pub?.data_mode)}
                  </dd>
                  <dt>Lineage</dt>
                  <dd className="hash">
                    F5 {short(pub?.publication_id)} · F4 {short(provenance.forecast_id)} · GBFS{" "}
                    {String(provenance.gbfs_collection_id)}
                  </dd>
                  <dt>Verify</dt>
                  <dd>
                    {String(verification.status)} ({String(verification.paired_draws)} draws)
                  </dd>
                  <dt>Operational promotion</dt>
                  <dd>{String(pub?.operational_promotion)}</dd>
                </dl>
              </section>
              <section className="card span-4">
                <div className="card-head">
                  <h2>Допущения симулятора</h2>
                </div>
                <details className="disclosure" style={{ marginTop: 0 }}>
                  <summary>
                    Показать {((pub?.assumptions as string[]) ?? []).length} допущений
                  </summary>
                  <ol className="bullets small">
                    {((pub?.assumptions as string[]) ?? []).map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ol>
                </details>
                <p className="note" style={{ marginTop: 10 }}>
                  Ключевое: спрос — Poisson с прогнозом как средним, перевозки стартуют на as-of,
                  оптимизатор оптимален только для surrogate-цели.
                </p>
              </section>
            </div>
          </>
        )}
      </main>
      <SiteFooter />
    </>
  );
}
