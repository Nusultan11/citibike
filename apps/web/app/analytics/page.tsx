import Link from "next/link";
import { type AnalyticsData, type AnalyticsRow, analytics } from "../../lib/analytics";
import AnalyticsTrend from "./trend";

export const dynamic = "force-dynamic";
const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const show = (n: number | null | undefined) => (n == null ? "N/A" : number.format(n));

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
  ]) {
    const value = input[key];
    if (Array.isArray(value)) error = "Каждый фильтр должен быть задан один раз.";
    else if (value) params.set(key, value);
  }
  const routeParams = new URLSearchParams(params);
  params.delete("to_station");
  let kpi: AnalyticsData | null = null;
  let series: AnalyticsData | null = null;
  let stations: AnalyticsData | null = null;
  let routes: AnalyticsData | null = null;
  let categories: AnalyticsData | null = null;
  let gbfs: AnalyticsData | null = null;
  let routeError = "";
  let gbfsError = "";
  try {
    if (error) throw new Error(error);
    kpi = await analytics("kpi", params);
    const pub = kpi.publication;
    if (pub) {
      params.set("publication_id", pub.publication_id);
      routeParams.set("publication_id", pub.publication_id);
      [series, stations, categories] = await Promise.all([
        analytics("timeseries", params),
        analytics("stations", params),
        analytics("categories", params),
      ]);
      const [routeResult, gbfsResult] = await Promise.allSettled([
        analytics("routes", routeParams),
        analytics("availability", new URLSearchParams({ publication_id: pub.publication_id })),
      ]);
      if (routeResult.status === "fulfilled") routes = routeResult.value;
      else
        routeError = `Маршруты: ${routeResult.reason instanceof Error ? routeResult.reason.message : "Данные недоступны."} Нужны ≤31 дня, целые календарные месяцы или station_id.`;
      if (gbfsResult.status === "fulfilled") gbfs = gbfsResult.value;
      else gbfsError = "GBFS API недоступен; исторические поездки показаны отдельно.";
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
  const table = (rows: AnalyticsRow[], route: boolean) => (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            <th>{route ? "Маршрут" : "Станция"}</th>
            <th>Поездки</th>
            <th>Отправления</th>
            <th>Прибытия</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>
                <Link
                  href={href(route ? r.from_station : r.station_id, route ? r.to_station : null)}
                >
                  {r.key}
                </Link>
              </td>
              <td>{show(r.metrics?.completed_published_rides)}</td>
              <td>{route ? "—" : show(r.metrics?.departures)}</td>
              <td>{route ? "—" : show(r.metrics?.arrivals)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
  return (
    <main className="analytics-page">
      <header>
        <Link className="brand" href="/">
          BikeFlow <span>Intelligence</span>
        </Link>
        <span className="phase">F3 · Историческая аналитика</span>
      </header>
      {pub?.data_mode === "controlled_fixture" && (
        <p role="status">Тестовые синтетические данные · не фактические показатели Citi Bike.</p>
      )}
      <section className="intro">
        <p className="eyebrow">NYC · ФАКТЫ ПОЕЗДОК</p>
        <h1>История в деталях.</h1>
        <p className="lead">
          Опубликованные завершённые поездки. Отправления по времени начала, прибытия — по времени
          окончания. Это не скрытый спрос и не наличие велосипедов.
        </p>
        <Link href="/">Публикация F1 ↗</Link>
      </section>
      <section className="panel">
        <h2>Фильтры</h2>
        <form action="/analytics" method="get" className="analytics-filters">
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
              <h2>Допустимые поездки</h2>
              <strong
                className="kpi"
                data-analytics-completed-rides={kpi.metrics.completed_published_rides}
              >
                {show(kpi.metrics.completed_published_rides)}
              </strong>
            </article>
            <article className="panel">
              <h2>Средняя длительность</h2>
              <strong className="kpi">{show(kpi.metrics.average_duration_seconds)} с</strong>
              <p>Сумма длительностей / число поездок с допустимой длительностью.</p>
            </article>
            <article className="panel">
              <h2>Отправления / прибытия</h2>
              <strong>
                {show(kpi.metrics.departures)} / {show(kpi.metrics.arrivals)}
              </strong>
              <p>
                Только известные station IDs. Net flow: {show(kpi.metrics.net_flow)}. Не изменение
                inventory.
              </p>
            </article>
          </section>
          <section className="panel">
            <h2>Динамика поездок</h2>
            <p>
              Часы подписаны в UTC, дни и месяцы — America/New_York. Пропуски не заполнены нулями.
            </p>
            <AnalyticsTrend rows={series?.rows ?? []} />
            <details>
              <summary>Значения временного ряда</summary>
              <div style={{ overflowX: "auto", maxHeight: 300 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Период</th>
                      <th>Поездки</th>
                    </tr>
                  </thead>
                  <tbody>
                    {series?.rows.map((r) => (
                      <tr key={r.key}>
                        <td>{r.key}</td>
                        <td>{show(r.metrics?.completed_published_rides)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
          <section className="panels">
            <article className="panel">
              <h2>Станции</h2>
              <p>Первые 20 по числу начал поездок; нажмите ID для фильтра.</p>
              {table(stations?.rows ?? [], false)}
            </article>
            <article
              className="panel"
              data-route-state={routeError ? "error" : (routes?.status ?? "unavailable")}
            >
              <h2>Маршруты</h2>
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
          <section className="panel">
            <h2>Категории</h2>
            <p>
              Доли пересчитаны по известным значениям выбранной категории; unknown показан отдельно.
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
          </section>
          <section className="panel provenance">
            <h2>Происхождение и качество</h2>
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
              присвоены; география по координатам самих событий.
            </p>
          </section>
        </>
      )}
      <section className="panel" data-gbfs-state={gbfs?.status ?? "unavailable"}>
        <p className="eyebrow">ОТДЕЛЬНЫЙ ИСТОЧНИК · GBFS</p>
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
              Покрытие station-time:{" "}
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
