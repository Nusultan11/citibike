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
import {
  Icon,
  Kind,
  nyTime,
  PageHead,
  SiteFooter,
  SiteHeader,
  StatePanel,
  Status,
  short,
  show,
  Term,
  type Tone,
} from "../ui";

export const dynamic = "force-dynamic";

const STATE = {
  valid: {
    tone: "success",
    label: "действителен",
    title: "Прогноз действителен",
    icon: "check",
  },
  expired: {
    tone: "warning",
    label: "истёк",
    title: "Прогноз недоступен: окно истекло",
    icon: "clock",
  },
  empty: {
    tone: "neutral",
    label: "не опубликован",
    title: "Прогноз не опубликован",
    icon: "database",
  },
  unavailable: {
    tone: "warning",
    label: "недоступен",
    title: "Прогноз недоступен",
    icon: "off",
  },
} as const;

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
  const now = new Date();
  const state = data
    ? forecastState(data.status, pub?.valid_until as string | undefined, now)
    : "unavailable";
  const meta = STATE[state];
  const rows = state === "valid" ? (data?.rows ?? []) : [];
  const horizons = Array.from({ length: 24 }, (_, i) => i + 1);
  const value = (type: string, h: number) =>
    rows.find((r) => r.prediction_type === type && r.horizon_h === h);
  const freshness = pub?.freshness as Record<string, number> | undefined;
  const origin = Date.parse(String(pub?.forecast_origin_utc ?? ""));
  const until = Date.parse(String(pub?.valid_until ?? ""));
  const span = Number.isFinite(origin) && Number.isFinite(until) && until > origin;
  const elapsed = span ? Math.min(1, Math.max(0, (now.getTime() - origin) / (until - origin))) : 0;
  const validRows = Number(pub?.valid_rows ?? 0);
  const unavailableRows = Number(pub?.unavailable_rows ?? 0);
  const rowShare = validRows + unavailableRows > 0 ? validRows / (validRows + unavailableRows) : 0;
  return (
    <>
      <SiteHeader current="/forecasts" params={params} />
      <main id="content" className="product-page">
        <PageHead
          eyebrow={
            <>
              <Icon name="forecast" size={13} /> Прогноз F4 · опубликованные поездки
            </>
          }
          title="Прогноз — только пока он действителен"
          lead={
            <>
              Показывается лишь активная публикация до её <code>valid_until</code>. Истёкший или
              устаревший прогноз помечается как недоступный и не выдаётся за свежую оценку.
            </>
          }
        >
          <Kind kind="forecast" />
        </PageHead>

        <form
          className="filterbar static"
          action="/forecasts"
          method="get"
          aria-label="Станция прогноза"
        >
          <div className="filter-row">
            <label className="field grow">
              Станция (cohort F4)
              <input
                name="station_id"
                maxLength={80}
                defaultValue={station ?? ""}
                placeholder="Пусто — первая станция cohort"
              />
            </label>
            <div className="filter-actions">
              <Link className="btn btn-ghost" href="/forecasts">
                Все
              </Link>
              <button className="btn btn-primary" type="submit">
                Показать
              </button>
            </div>
          </div>
        </form>

        {failure ? (
          <StatePanel
            state={failure.state}
            title={failure.state === "error" ? "Запрос не подходит" : "Прогноз недоступен"}
          >
            <p data-forecast-page={failure.state}>{failure.text}</p>
          </StatePanel>
        ) : (
          <>
            <section
              className={`callout tone-${meta.tone} state-${state}`}
              data-forecast-page={state}
              role="status"
            >
              <span className="callout-icon">
                <Icon name={meta.icon} size={17} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="card-head" style={{ marginBottom: 6 }}>
                  <h2>{meta.title}</h2>
                  <Status tone={meta.tone as Tone}>{meta.label}</Status>
                </div>
                <p>
                  {state === "expired"
                    ? `Окно действия закрылось ${nyTime(pub?.valid_until)}. Значения скрыты: новая публикация требует отдельного batch F4/F7, модель из интерфейса не запускается.`
                    : state === "valid"
                      ? "Значения ниже — оценки опубликованных поездок, не факт и не скрытый спрос."
                      : state === "empty"
                        ? "Нет активной публикации прогноза."
                        : "Все строки выбранного среза недоступны или ответ не прошёл проверку."}
                </p>
                {span && (
                  <>
                    <div className="timeline" aria-hidden="true">
                      <i
                        style={{
                          width: `${elapsed * 100}%`,
                          background: state === "valid" ? "var(--success)" : "var(--warning)",
                          opacity: 0.7,
                        }}
                      />
                      <span className="now" style={{ left: `calc(${elapsed * 100}% - 1px)` }} />
                    </div>
                    <div className="timeline-labels">
                      <span>origin {nyTime(pub?.forecast_origin_utc)}</span>
                      <span>сейчас {state === "valid" ? "внутри окна" : "после окна"}</span>
                      <span>valid_until {nyTime(pub?.valid_until)}</span>
                    </div>
                  </>
                )}
              </div>
            </section>

            {pub && (
              <div className="grid" style={{ marginTop: 16 }}>
                <section className="card span-7">
                  <div className="card-head">
                    <div>
                      <h2>Provenance публикации</h2>
                      <p className="card-sub">Из чего и когда собран этот прогноз.</p>
                    </div>
                  </div>
                  <div className="lineage">
                    <span className="chip">
                      F2 <span className="mono">{short(pub.source_snapshot_id)}</span>
                    </span>
                    <span className="arrow">→</span>
                    <span className="chip">
                      F3 <span className="mono">{short(pub.gold_publication_id)}</span>
                    </span>
                    <span className="arrow">→</span>
                    <span className="chip">
                      dataset <span className="mono">{short(pub.dataset_id)}</span>
                    </span>
                    <span className="arrow">→</span>
                    <span className="chip">
                      bundle <span className="mono">{short(pub.model_bundle_id)}</span>
                    </span>
                  </div>
                  <dl className="facts" style={{ marginTop: 16 }}>
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
                    <dt>Данные as-of</dt>
                    <dd>
                      {nyTime(pub.data_as_of)} ({String(pub.data_as_of_kind)}); возраст источника{" "}
                      {show(freshness?.source_age_days)} из {show(freshness?.max_source_age_days)}{" "}
                      дней
                    </dd>
                    <dt>Ограничения</dt>
                    <dd>{String(pub.limitations)}</dd>
                  </dl>
                </section>
                <section className="card span-5">
                  <div className="card-head">
                    <h2>Модель и покрытие</h2>
                    <Status tone="info">Seasonal Naive</Status>
                  </div>
                  <p className="body-text">
                    Frozen selection F4: delayed Seasonal Naive baseline для отправлений и прибытий.
                    Исследовательские кандидаты точнее в среднем, но не прошли gate деградации
                    сегментов; operational promotion — NOT VERIFIED. Семейство модели в
                    serving-контракте F4 не публикуется.
                  </p>
                  <div style={{ marginTop: 14 }}>
                    <div className="share-legend" style={{ justifyContent: "space-between" }}>
                      <span>
                        {show(validRows)} valid / {show(unavailableRows)} unavailable строк
                      </span>
                      <span>{show(rowShare * 100)}%</span>
                    </div>
                    <div className="share-bar" style={{ marginTop: 6 }} aria-hidden="true">
                      <span style={{ width: `${rowShare * 100}%`, background: "var(--success)" }} />
                    </div>
                  </div>
                  <dl className="facts" style={{ marginTop: 14 }}>
                    <dt>Quality</dt>
                    <dd>{String(pub.quality_status)}</dd>
                    <dt>Accuracy evidence</dt>
                    <dd>{String(pub.accuracy_evidence_mode)}</dd>
                    <dt>Availability</dt>
                    <dd>{String(pub.availability_mode)}</dd>
                  </dl>
                  <div className="card-foot">
                    <span>Выводы исследований F4</span>
                    <Link className="link-arrow" href="/analytics#findings">
                      Что проверяли <Icon name="arrow" size={14} />
                    </Link>
                  </div>
                </section>
              </div>
            )}

            {state === "valid" ? (
              <section
                className="card"
                style={{ marginTop: 16 }}
                data-forecast-values={rows.length}
              >
                <div className="card-head">
                  <div>
                    <h2>
                      <Term name="forecast">
                        {station ? `Станция ${station}` : `Станция ${rows[0]?.station_id ?? "N/A"}`}
                      </Term>
                    </h2>
                    <p className="card-sub">Оценка поездок по горизонту 1–24 ч; не факт.</p>
                  </div>
                  <Kind kind="forecast" />
                </div>
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
                <details className="disclosure" open>
                  <summary>Таблица по горизонтам</summary>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Горизонт</th>
                          <th>Целевой час (NY)</th>
                          <th className="num">Отправления</th>
                          <th className="num">Прибытия</th>
                        </tr>
                      </thead>
                      <tbody>
                        {horizons.map((h) => (
                          <tr key={h}>
                            <td>+{h} ч</td>
                            <td>{nyTime(value("departures", h)?.target_hour_utc)}</td>
                            {(["departures", "arrivals"] as const).map((type) => {
                              const point = value(type, h);
                              return (
                                <td key={type} className="num">
                                  {point?.status === "ok" ? (
                                    show(point.predicted_value, 2)
                                  ) : (
                                    <span className="badge warn">
                                      unavailable: {point?.reason ?? "нет строки"}
                                    </span>
                                  )}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </section>
            ) : (
              <section className="card" style={{ marginTop: 16 }}>
                <div className="empty-state">
                  <Icon name="forecast" size={22} />
                  <strong>Значения прогноза скрыты</strong>
                  <span>
                    Пока нет действительной публикации, здесь не показываются числа. Исторические
                    факты по станциям доступны в аналитике.
                  </span>
                  <span className="meta-row" style={{ justifyContent: "center", marginTop: 6 }}>
                    <Link
                      className="btn btn-secondary btn-sm"
                      href={withParams("/stations", params, { station_id: station ?? "" })}
                    >
                      Карта станций
                    </Link>
                    <Link
                      className="btn btn-secondary btn-sm"
                      href={withParams("/analytics", params, { station_id: station ?? "" })}
                    >
                      История поездок
                    </Link>
                  </span>
                </div>
              </section>
            )}
            {station && (
              <p style={{ marginTop: 16 }}>
                <Link
                  className="link-arrow"
                  href={withParams("/stations", params, { station_id: station })}
                >
                  Карточка станции {station} <Icon name="arrow" size={14} />
                </Link>
              </p>
            )}
          </>
        )}
      </main>
      <SiteFooter />
    </>
  );
}
