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
import { Kind, nyTime, percent, SiteHeader, StatePanel, short, show, Term } from "../ui";

export const dynamic = "force-dynamic";
const POLICY = {
  no_action: "Ничего не делать",
  heuristic: "Эвристика",
  optimizer: "MILP",
} as const;

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
  return (
    <main className="product-page">
      <SiteHeader current="/scenarios" params={params} />
      <section className="intro compact">
        <p className="eyebrow">F5 · СЦЕНАРНЫЕ РИСКИ И РЕКОМЕНДАЦИИ (СИМУЛЯЦИЯ)</p>
        <h1>Сценарии, а не распоряжения.</h1>
        <p className="lead">
          Read-only результат проверенной F5 симуляции: no-action, пороговая эвристика и
          ограниченная MILP-оптимизация на одном потоке случайностей. Не оперативные распоряжения
          Citi Bike и не доказанная экономия.
        </p>
      </section>
      {failure ? (
        <StatePanel state={failure.state} title="Сценарии недоступны">
          <p data-scenario-state={failure.state}>{failure.text}</p>
        </StatePanel>
      ) : !data || data.status === "empty" ? (
        <StatePanel state="empty" title="Сценарии не опубликованы">
          <p data-scenario-state="empty">{data?.message ?? "Нет активной F5 публикации."}</p>
        </StatePanel>
      ) : (
        <>
          <section
            className={`panel ${archival ? "state-expired" : ""}`}
            data-scenario-state={data.status}
            data-scenario-id={String(pub?.publication_id)}
          >
            <Kind kind="simulation" />
            <h2>
              {archival
                ? "Архивный recorded-as-of replay — не текущая рекомендация"
                : "Сценарий в пределах TTL начального состояния"}
            </h2>
            <p>{data.currency?.reason}</p>
            <dl>
              <dt>As-of (GBFS начального состояния)</dt>
              <dd data-scenario-as-of={String(pub?.data_as_of)}>{nyTime(pub?.data_as_of)}</dd>
              <dt>Горизонт</dt>
              <dd>
                {nyTime(pub?.horizon_start)} → {nyTime(pub?.horizon_end)}
              </dd>
              <dt>Результат создан</dt>
              <dd>
                {nyTime(pub?.created_at)} · режим {String(pub?.mode)} · {String(pub?.data_mode)}
              </dd>
              <dt>Возраст as-of / TTL</dt>
              <dd>
                {show((data.currency?.as_of_age_seconds ?? 0) / 3600)} ч /{" "}
                {data.currency?.ttl_seconds} с
              </dd>
              <dt>Coverage</dt>
              <dd data-scenario-coverage={`${coverage.eligible}/${coverage.requested}`}>
                {coverage.eligible}/{coverage.requested} станций F4 cohort (
                {percent(coverage.fraction)}); остальные исключены с причиной, риск = N/A
              </dd>
              <dt>Параметры сценария</dt>
              <dd>
                λ (cost_weight) {String(config.cost_weight)}; парк {String(config.vehicles)} ×{" "}
                {String(config.vehicle_capacity)} велосипедов; {String(config.speed_kmh)} км/ч +{" "}
                {String(config.handling_minutes)} мин; пороги эвристики{" "}
                {String(config.low_threshold)}/{String(config.high_threshold)};{" "}
                {String(config.repeats)} повторов, seed {String(config.seed)}
              </dd>
              <dt>Lineage</dt>
              <dd className="hash">
                F5 {short(pub?.publication_id)} · F4{" "}
                {short((pub?.provenance as Record<string, string>)?.forecast_id)} · GBFS{" "}
                {String((pub?.provenance as Record<string, string>)?.gbfs_collection_id)} · verify{" "}
                {String((pub?.verification as Record<string, string>)?.status)} (
                {String((pub?.verification as Record<string, number>)?.paired_draws)} draws)
              </dd>
              <dt>Operational promotion</dt>
              <dd>{String(pub?.operational_promotion)}</dd>
            </dl>
          </section>
          <form className="filters panel" action="/scenarios" method="get">
            <label>
              Масштаб спроса
              <select name="scale" defaultValue={String(data.filters.scale)}>
                {((config.scales as number[]) ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Порядок событий
              <select name="event_order" defaultValue={String(data.filters.event_order)}>
                {((config.orders as string[]) ?? []).map((o) => (
                  <option key={o}>{o}</option>
                ))}
              </select>
            </label>
            <button className="refresh" type="submit">
              Показать
            </button>
          </form>
          <section className="panel">
            <h2>
              Политики: <Term name="simulated_unserved">simulated unserved</Term>,{" "}
              <Term name="objective">loss + λ·cost</Term>
            </h2>
            <p data-scenario-cells={cells.length}>
              Показано ячеек масштаб × порядок: {cells.length}; меньший objective лучше. Отметка
              «лучший» — по objective внутри ячейки, ничьи показываются все.
            </p>
            <p data-scenario-action={noActionBest ? "no_action" : "compare_only"}>
              {noActionBest
                ? "Перевозки не рекомендуются: no-action не хуже по loss + λ·cost во всех показанных ячейках. Перемещения эвристики и MILP ниже — только сценарные альтернативы."
                : "Перемещения ниже — сценарные альтернативы. Победа по objective в отдельных ячейках сама по себе не подтверждает устойчивую пользу и не разрешает перевозки."}
            </p>
            <Chart
              label="Scenario objective по политикам для каждой ячейки"
              spec={{
                kind: "bar",
                unit: "",
                categories: cells.map((c) => c.replace("|", " · ")),
                series: (["no_action", "heuristic", "optimizer"] as const).map((p) => ({
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
            <div className="table-scroll">
              <table data-scenario-outcomes={data.outcomes.length}>
                <thead>
                  <tr>
                    <th>Масштаб · порядок</th>
                    <th>Политика</th>
                    <th>Unserved, среднее (p05–p95)</th>
                    <th>Перемещено / машин</th>
                    <th>Movement cost</th>
                    <th>Objective</th>
                    <th>Нарушения</th>
                  </tr>
                </thead>
                <tbody>
                  {data.outcomes.map((o) => {
                    const key = `${o.scale}|${o.event_order}`;
                    const winner = best.get(key)?.includes(o.policy);
                    return (
                      <tr key={`${key}-${o.policy}`} data-best={winner ? "1" : "0"}>
                        <td>
                          {o.scale} · {o.event_order}
                        </td>
                        <td>{POLICY[o.policy]}</td>
                        <td>
                          {show(o.totals.unserved_total.mean, 3)} (
                          {show(o.totals.unserved_total.p05, 3)}–
                          {show(o.totals.unserved_total.p95, 3)})
                        </td>
                        <td>
                          {o.moved_bikes} / {o.vehicles}
                        </td>
                        <td>{show(o.movement_cost, 3)}</td>
                        <td>
                          {show(o.totals.scenario_objective.mean, 3)}
                          {winner && <span className="badge good">лучший</span>}
                        </td>
                        <td>{o.constraint_violations}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panels">
            <article className="panel">
              <Kind kind="simulation" />
              <h2>Перемещения при масштабе {data.filters.scale}</h2>
              {data.transfers.length === 0 ? (
                <p>Нет перемещений.</p>
              ) : (
                <ul>
                  {data.transfers.map((t) => (
                    <li key={`${t.policy}-${t.source}-${t.destination}`}>
                      {POLICY[t.policy as keyof typeof POLICY]}: {t.source} → {t.destination},{" "}
                      {t.bikes} вел., {t.vehicles} машин, {show(t.distance_km, 2)} км (Haversine
                      proxy), {show(t.minutes, 1)} мин
                    </li>
                  ))}
                </ul>
              )}
              <p>Расстояния и время — сценарные допущения, не измеренные маршруты улиц.</p>
            </article>
            <article className="panel">
              <Kind kind="simulation" />
              <h2>
                <Term name="empty_full_risk">Риск станций</Term> ({data.filters.scale} ·{" "}
                {data.filters.event_order})
              </h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Станция</th>
                      <th>Политика</th>
                      <th>Пустая (Wilson 95%)</th>
                      <th>Полная (Wilson 95%)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.station_outcomes
                      .filter((o) => o.status === "ok")
                      .map((o) => (
                        <tr key={`${o.station_id}-${o.policy}`}>
                          <td>
                            <Link
                              href={withParams("/stations", new URLSearchParams(), {
                                station_id: o.station_id,
                              })}
                            >
                              {o.station_id}
                            </Link>
                          </td>
                          <td>{POLICY[o.policy as keyof typeof POLICY]}</td>
                          <td>
                            {percent(o.empty_fraction, 0)} ({percent(o.empty_low, 0)}–
                            {percent(o.empty_high, 0)})
                          </td>
                          <td>
                            {percent(o.full_fraction, 0)} ({percent(o.full_low, 0)}–
                            {percent(o.full_high, 0)})
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
              <p>Монте-Карло интервалы отражают точность симуляции, не калибровку риска.</p>
            </article>
          </section>
          <section className="panels">
            <article className="panel">
              <h2>Исключённые станции ({excluded.length})</h2>
              <ul>
                {[...reasons].map(([reason, count]) => (
                  <li key={reason}>
                    {reason}: {count}
                  </li>
                ))}
              </ul>
              <details>
                <summary>Все исключения</summary>
                <ul>
                  {excluded.map((s) => (
                    <li key={s.station_id}>
                      {s.station_id}: {s.reasons.join(", ")}
                    </li>
                  ))}
                </ul>
              </details>
            </article>
            <article className="panel">
              <h2>Допущения симулятора</h2>
              <ol className="small-list">
                {((pub?.assumptions as string[]) ?? []).map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ol>
            </article>
          </section>
        </>
      )}
    </main>
  );
}
