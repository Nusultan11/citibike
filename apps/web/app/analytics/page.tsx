import Link from "next/link";
import { type AnalyticsData, type AnalyticsRow, analytics } from "../../lib/analytics";
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
import Chart from "../chart";
import { Kind, percent, SiteHeader, show, Term } from "../ui";
import AnalyticsTrend from "./trend";

export const dynamic = "force-dynamic";
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

type Comparison = {
  comparable: boolean | null;
  previous_start: string | null;
  previous_end: string | null;
  previous_rides: number | null;
  relative_change: number | null;
  reason: string | null;
};

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
  let gbfs: AnalyticsData | null = null;
  let peaks: AnalyticsData | null = null;
  let monthly: AnalyticsData | null = null;
  let hourly: AnalyticsData | null = null;
  let geography: AnalyticsData | null = null;
  let compare: (AnalyticsData & { comparison?: Comparison }) | null = null;
  let flags = new Map<string, string | null>();
  let routeError = "";
  let gbfsError = "";
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
      const [routeResult, gbfsResult, geoResult, compareResult, flagResult] =
        await Promise.allSettled([
          analytics("routes", routeParams),
          analytics("availability", new URLSearchParams({ publication_id: pub.publication_id })),
          analytics("geography", geoParams),
          baseline ? analytics("compare", compareParams) : Promise.reject(new Error("")),
          flagRequest,
        ]);
      if (routeResult.status === "fulfilled") routes = routeResult.value;
      else
        routeError = `Маршруты: ${routeResult.reason instanceof Error ? routeResult.reason.message : "Данные недоступны."} Выберите весь опубликованный период, ≤31 дня либо station_id / станцию назначения.`;
      if (gbfsResult.status === "fulfilled") gbfs = gbfsResult.value;
      else gbfsError = "GBFS API недоступен; исторические поездки показаны отдельно.";
      if (geoResult.status === "fulfilled") geography = geoResult.value;
      else geoError = "География доступна для ≤31 дня или выбранной станции.";
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
  const table = (rows: AnalyticsRow[], route: boolean) => (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>{route ? "Маршрут" : "Станция"}</th>
            <th>Поездки</th>
            <th>
              <Term name="departures">Отправления</Term>
            </th>
            <th>
              <Term name="arrivals">Прибытия</Term>
            </th>
            {!route && (
              <th>
                <Term name="net_trip_flow">Net flow</Term>
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const pair = route ? null : partner(r.station_id);
            return (
              <tr key={r.key}>
                <td>
                  <Link
                    href={href(route ? r.from_station : r.station_id, route ? r.to_station : null)}
                  >
                    {r.key}
                  </Link>
                  {!route && r.station_id && (
                    <>
                      {" "}
                      <Link
                        className="drill"
                        href={withParams("/stations", params, { station_id: r.station_id })}
                      >
                        станция ↗
                      </Link>
                    </>
                  )}
                  {pair && (
                    <span className="badge warn" data-id-split={r.station_id}>
                      возможный ID-split с {pair}
                    </span>
                  )}
                </td>
                <td>{show(r.metrics?.completed_published_rides)}</td>
                <td>{route ? "—" : show(r.metrics?.departures)}</td>
                <td>{route ? "—" : show(r.metrics?.arrivals)}</td>
                {!route && <td>{show(r.metrics?.net_flow)}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
  const profile = localHourProfile(
    (hourly?.rows ?? []).map((r) => ({
      key: r.key,
      value: r.metrics?.completed_published_rides ?? null,
    })),
  );
  const comparison = compare?.comparison;
  return (
    <main className="analytics-page product-page">
      <SiteHeader current="/analytics" params={params} />
      {pub?.data_mode === "controlled_fixture" && (
        <p role="status">Тестовые синтетические данные · не фактические показатели Citi Bike.</p>
      )}
      <section className="intro compact">
        <p className="eyebrow">NYC · ФАКТЫ ПОЕЗДОК · DEEP ANALYTICS</p>
        <h1>История в деталях.</h1>
        <p className="lead">
          Опубликованные завершённые поездки. Отправления по времени начала, прибытия — по времени
          окончания. Это не скрытый спрос и не наличие велосипедов.
        </p>
      </section>
      <section className="panel">
        <h2>Фильтры</h2>
        <form
          key={JSON.stringify(input)}
          action="/analytics"
          method="get"
          className="analytics-filters"
        >
          {pub && <input type="hidden" name="publication_id" value={pub.publication_id} />}
          <label>
            Начало, NY
            <input
              type="date"
              name="start_date"
              defaultValue={params.get("start_date") ?? kpi?.filters.start_date ?? ""}
              required
            />
          </label>
          <label>
            Конец, не включён
            <input
              type="date"
              name="end_date"
              defaultValue={params.get("end_date") ?? kpi?.filters.end_date ?? ""}
              required
            />
          </label>
          <label>
            Станция отправления
            <input
              name="station_id"
              maxLength={80}
              defaultValue={params.get("station_id") ?? ""}
              placeholder="Все станции"
            />
          </label>
          <label>
            Маршрут → станция
            <input
              name="to_station"
              maxLength={80}
              defaultValue={routeParams.get("to_station") ?? ""}
              placeholder="Только таблица маршрутов"
            />
          </label>
          <label>
            Велосипед
            <select name="vehicle" defaultValue={params.get("vehicle") ?? ""}>
              <option value="">Все</option>
              <option>classic_bike</option>
              <option>electric_bike</option>
              <option>unknown</option>
            </select>
          </label>
          <label>
            Пользователь
            <select name="membership" defaultValue={params.get("membership") ?? ""}>
              <option value="">Все</option>
              <option>member</option>
              <option>casual</option>
              <option>unknown</option>
            </select>
          </label>
          <label>
            Интервал
            <select name="grain" defaultValue={params.get("grain") ?? "day"}>
              <option value="day">День NY</option>
              <option value="month">Месяц NY</option>
              <option value="hour">Час UTC, ≤31 дня</option>
            </select>
          </label>
          <label>
            Сравнить с, начало
            <input type="date" name="compare_start" defaultValue={compareStart ?? ""} />
          </label>
          <label>
            Сравнить с, конец
            <input type="date" name="compare_end" defaultValue={compareEnd ?? ""} />
          </label>
          <label>
            География
            <select name="event_type" defaultValue={eventType}>
              <option value="start">Начала поездок</option>
              <option value="end">Окончания поездок</option>
            </select>
          </label>
          <button type="submit">Применить</button>
          <Link href="/analytics">Активная публикация</Link>
        </form>
      </section>
      {error && (
        <section className="panel" role="alert" data-analytics-state="error">
          <h2>Не удалось загрузить аналитику</h2>
          <p>{error}</p>
        </section>
      )}
      {!error && (!pub || kpi?.status === "empty") && (
        <section className="panel" data-analytics-state="empty">
          <h2>Нет опубликованных наблюдений</h2>
          <p>
            {kpi?.message ?? "Для этих фильтров наблюдения отсутствуют. N/A не заменяется нулём."}
          </p>
        </section>
      )}
      {!error && pub && kpi?.metrics && (
        <>
          <section
            className="panels analytics-kpis"
            data-analytics-state="ok"
            data-publication-id={pub.publication_id}
          >
            <article className="panel">
              <Kind kind="fact" />
              <h2>
                <Term name="completed_published_rides">Допустимые поездки</Term>
              </h2>
              <strong
                className="kpi"
                data-analytics-completed-rides={kpi.metrics.completed_published_rides}
              >
                {show(kpi.metrics.completed_published_rides)}
              </strong>
            </article>
            <article className="panel">
              <Kind kind="fact" />
              <h2>
                <Term name="average_duration">Средняя длительность</Term>
              </h2>
              <strong className="kpi">{show(kpi.metrics.average_duration_seconds)} с</strong>
              <p>Сумма длительностей / число поездок с допустимой длительностью.</p>
            </article>
            <article className="panel">
              <Kind kind="fact" />
              <h2>Отправления / прибытия</h2>
              <strong>
                {show(kpi.metrics.departures)} / {show(kpi.metrics.arrivals)}
              </strong>
              <p>
                Только известные station IDs. <Term name="net_trip_flow">Net flow</Term>:{" "}
                {show(kpi.metrics.net_flow)}. Не изменение inventory.
              </p>
            </article>
          </section>
          <section className="panel" data-compare-state={comparison?.comparable ? "ok" : "na"}>
            <Kind kind="fact" />
            <h2>
              <Term name="period_change">Равное сравнение периодов</Term>
            </h2>
            {comparison ? (
              <p>
                {kpi.filters.start_date} → {kpi.filters.end_date}:{" "}
                {show(kpi.metrics.completed_published_rides)} против {comparison.previous_start} →{" "}
                {comparison.previous_end} ({baseline?.rule}): {show(comparison.previous_rides)}.
                Изменение:{" "}
                <b data-compare-change={comparison.relative_change ?? "NA"}>
                  {percent(comparison.relative_change)}
                </b>
                . {comparison.reason ?? "Одинаковые NY-дни, часы UTC, набор weekday и покрытие."}
              </p>
            ) : (
              <p>{compareError}</p>
            )}
          </section>
          <section className="panel">
            <Kind kind="fact" />
            <h2>Динамика поездок</h2>
            <p>
              Часы подписаны в UTC, дни и месяцы — America/New_York. Пропуски не заполнены нулями.
              Нажмите на день в таблице для почасового drill-down.
            </p>
            <AnalyticsTrend
              rows={(series?.rows ?? []).map((r) => ({
                key: r.key,
                value: r.metrics?.completed_published_rides ?? null,
              }))}
            />
            <details>
              <summary>Значения временного ряда</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Период</th>
                      <th>Поездки</th>
                      <th>Отправления</th>
                      <th>Прибытия</th>
                    </tr>
                  </thead>
                  <tbody>
                    {series?.rows.map((r) => (
                      <tr key={r.key}>
                        <td>
                          {/^\d{4}-\d{2}-\d{2}$/.test(r.key) ? (
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
              </div>
            </details>
          </section>
          <section className="panels">
            <article className="panel" data-peaks={peaks?.rows.length ?? 0}>
              <Kind kind="fact" />
              <h2>Пики отправлений и прибытий</h2>
              <p>
                Отдельно по каждой стороне, одинаковые интервалы (
                {peaks?.coverage.peak_grain === "hour" ? "час UTC" : "NY-день"}
                ); среди равных — самый ранний.
              </p>
              <ul>
                {peaks?.rows.map((r) => (
                  <li key={`${r.dimension}-${r.key}`}>
                    {r.dimension === "departures" ? "Отправления" : "Прибытия"}: {show(r.n)} ·{" "}
                    {r.key}
                  </li>
                ))}
              </ul>
            </article>
            <article className="panel">
              <Kind kind="fact" />
              <h2>Сезонность по месяцам</h2>
              <p>Весь опубликованный период, NY-месяцы, те же фильтры станции/категорий.</p>
              <Chart
                label="Поездки по NY-месяцам за весь опубликованный период"
                spec={{
                  kind: "bar",
                  unit: "поездок",
                  categories: (monthly?.rows ?? []).map((r) => r.key.slice(0, 7)),
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
            </article>
          </section>
          <section className="panel">
            <Kind kind="fact" />
            <h2>Профиль: день недели × локальный час NY</h2>
            <p>
              Сумма опубликованных начал за {hourWindow?.start} → {hourWindow?.end} (≤28 дней, конец
              выбранного периода). Только наблюдённые часы; в ночь смены времени повторный час 01:00
              учитывает два UTC-часа, пропущенный 02:00 отсутствует.
            </p>
            <Chart
              label="Тепловая карта поездок по дню недели и локальному часу"
              spec={{
                kind: "heatmap",
                unit: "поездок",
                x: Array.from({ length: 24 }, (_, h) => String(h)),
                y: WEEKDAYS,
                cells: profile.map((c) => [c.hour, c.weekday, c.value]),
              }}
            />
            <details>
              <summary>Значения профиля</summary>
              <div className="table-scroll">
                <table>
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
              </div>
            </details>
          </section>
          <section className="panels">
            <article className="panel">
              <Kind kind="fact" />
              <h2>Станции</h2>
              <p>Первые 20 по числу начал поездок; ID — фильтр, «станция» — карточка.</p>
              {table(stations?.rows ?? [], false)}
            </article>
            <article
              className="panel"
              data-route-state={routeError ? "error" : (routes?.status ?? "unavailable")}
            >
              <Kind kind="fact" />
              <h2>
                <Term name="route_volume">Маршруты</Term>
              </h2>
              {routeError && <p role="status">{routeError}</p>}
              <p>
                Обе станции известны. Покрытие до фильтра назначения:{" "}
                {show(kpi.metrics.route_coverage == null ? null : kpi.metrics.route_coverage * 100)}
                %. Показано {routes ? `${routes.rows.length} из ${routes.total_groups} пар` : "N/A"}
                .
              </p>
              {routes && table(routes.rows, true)}
            </article>
          </section>
          <section className="panels">
            <article className="panel">
              <Kind kind="fact" />
              <h2>
                <Term name="trip_category_share">Категории</Term>
              </h2>
              <p>
                Доли пересчитаны по известным значениям выбранной категории; unknown показан
                отдельно.
              </p>
              <ul>
                {categories?.rows.map((r) => (
                  <li key={r.key}>
                    {r.key}: {show(r.n)} · доля известных{" "}
                    {r.known_share == null ? "N/A" : `${show(r.known_share * 100)}%`} · unknown{" "}
                    {show(r.unknown_count)}
                  </li>
                ))}
              </ul>
            </article>
            <article className="panel" data-geo-state={geography?.status ?? "unavailable"}>
              <Kind kind="fact" />
              <h2>Гео-агрегаты (сетка 0.01°)</h2>
              {geoError ? (
                <p role="status">{geoError}</p>
              ) : (
                <>
                  <p>
                    {eventType === "start" ? "Начала" : "Окончания"} по координатам самих событий,
                    не районы. Покрытие {percent(geography?.coverage.eligible_coverage as number)}.
                    показано {geography?.rows.length ?? 0} из {geography?.total_groups ?? "N/A"}{" "}
                    ячеек.
                  </p>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Ячейка lat/lon (юго-запад)</th>
                          <th>События</th>
                        </tr>
                      </thead>
                      <tbody>
                        {geography?.rows.slice(0, 30).map((r) => (
                          <tr key={r.key}>
                            <td>
                              {r.cell_lat == null
                                ? r.key
                                : `${(r.cell_lat / 100).toFixed(2)}, ${((r.cell_lon ?? 0) / 100).toFixed(2)}`}
                            </td>
                            <td>{show(r.n)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </article>
          </section>
          <section className="panel provenance">
            <h2>Происхождение, покрытие и качество</h2>
            <p>
              {pub.source} · {pub.scope} · {pub.timezone}
            </p>
            <p>
              {kpi.filters.start_date} → {kpi.filters.end_date}, [start,end). Population:{" "}
              {pub.population}
            </p>
            <p>
              Актуальность Silver: {pub.data_as_of}. Качество: {pub.quality_status}. Версия:{" "}
              {pub.pipeline_version}
            </p>
            <p>
              Публикация: <code>{pub.publication_id}</code>
              <br />
              F2: <code>{pub.source_snapshot_id}</code>
            </p>
            <p>
              Часы без подтверждённых начал поездок:{" "}
              {String(kpi.coverage.unobserved_city_start_hours ?? "N/A")}. Исторические районы не
              присвоены; география по координатам самих событий. Подозрительные ID{" "}
              <code>dddd.d</code>/<code>dddd.d0</code> помечены, Silver не объединялся.
            </p>
          </section>
        </>
      )}
      <section className="panel" data-gbfs-state={gbfs?.status ?? "unavailable"}>
        <Kind kind="realtime" />
        <h2>Наблюдённая доступность сети</h2>
        <p>
          {gbfsError ||
            gbfs?.message ||
            (gbfs?.status === "stale"
              ? "Наблюдённое окно покрыто, текущий источник устарел; это исторические GBFS интервалы."
              : gbfs?.status === "ok"
                ? "Метрики рассчитаны по валидным наблюдённым интервалам."
                : "Наблюдений недостаточно. История поездок не определяет текущую доступность.")}
        </p>
        {gbfs && (
          <>
            <p>
              Окно: {String(gbfs.availability.start ?? "N/A")} →{" "}
              {String(gbfs.availability.end ?? "N/A")}
            </p>
            <p>
              <Term name="observed_coverage">Покрытие station-time</Term>:{" "}
              {typeof gbfs.availability.observed_coverage === "number"
                ? `${show(gbfs.availability.observed_coverage * 100)}%`
                : "N/A"}
              . Freshness: {String(gbfs.availability.freshness_status ?? "N/A")}. Empty/full time
              share: {String(gbfs.availability.empty_station_time_share ?? "N/A")} /{" "}
              {String(gbfs.availability.full_station_time_share ?? "N/A")}
            </p>
            <p>Это ограниченные собранные snapshots, а не доступность сети за 24 месяца.</p>
            {typeof gbfs.availability.scope === "string" && <p>Scope: {gbfs.availability.scope}</p>}
          </>
        )}
      </section>
    </main>
  );
}
