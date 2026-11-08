import Link from "next/link";
import { type AnalyticsData, type AnalyticsRow, analytics } from "../../lib/analytics";
import {
  evidenceHref,
  FINDING_AREAS,
  FINDINGS,
  type FindingStatus,
  findingPage,
  findingPopulation,
} from "../../lib/findings";
import {
  addDays,
  comparisonWindow,
  daysBetween,
  getStations,
  localHourProfile,
  nyDate,
  splitPartner,
  withParams,
} from "../../lib/operations";
import Chart, { SERIES } from "../chart";
import {
  AppliedFilters,
  Delta,
  EvidenceNote,
  Icon,
  Kind,
  PageHead,
  percent,
  range,
  SiteFooter,
  SiteHeader,
  Status,
  short,
  show,
  TableScroll,
  Term,
  type Tone,
} from "../ui";

export const dynamic = "force-dynamic";
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const DAY = /^\d{4}-\d{2}-\d{2}$/;

type Comparison = {
  comparable: boolean | null;
  previous_start: string | null;
  previous_end: string | null;
  previous_rides: number | null;
  relative_change: number | null;
  reason: string | null;
};

const STATUS_TONE: Record<FindingStatus, Tone> = {
  verified: "success",
  limited: "warning",
  not_verified: "neutral",
};
const STATUS_LABEL: Record<FindingStatus, string> = {
  verified: "VERIFIED",
  limited: "LIMITED",
  not_verified: "NOT VERIFIED",
};

function axisLabel(key: string) {
  if (/^\d{4}-\d{2}-\d{2}T/.test(key)) return `${key.slice(5, 10)} ${key.slice(11, 16)}Z`;
  return key;
}

