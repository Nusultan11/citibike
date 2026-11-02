import Link from "next/link";
import {
  type ForecastData,
  forecastState,
  getForecasts,
  message,
  readFilters,
  withParams,
} from "../../lib/operations";
import Chart from "../chart";
import { Kind, nyTime, SiteHeader, StatePanel, short, show, Term } from "../ui";

export const dynamic = "force-dynamic";

export default async function ForecastsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const { params, error } = readFilters(input);
  const station = params.get("station_id");
  const query = new URLSearchParams({ limit: "48" });
  if (station) query.set("station_id", station);
  let data: ForecastData | null = null;
  let failure: { state: "unavailable" | "error"; text: string } | null = error
    ? { state: "error", text: error }
    : null;
  if (!failure) {
    try {
      data = await getForecasts(query);
    } catch (cause) {
      failure = message(cause);
    }
  }
  const pub = data?.publication ?? null;
  const state = data
    ? forecastState(data.status, pub?.valid_until as string | undefined, new Date())
    : "unavailable";
  const rows = state === "valid" ? (data?.rows ?? []) : [];
  const horizons = Array.from({ length: 24 }, (_, i) => i + 1);
  const value = (type: string, h: number) =>
    rows.find((r) => r.prediction_type === type && r.horizon_h === h);
  const freshness = pub?.freshness as Record<string, number> | undefined;
  return (
    <main className="product-page">
      <SiteHeader current="/forecasts" params={params} />
      <section className="intro compact">
        <p className="eyebrow">ПРОГНОЗ F4 · ОПУБЛИКОВАННЫЕ НАБЛЮДАЕМЫЕ ПОЕЗДКИ</p>
        <h1>Прогноз, только если он действителен.</h1>
        <p className="lead">
          Показывается лишь активная опубликованная версия до её <code>valid_until</code>. Истёкший
          или устаревший прогноз — <b>unavailable</b>, а не свежая оценка.
        </p>
      </section>
      <form className="filters panel" action="/forecasts" method="get">
        <label>
          Станция (cohort F4)
          <input name="station_id" maxLength={80} defaultValue={station ?? ""} />
        </label>
        <button className="refresh" type="submit">
          Показать
        </button>
        <Link href="/forecasts">Все</Link>
      </form>
      {failure ? (
        <StatePanel state={failure.state} title="Прогноз недоступен">
          <p data-forecast-page={failure.state}>{failure.text}</p>
        </StatePanel>
      ) : (
        <>
          <section className={`panel state-${state}`} data-forecast-page={state}>
            <Kind kind="forecast" />
            <h2>
              {state === "valid"
                ? "Действительный прогноз"
                : state === "expired"
                  ? "Прогноз недоступен: окно истекло"
                  : state === "empty"
                    ? "Прогноз не опубликован"
                    : "Прогноз недоступен"}
            </h2>
            <p>
              {state === "expired"
                ? `valid_until ${nyTime(pub?.valid_until)} уже прошёл. Значения скрыты; новая публикация требует отдельного batch F4/F7, а не запуска модели из UI.`
                : state === "valid"
                  ? "Значения — оценки опубликованных поездок, не факт и не скрытый спрос."
                  : state === "empty"
                    ? "Нет активной публикации прогноза."
                    : "Все строки выбранного среза unavailable или ответ не прошёл проверку."}
            </p>
            {pub && (
              <dl>
                <dt>Publication</dt>
                <dd className="hash">{String(pub.publication_id)}</dd>
                <dt>Forecast origin</dt>
                <dd>{nyTime(pub.forecast_origin_utc)}</dd>
                <dt>Горизонт</dt>
                <dd>1–24 ч после origin (UTC-часы)</dd>
                <dt>valid_until</dt>
                <dd data-forecast-valid-until={String(pub.valid_until)}>
                  {nyTime(pub.valid_until)}
                </dd>
                <dt>Модель / baseline</dt>
                <dd>
                  bundle {short(pub.model_bundle_id)}; F4 frozen selection: delayed Seasonal Naive
                  baseline для departures и arrivals; operational promotion NOT VERIFIED. Семейство
                  модели не публикуется в serving-контракте F4.
                </dd>
                <dt>Coverage</dt>
                <dd>
                  {show(pub.valid_rows as number)} valid / {show(pub.unavailable_rows as number)}{" "}
                  unavailable строк
                </dd>
                <dt>Quality</dt>
                <dd>
                  {String(pub.quality_status)} · accuracy evidence:{" "}
                  {String(pub.accuracy_evidence_mode)} · {String(pub.availability_mode)}
                </dd>
                <dt>Данные as-of</dt>
                <dd>
                  {nyTime(pub.data_as_of)} ({String(pub.data_as_of_kind)}); возраст источника{" "}
                  {show(freshness?.source_age_days)} из {show(freshness?.max_source_age_days)} дней
                </dd>
                <dt>Lineage</dt>
                <dd className="hash">
                  dataset {short(pub.dataset_id)} · F2 {short(pub.source_snapshot_id)} · F3{" "}
                  {short(pub.gold_publication_id)}
                </dd>
                <dt>Ограничения</dt>
                <dd>{String(pub.limitations)}</dd>
              </dl>
            )}
          </section>
          {state === "valid" && (
            <section className="panel" data-forecast-values={rows.length}>
              <h2>
                <Term name="forecast">
                  {station ? `Станция ${station}` : `Станция ${rows[0]?.station_id ?? "N/A"}`}
                </Term>
              </h2>
              <Chart
                label="Прогноз отправлений и прибытий по горизонту"
                spec={{
                  kind: "line",
                  unit: "поездок",
                  categories: horizons.map((h) => `+${h} ч`),
                  series: ["departures", "arrivals"].map((type) => ({
                    name: type === "departures" ? "Отправления" : "Прибытия",
                    values: horizons.map((h) => value(type, h)?.predicted_value ?? null),
                  })),
                }}
              />
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Горизонт</th>
                      <th>Целевой час (NY)</th>
                      <th>Отправления</th>
                      <th>Прибытия</th>
                    </tr>
                  </thead>
                  <tbody>
                    {horizons.map((h) => (
                      <tr key={h}>
                        <td>+{h}</td>
                        <td>{nyTime(value("departures", h)?.target_hour_utc)}</td>
                        <td>
                          {value("departures", h)?.status === "ok"
                            ? show(value("departures", h)?.predicted_value, 2)
                            : `unavailable: ${value("departures", h)?.reason ?? "нет строки"}`}
                        </td>
                        <td>
                          {value("arrivals", h)?.status === "ok"
                            ? show(value("arrivals", h)?.predicted_value, 2)
                            : `unavailable: ${value("arrivals", h)?.reason ?? "нет строки"}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
          {station && (
            <p>
              <Link href={withParams("/stations", params, { station_id: station })}>
                Карточка станции {station} ↗
              </Link>
            </p>
          )}
        </>
      )}
    </main>
  );
}