export default async function AnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const params = new URLSearchParams();
  let error = "";
  for (const key of [
    "start_date",
    "end_date",
    "station_id",
    "to_station",
    "vehicle",
    "membership",
    "grain",
    "publication_id",
    "compare_start",
    "compare_end",
    "event_type",
  ]) {
    const value = input[key];
    if (Array.isArray(value)) error = "Каждый фильтр должен быть задан один раз.";
    else if (value) params.set(key, value);
  }
  const chipParams = new URLSearchParams(params);
  const routeParams = new URLSearchParams(params);
  const compareStart = params.get("compare_start");
  const compareEnd = params.get("compare_end");
  const eventType = params.get("event_type") === "end" ? "end" : "start";
  for (const key of ["to_station", "compare_start", "compare_end", "event_type"])
    params.delete(key);
  for (const key of ["compare_start", "compare_end", "event_type"]) routeParams.delete(key);
  let kpi: AnalyticsData | null = null;
  let series: AnalyticsData | null = null;
  let stations: AnalyticsData | null = null;
  let routes: AnalyticsData | null = null;
  let categories: AnalyticsData | null = null;
  let peaks: AnalyticsData | null = null;
  let monthly: AnalyticsData | null = null;
  let hourly: AnalyticsData | null = null;
  let geography: AnalyticsData | null = null;
  let compare: (AnalyticsData & { comparison?: Comparison }) | null = null;
  let flags = new Map<string, string | null>();
  let routeError = "";
  let geoError = "";
  let compareError = "";
  let hourWindow: { start: string; end: string } | null = null;
  let baseline: { start: string; end: string; rule: string } | null = null;
  try {
    if (error) throw new Error(error);
    // The ID-split universe query is independent of KPI, so it starts in parallel;
    // its publication is checked against the KPI publication below.
    const stationParams = new URLSearchParams(params);
    stationParams.delete("station_id");
    stationParams.delete("grain");
    const flagRequest = getStations(stationParams).then(
      (value) => ({ status: "fulfilled" as const, value }),
      (reason: unknown) => ({ status: "rejected" as const, reason }),
    );
    kpi = await analytics("kpi", params);
    const pub = kpi.publication;
    if (pub) {
      params.set("publication_id", pub.publication_id);
      routeParams.set("publication_id", pub.publication_id);
      const { start_date: start, end_date: end } = kpi.filters;
      baseline =
        compareStart && compareEnd
          ? { start: compareStart, end: compareEnd, rule: "заданный период" }
          : comparisonWindow(start, end, nyDate(pub.period_start));
      hourWindow = { start: daysBetween(start, end) > 28 ? addDays(end, -28) : start, end };
      const hourlyParams = new URLSearchParams(params);
      hourlyParams.set("grain", "hour");
      hourlyParams.set("start_date", hourWindow.start);
      hourlyParams.set("end_date", hourWindow.end);
      const monthlyParams = new URLSearchParams(params);
      monthlyParams.delete("start_date");
      monthlyParams.delete("end_date");
      monthlyParams.set("grain", "month");
      const peakParams = new URLSearchParams(params);
      peakParams.set("grain", params.get("grain") === "hour" ? "hour" : "day");
      [series, stations, categories, peaks, monthly, hourly] = await Promise.all([
        analytics("timeseries", params),
        analytics("stations", params),
        analytics("categories", params),
        analytics("peaks", peakParams),
        analytics("timeseries", monthlyParams),
        analytics("timeseries", hourlyParams),
      ]);
      const geoParams = new URLSearchParams(params);
      geoParams.set("event_type", eventType);
      geoParams.set("limit", "100");
      const compareParams = new URLSearchParams(params);
      if (baseline) {
        compareParams.set("compare_start", baseline.start);
        compareParams.set("compare_end", baseline.end);
      }
      const [routeResult, geoResult, compareResult, flagResult] = await Promise.allSettled([
        analytics("routes", routeParams),
        analytics("geography", geoParams),
        baseline ? analytics("compare", compareParams) : Promise.reject(new Error("")),
        flagRequest,
      ]);
      if (routeResult.status === "fulfilled") routes = routeResult.value;
      else
        routeError = `${routeResult.reason instanceof Error ? routeResult.reason.message : "Данные недоступны."} Выберите весь опубликованный период, ≤ 31 дня либо станцию отправления / назначения.`;
      if (geoResult.status === "fulfilled") geography = geoResult.value;
      else geoError = "География доступна для периода ≤ 31 дня или выбранной станции.";
      if (compareResult.status === "fulfilled") compare = compareResult.value;
      else
        compareError = baseline
          ? "Сравнение недоступно: базовый период вне окна публикации или фильтры не поддержаны."
          : "Нет сопоставимого базового периода внутри публикации.";
      if (
        flagResult.status === "fulfilled" &&
        flagResult.value.status === "fulfilled" &&
        flagResult.value.value.publication?.publication_id === pub.publication_id
      )
        flags = new Map(flagResult.value.value.rows.map((r) => [r.station_id, r.split_partner]));
    }
  } catch (cause) {
    error = cause instanceof Error ? cause.message : "Ошибка аналитического API";
  }
  const pub = kpi?.publication;
  const href = (station: string | null, destination?: string | null) => {
    const next = new URLSearchParams(params);
    next.set("station_id", station ?? "");
    if (destination) next.set("to_station", destination);
    return `/analytics?${next}`;
  };
  const universe = new Set(flags.keys());
  const partner = (id: string | null) =>
    id ? (flags.get(id) ?? splitPartner(id, universe)) : null;
  const table = (rows: AnalyticsRow[], route: boolean) => {
    const max = Math.max(1, ...rows.map((r) => r.metrics?.completed_published_rides ?? 0));
    return (
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th>{route ? "Маршрут" : "Станция"}</th>
              <th className="num">Поездки</th>
              {!route && (
                <>
                  <th className="num">
                    <Term name="departures">Отпр.</Term>
                  </th>
                  <th className="num">
                    <Term name="arrivals">Приб.</Term>
                  </th>
                  <th className="num">
                    <Term name="net_trip_flow">Net</Term>
                  </th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const pair = route ? null : partner(r.station_id);
              const rides = r.metrics?.completed_published_rides ?? null;
              return (
                <tr key={r.key}>
                  <td>
                    <span className="rank">{i + 1}</span>
                    <Link
                      href={href(
                        route ? r.from_station : r.station_id,
                        route ? r.to_station : null,
                      )}
                      title="Отфильтровать аналитику"
                    >
                      {r.key}
                    </Link>
                    {!route && r.station_id && (
                      <>
                        {" "}
                        <Link
                          className="badge info"
                          href={withParams("/stations", params, { station_id: r.station_id })}
                          aria-label={`Карточка станции ${r.station_id}`}
                        >
                          ↗
                        </Link>
                      </>
                    )}
                    {pair && (
                      <span className="badge warn" data-id-split={r.station_id}>
                        ◇ ID-split с {pair}
                      </span>
                    )}
                  </td>
                  <td className="num">
                    <span className="cell-bar">
                      <i style={{ width: `${((rides ?? 0) / max) * 64}px` }} />
                      {show(rides)}
                    </span>
                  </td>
                  {!route && (
                    <>
                      <td className="num">{show(r.metrics?.departures)}</td>
                      <td className="num">{show(r.metrics?.arrivals)}</td>
                      <td className="num">{show(r.metrics?.net_flow)}</td>
                    </>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </TableScroll>
    );
  };
  const profile = localHourProfile(
    (hourly?.rows ?? []).map((r) => ({
      key: r.key,
      value: r.metrics?.completed_published_rides ?? null,
    })),
  );
  const comparison = compare?.comparison;
  const seriesRows = series?.rows ?? [];
  const lastDay = pub ? nyDate(pub.period_end) : null;
  const advancedOpen = Boolean(
    chipParams.get("station_id") ||
      chipParams.get("to_station") ||
      compareStart ||
      compareEnd ||
      chipParams.get("event_type"),
  );
  const presets = lastDay
    ? [
        { label: "7 дн.", start: addDays(lastDay, -7), end: lastDay },
        { label: "28 дн.", start: addDays(lastDay, -28), end: lastDay },
        { label: "Весь период", start: "", end: "" },
      ]
    : [];
  const categoryRows = categories?.rows ?? [];
  return (
    <>
      <SiteHeader current="/analytics" params={params} />
      <main id="content" className="analytics-page product-page">
        {pub?.data_mode === "controlled_fixture" && (
          <p className="callout tone-warning" role="status" style={{ marginBottom: 16 }}>
            Тестовые синтетические данные · не фактические показатели Citi Bike.
          </p>
        )}
        <PageHead
          eyebrow={
            <>
              <Icon name="analytics" size={13} /> Глубокая аналитика · факты поездок
            </>
          }
          title="История поездок в деталях"
          lead="Отправления — по времени начала, прибытия — по времени окончания. Это опубликованные поездки, а не скрытый спрос и не наличие велосипедов. Клик по графику или строке таблицы — drill-down."
        >
          {kpi && (
            <span className="chip">
              <Icon name="clock" size={13} /> {range(kpi.filters.start_date, kpi.filters.end_date)}
            </span>
          )}
          {pub && (
            <span className="chip">
              <Icon name="database" size={13} />{" "}
              <span className="mono">{short(pub.publication_id)}</span>
            </span>
          )}
          <a className="chip" href="#findings">
            <Icon name="spark" size={13} /> Выводы исследований ↓
          </a>
        </PageHead>

        <form
          key={JSON.stringify(input)}
          action="/analytics"
          method="get"
          className="filterbar"
          aria-label="Фильтры аналитики"
        >
          {pub && <input type="hidden" name="publication_id" value={pub.publication_id} />}
          <div className="filter-row">
            <label className="field">
              Начало, NY
              <input
                type="date"
                name="start_date"
                defaultValue={params.get("start_date") ?? kpi?.filters.start_date ?? ""}
                required
              />
            </label>
            <label className="field">
              Конец, не включён
              <input
                type="date"
                name="end_date"
                defaultValue={params.get("end_date") ?? kpi?.filters.end_date ?? ""}
                required
              />
            </label>
            <label className="field">
              Велосипед
              <select name="vehicle" defaultValue={params.get("vehicle") ?? ""}>
                <option value="">Все</option>
                <option value="classic_bike">Классический</option>
                <option value="electric_bike">Электро</option>
                <option value="unknown">Неизвестно</option>
              </select>
            </label>
            <label className="field">
              Пользователь
              <select name="membership" defaultValue={params.get("membership") ?? ""}>
                <option value="">Все</option>
                <option value="member">Подписчик</option>
                <option value="casual">Разовый</option>
                <option value="unknown">Неизвестно</option>
              </select>
            </label>
            <label className="field">
              Интервал
              <select name="grain" defaultValue={params.get("grain") ?? "day"}>
                <option value="day">День NY</option>
                <option value="month">Месяц NY</option>
                <option value="hour">Час UTC (≤ 31 дня)</option>
              </select>
            </label>
            {presets.length > 0 && (
              <div className="field">
                Быстрый период
                <nav className="segmented" aria-label="Быстрый выбор периода">
                  {presets.map((p) => (
                    <Link
                      key={p.label}
                      href={withParams("/analytics", chipParams, {
                        start_date: p.start,
                        end_date: p.end,
                        grain: p.start ? (chipParams.get("grain") ?? "") : "",
                      })}
                      aria-current={
                        (p.start === "" && !chipParams.get("start_date")) ||
                        (p.start === chipParams.get("start_date") &&
                          p.end === chipParams.get("end_date"))
                          ? "true"
                          : undefined
                      }
                    >
                      {p.label}
                    </Link>
                  ))}
                </nav>
              </div>
            )}
            <div className="filter-actions">
              <Link className="btn btn-ghost" href="/analytics">
                Сбросить
              </Link>
              <button className="btn btn-primary" type="submit">
                Применить
              </button>
            </div>
          </div>
          <details className="filter-more" open={advancedOpen}>
            <summary>Станции, сравнение и география</summary>
            <div className="filter-row">
              <label className="field grow">
                Станция отправления
                <input
                  name="station_id"
                  maxLength={80}
                  defaultValue={params.get("station_id") ?? ""}
                  placeholder="Все станции, например 6611.07"
                />
              </label>
              <label className="field grow">
                Маршрут → станция назначения
                <input
                  name="to_station"
                  maxLength={80}
                  defaultValue={routeParams.get("to_station") ?? ""}
                  placeholder="Влияет только на таблицу маршрутов"
                />
              </label>
              <label className="field">
                Сравнить с: начало
                <input type="date" name="compare_start" defaultValue={compareStart ?? ""} />
              </label>
              <label className="field">
                Сравнить с: конец
                <input type="date" name="compare_end" defaultValue={compareEnd ?? ""} />
              </label>
              <label className="field">
                География
                <select name="event_type" defaultValue={eventType}>
                  <option value="start">Начала поездок</option>
                  <option value="end">Окончания поездок</option>
                </select>
              </label>
            </div>
          </details>
          <AppliedFilters
            path="/analytics"
            params={chipParams}
            keys={{
              station_id: "Станция",
              to_station: "Назначение",
              vehicle: "Велосипед",
              membership: "Пользователь",
              compare_start: "Сравнение с",
              grain: "Интервал",
            }}
          />
        </form>

        {error && (
          <section className="callout tone-danger" role="alert" data-analytics-state="error">
            <span className="callout-icon">
              <Icon name="alert" size={17} />
            </span>
            <div>
              <h2>Не удалось загрузить аналитику</h2>
              <p>{error}</p>
              <p className="actions">
                <Link className="btn btn-secondary btn-sm" href="/analytics">
                  Открыть активную публикацию
                </Link>
              </p>
            </div>
          </section>
        )}
        {!error && (!pub || kpi?.status === "empty") && (
          <section className="callout tone-neutral" role="status" data-analytics-state="empty">
            <span className="callout-icon">
              <Icon name="search" size={17} />
            </span>
            <div>
              <h2>Нет опубликованных наблюдений</h2>
              <p>
                {kpi?.message ??
                  "Для этих фильтров наблюдения отсутствуют. N/A не заменяется нулём."}
              </p>
            </div>
          </section>
        )}
        {!error && pub && kpi?.metrics && (
          <>
            <section
              className="kpi-row"
              aria-label="Ключевые показатели периода"
              data-analytics-state="ok"
              data-publication-id={pub.publication_id}
            >
              <article className="card kpi-card hero">
                <div className="kpi-label">
                  <Term name="completed_published_rides">Поездки</Term>
                  <Kind kind="fact" compact />
                </div>
                <strong
                  className="kpi"
                  data-analytics-completed-rides={kpi.metrics.completed_published_rides}
                >
                  {show(kpi.metrics.completed_published_rides)}
                </strong>
                <div className="kpi-split">
                  <Delta value={comparison?.relative_change} />
                  <span className="kpi-desc">
                    {comparison
                      ? `к ${range(comparison.previous_start, comparison.previous_end)}`
                      : "нет сопоставимой базы"}
                  </span>
                </div>
              </article>
              <article className="card kpi-card">
                <div className="kpi-label">
                  <Term name="average_duration">Средняя длительность</Term>
                  <Kind kind="fact" compact />
                </div>
                <strong className="kpi">
                  {kpi.metrics.average_duration_seconds == null ? (
                    "N/A"
                  ) : (
                    <>
                      {show(kpi.metrics.average_duration_seconds / 60)}
                      <span className="kpi-unit">мин</span>
                    </>
                  )}
                </strong>
                <p className="kpi-desc">
                  {show(kpi.metrics.average_duration_seconds)} с — сумма длительностей / число
                  поездок с допустимой длительностью.
                </p>
              </article>
              <article className="card kpi-card">
                <div className="kpi-label">
                  <span>
                    <Term name="departures">Отправления</Term> /{" "}
                    <Term name="arrivals">прибытия</Term>
                  </span>
                </div>
                <div className="kpi-pair">
                  <span>Отпр.</span>
                  <strong className="kpi sm">{show(kpi.metrics.departures)}</strong>
                  <span>Приб.</span>
                  <strong className="kpi sm">{show(kpi.metrics.arrivals)}</strong>
                </div>
                <p className="kpi-desc">Только известные station ID.</p>
              </article>
              <article className="card kpi-card">
                <div className="kpi-label">
                  <Term name="net_trip_flow">Net flow · маршруты</Term>
                </div>
                <strong className="kpi">{show(kpi.metrics.net_flow)}</strong>
                <p className="kpi-desc">
                  Не изменение inventory. <Term name="route_volume">Покрытие маршрутов</Term>:{" "}
                  {percent(kpi.metrics.route_coverage)}.
                </p>
              </article>
            </section>

            <div className="grid" style={{ marginTop: 16 }}>
              <section className="card span-8">
                <div className="card-head">
                  <div>
                    <h2>Динамика поездок</h2>
                    <p className="card-sub">
                      Часы — UTC, дни и месяцы — America/New_York. Пропуски не заполнены нулями.
                      Клик по дню открывает почасовой разрез, по месяцу — дни месяца.
                    </p>
                  </div>
                  <Kind kind="fact" />
                </div>
                <Chart
                  size="tall"
                  label="Динамика допустимых поездок; часовые подписи UTC, дни и месяцы Нью-Йорка"
                  spec={{
                    kind: "line",
                    unit: "поездок",
                    peak: true,
                    categories: seriesRows.map((r) => axisLabel(r.key)),
                    drill: {
                      base: withParams("/analytics", params),
                      to: params.get("grain") === "month" ? "day" : "hour",
                    },
                    series: [
                      {
                        name: "Поездки",
                        values: seriesRows.map((r) => r.metrics?.completed_published_rides ?? null),
                      },
                    ],
                  }}
                />
                <details className="disclosure">
                  <summary>Таблица временного ряда</summary>
                  <TableScroll>
                    <table className="num-cols">
                      <thead>
                        <tr>
                          <th>Период</th>
                          <th>Поездки</th>
                          <th>Отправления</th>
                          <th>Прибытия</th>
                        </tr>
                      </thead>
                      <tbody>
                        {seriesRows.map((r) => (
                          <tr key={r.key}>
                            <td>
                              {DAY.test(r.key) ? (
                                <Link
                                  href={withParams("/analytics", params, {
                                    start_date: r.key,
                                    end_date: addDays(r.key, 1),
                                    grain: "hour",
                                  })}
                                >
                                  {r.key}
                                </Link>
                              ) : (
                                r.key
                              )}
                            </td>
                            <td>{show(r.metrics?.completed_published_rides)}</td>
                            <td>{show(r.metrics?.departures)}</td>
                            <td>{show(r.metrics?.arrivals)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableScroll>
                </details>
              </section>

              <div className="span-4 stack">
                <section className="card" data-compare-state={comparison?.comparable ? "ok" : "na"}>
                  <div className="card-head">
                    <h2>
                      <Term name="period_change">Равное сравнение</Term>
                    </h2>
                    {comparison?.comparable ? (
                      <Status tone="success">сопоставимо</Status>
                    ) : (
                      <Status tone="neutral">N/A</Status>
                    )}
                  </div>
                  {comparison ? (
                    <>
                      <div className="stat-pair" style={{ gridTemplateColumns: "1fr 1fr" }}>
                        <div className="stat">
                          <span>{range(kpi.filters.start_date, kpi.filters.end_date)}</span>
                          <strong>{show(kpi.metrics.completed_published_rides)}</strong>
                        </div>
                        <div className="stat">
                          <span>{range(comparison.previous_start, comparison.previous_end)}</span>
                          <strong>{show(comparison.previous_rides)}</strong>
                        </div>
                      </div>
                      <p className="body-text" style={{ marginTop: 12 }}>
                        Изменение:{" "}
                        <b data-compare-change={comparison.relative_change ?? "NA"}>
                          {percent(comparison.relative_change)}
                        </b>{" "}
                        · база: {baseline?.rule}
                      </p>
                      <p className="note" style={{ marginTop: 6 }}>
                        {comparison.reason ??
                          "Одинаковые NY-дни, часы UTC, набор weekday и покрытие."}
                      </p>
                    </>
                  ) : (
                    <p className="body-text">{compareError}</p>
                  )}
                </section>
                <section className="card" data-peaks={peaks?.rows.length ?? 0}>
                  <div className="card-head">
                    <div>
                      <h2>Пики</h2>
                      <p className="card-sub">
                        Отдельно по сторонам, интервал{" "}
                        {peaks?.coverage.peak_grain === "hour" ? "час UTC" : "NY-день"}; среди
                        равных — самый ранний.
                      </p>
                    </div>
                  </div>
                  <ul className="list-clean">
                    {peaks?.rows.map((r) => (
                      <li key={`${r.dimension}-${r.key}`} className="row-link">
                        <span>
                          {r.dimension === "departures" ? "Отправления" : "Прибытия"}
                          <span className="cell-sub mono">{r.key}</span>
                        </span>
                        <b>{show(r.n)}</b>
                      </li>
                    ))}
                  </ul>
                </section>
              </div>

              <section className="card span-7">
                <div className="card-head">
                  <div>
                    <h2>Профиль недели: день × час NY</h2>
                    <p className="card-sub">
                      Сумма начал за {range(hourWindow?.start, hourWindow?.end)} (≤ 28 дней, конец
                      периода). Только наблюдённые часы; в ночь смены времени 01:00 учитывает два
                      UTC-часа, пропущенный 02:00 отсутствует.
                    </p>
                  </div>
                  <Kind kind="fact" />
                </div>
                <Chart
                  size="heat"
                  label="Тепловая карта поездок по дню недели и локальному часу"
                  spec={{
                    kind: "heatmap",
                    unit: "поездок",
                    x: Array.from({ length: 24 }, (_, h) => String(h)),
                    y: WEEKDAYS,
                    cells: profile.map((c) => [c.hour, c.weekday, c.value]),
                  }}
                />
                <details className="disclosure">
                  <summary>Таблица профиля</summary>
                  <TableScroll>
                    <table className="num-cols">
                      <thead>
                        <tr>
                          <th>День</th>
                          <th>Час NY</th>
                          <th>Поездки</th>
                          <th>Наблюдённых UTC-часов</th>
                        </tr>
                      </thead>
                      <tbody>
                        {profile.map((c) => (
                          <tr key={`${c.weekday}-${c.hour}`}>
                            <td>{WEEKDAYS[c.weekday]}</td>
                            <td>{c.hour}</td>
                            <td>{show(c.value)}</td>
                            <td>{c.hours}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableScroll>
                </details>
              </section>
              <section className="card span-5">
                <div className="card-head">
                  <div>
                    <h2>Сезонность по месяцам</h2>
                    <p className="card-sub">
                      Весь опубликованный период, NY-месяцы, те же фильтры. Клик по месяцу — открыть
                      его по дням.
                    </p>
                  </div>
                </div>
                <Chart
                  size="heat"
                  label="Поездки по NY-месяцам за весь опубликованный период"
                  spec={{
                    kind: "bar",
                    unit: "поездок",
                    peak: true,
                    categories: (monthly?.rows ?? []).map((r) => r.key.slice(0, 7)),
                    drill: { base: withParams("/analytics", params), to: "day" },
                    series: [
                      {
                        name: "Поездки",
                        values: (monthly?.rows ?? []).map(
                          (r) => r.metrics?.completed_published_rides ?? null,
                        ),
                      },
                    ],
                  }}
                />
              </section>

              <section className="card span-6">
                <div className="card-head">
                  <div>
                    <h2>Станции</h2>
                    <p className="card-sub">
                      Первые 20 по числу начал. ID — фильтр аналитики, «карточка» — профиль станции.
                    </p>
                  </div>
                  <Kind kind="fact" />
                </div>
                {table(stations?.rows ?? [], false)}
              </section>
              <section
                className="card span-6"
                data-route-state={routeError ? "error" : (routes?.status ?? "unavailable")}
              >
                <div className="card-head">
                  <div>
                    <h2>
                      <Term name="route_volume">Маршруты</Term>
                    </h2>
                    <p className="card-sub">
                      Обе станции известны. Покрытие: {percent(kpi.metrics.route_coverage)} ·
                      показано{" "}
                      {routes ? `${routes.rows.length} из ${show(routes.total_groups)} пар` : "N/A"}
                    </p>
                  </div>
                  <Kind kind="fact" />
                </div>
                {routeError && (
                  <p className="callout tone-warning" role="status" style={{ marginBottom: 10 }}>
                    {routeError}
                  </p>
                )}
                {routes && table(routes.rows, true)}
              </section>

              <section className="card span-5">
                <div className="card-head">
                  <div>
                    <h2>
                      <Term name="trip_category_share">Категории</Term>
                    </h2>
                    <p className="card-sub">
                      Доля по известным значениям внутри измерения; unknown — отдельно.
                    </p>
                  </div>
                </div>
                <ul className="list-clean">
                  {categoryRows.map((r, i) => (
                    <li key={r.key} style={{ padding: "10px 0" }}>
                      <div className="share-legend" style={{ justifyContent: "space-between" }}>
                        <span style={{ color: "var(--text-1)" }}>{r.key}</span>
                        <span>
                          {show(r.n)} · {percent(r.known_share)} · unknown {show(r.unknown_count)}
                        </span>
                      </div>
                      <div className="share-bar" aria-hidden="true" style={{ marginTop: 6 }}>
                        {r.known_share != null && (
                          <span
                            style={{
                              width: `${r.known_share * 100}%`,
                              background: SERIES[r.key.startsWith("membership") ? 2 : 0],
                              opacity: i % 2 ? 0.75 : 1,
                            }}
                          />
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
              {!!geography?.rows.length && (
                <section className="card span-7" data-geo-state={geography.status}>
                  <div className="card-head">
                    <div>
                      <h2>Гео-агрегаты · сетка 0,01°</h2>
                      <p className="card-sub">
                        {eventType === "start" ? "Начала" : "Окончания"} по координатам самих
                        событий, не районы.
                      </p>
                    </div>
                    <Kind kind="fact" />
                  </div>
                  {geoError ? (
                    <p className="callout tone-neutral" role="status">
                      {geoError}
                    </p>
                  ) : (
                    <>
                      <p className="note" style={{ marginBottom: 10 }}>
                        Покрытие {percent(geography?.coverage.eligible_coverage as number)} ·
                        показано {geography?.rows.length ?? 0} из {geography?.total_groups ?? "N/A"}{" "}
                        ячеек.
                      </p>
                      <TableScroll style={{ maxHeight: 320 }}>
                        <table>
                          <thead>
                            <tr>
                              <th>Ячейка lat, lon (юго-запад)</th>
                              <th className="num">События</th>
                            </tr>
                          </thead>
                          <tbody>
                            {geography?.rows.slice(0, 30).map((r) => {
                              const max = geography?.rows[0]?.n ?? 1;
                              return (
                                <tr key={r.key}>
                                  <td className="mono">
                                    {r.cell_lat == null
                                      ? r.key
                                      : `${(r.cell_lat / 100).toFixed(2)}, ${((r.cell_lon ?? 0) / 100).toFixed(2)}`}
                                  </td>
                                  <td className="num">
                                    <span className="cell-bar">
                                      <i
                                        style={{
                                          width: `${((r.n ?? 0) / Math.max(1, max)) * 80}px`,
                                          background: SERIES[2],
                                        }}
                                      />
                                      {show(r.n)}
                                    </span>
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </TableScroll>
                    </>
                  )}
                </section>
              )}
              {geoError && <p className="note span-12">{geoError}</p>}
            </div>
          </>
        )}

        <Findings />

        <div className="grid" style={{ marginTop: 16 }}>
          {pub && kpi && (
            <details className="disclosure card span-12 provenance">
              <summary>Происхождение текущего среза и покрытие</summary>
              <div className="card-head">
                <h2>Происхождение, покрытие и качество</h2>
                <Status tone={pub.quality_status === "pass" ? "success" : "warning"}>
                  {pub.quality_status}
                </Status>
              </div>
              <dl className="facts">
                <dt>Источник</dt>
                <dd>
                  {pub.source} · {pub.scope} · {pub.timezone}
                </dd>
                <dt>Период</dt>
                <dd>
                  {kpi.filters.start_date} → {kpi.filters.end_date}, [start, end)
                </dd>
                <dt>Population</dt>
                <dd>{pub.population}</dd>
                <dt>Silver as-of</dt>
                <dd>{pub.data_as_of}</dd>
                <dt>Pipeline</dt>
                <dd>{pub.pipeline_version}</dd>
                <dt>Публикация F3</dt>
                <dd className="hash">{pub.publication_id}</dd>
                <dt>Снимок F2</dt>
                <dd className="hash">{pub.source_snapshot_id}</dd>
              </dl>
              <p className="note" style={{ marginTop: 10 }}>
                Районы не присваиваются; география — по координатам событий. Пары ID{" "}
                <code>dddd.d</code> / <code>dddd.d0</code> помечены ◇, Silver не объединялся.
              </p>
            </details>
          )}
        </div>
      </main>
      <SiteFooter />
    </>
  );
}

/** Recorded research results: static evidence, independent of the filters above. */
function Findings() {
  const findings = FINDINGS.filter((f) => findingPage(f) === "/analytics");
  const areas = Object.entries(FINDING_AREAS).filter(([area]) =>
    findings.some((f) => f.area === area),
  );
  return (
    <section id="findings" className="findings-wrap" aria-labelledby="findings-title">
      <div className="section-title">
        <div>
          <h2 id="findings-title">Что мы проверили и что увидели</h2>
          <p>
            Зафиксированные наблюдения о данных и поездках, F2/F3 и аналитическая часть decision
            research. Не зависят от фильтров выше и не пересчитываются в интерфейсе.
          </p>
        </div>
        <div className="legend" style={{ marginTop: 0 }}>
          <Status tone="success">VERIFIED</Status>
          <Status tone="warning">LIMITED</Status>
          <Status tone="neutral">NOT VERIFIED</Status>
        </div>
      </div>
      <EvidenceNote source="docs/evidence/f3-analytics.json" status="VERIFIED">
        F3, 29.10.2026: независимая сверка Silver → Gold → API → HTML, 0 расхождений. 92 076 054
        начала в окне 2024-10–2026-09; ещё 513 canonical строк начинаются вне этого окна. Это разные
        популяции, а не потерянные поездки.
      </EvidenceNote>
      <fieldset className="findings-filter segmented" aria-label="Область исследований">
        <label>
          <input
            className="seg-input"
            type="radio"
            name="finding-area"
            value="all"
            defaultChecked
          />
          <span className="seg-label">Все · {findings.length}</span>
        </label>
        {areas.map(([area, label]) => (
          <label key={area}>
            <input className="seg-input" type="radio" name="finding-area" value={area} />
            <span className="seg-label">
              {label} · {findings.filter((f) => f.area === area).length}
            </span>
          </label>
        ))}
      </fieldset>
      <div className="findings" data-findings={findings.length}>
        {findings.map((f) => (
          <article key={f.id} className="finding" data-area={f.area} data-finding={f.id}>
            <div className="finding-top">
              <span className="finding-area">{FINDING_AREAS[f.area]}</span>
              <Status tone={STATUS_TONE[f.status]}>{STATUS_LABEL[f.status]}</Status>
            </div>
            <span className="finding-value">{f.value}</span>
            <h3>{f.title}</h3>
            <p>{f.takeaway}</p>
            <p className="note">{f.limitation}</p>
            <details>
              <summary>Период, популяция и источник</summary>
              <p>{findingPopulation(f)}</p>
              <p>
                <a href={evidenceHref(f.source)}>{f.source} ↗</a> · {f.recorded}
              </p>
              {f.relatedSources?.map((source) => (
                <p key={source}>
                  <a href={evidenceHref(source)}>{source} ↗</a>
                </p>
              ))}
            </details>
          </article>
        ))}
      </div>
    </section>
  );
}
