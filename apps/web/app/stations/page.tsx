import Link from "next/link";
import { type AnalyticsData, analytics } from "../../lib/analytics";
import {
  defaultWindow,
  forecastState,
  getForecasts,
  getRisk,
  getStations,
  message,
  readFilters,
  type StationData,
  stationPeriod,
  withParams,
} from "../../lib/operations";
import Chart from "../chart";
import { Kind, nyTime, percent, SiteHeader, StatePanel, show, Term } from "../ui";

export const dynamic = "force-dynamic";

function settle<T>(promise: Promise<T>) {
  return promise.then(
    (value) => ({ ok: true as const, value }),
    (cause: unknown) => ({ ok: false as const, ...message(cause) }),
  );
}

export default async function StationsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const { params, error } = readFilters(input);
  const q = typeof input.q === "string" ? input.q.slice(0, 80) : "";
  const station = params.get("station_id");
  let list: StationData | null = null;
  let failure: { state: "unavailable" | "error"; text: string } | null = error
    ? { state: "error", text: error }
    : null;
  if (!failure) {
    try {
      // Default map scope: the last 28 NY days of the active publication.
      if (!params.get("start_date")) {
        const pub = (await analytics("publication", new URLSearchParams(params))).publication;
        if (pub) {
          const window = defaultWindow(pub.period_end);
          params.set("start_date", window.start);
          params.set("end_date", window.end);
        }
      }
      const scope = new URLSearchParams(params);
      scope.delete("station_id");
      list = await getStations(scope);
      if (list.publication) params.set("publication_id", list.publication.publication_id);
    } catch (cause) {
      failure = message(cause);
    }
  }
  const detailParams = new URLSearchParams(params);
  detailParams.delete("station_id");
  const daily = new URLSearchParams(params);
  daily.set("grain", "day");
  const [detail, series, routes, inbound, risk, forecast] =
    station && list
      ? await Promise.all([
          settle(getStations(detailParams, station)),
          settle(analytics("timeseries", daily)),
          settle(
            analytics(
              "routes",
              new URLSearchParams({ ...Object.fromEntries(params), limit: "10" }),
            ),
          ),
          settle(
            analytics(
              "routes",
              new URLSearchParams({
                ...Object.fromEntries(detailParams),
                to_station: station,
                limit: "10",
              }),
            ),
          ),
          settle(getRisk(new URLSearchParams({ station_id: station }))),
          settle(getForecasts(new URLSearchParams({ station_id: station, limit: "48" }))),
        ])
      : [null, null, null, null, null, null];
  const needle = q.toLowerCase();
  const matches = (list?.rows ?? [])
    .filter(
      (r) =>
        !needle ||
        r.station_id.toLowerCase().startsWith(needle) ||
        (r.station_name ?? "").toLowerCase().includes(needle),
    )
    .sort((a, b) => (b.completed_published_rides ?? 0) - (a.completed_published_rides ?? 0));
  const mapped = (list?.rows ?? []).filter((r) => r.lat !== null && r.lon !== null);
  const missing = (list?.rows ?? []).filter((r) => r.coordinate_status !== "ok");
  // Split flags need the whole period's ID universe, which only the list carries.
  const listed = list?.rows.find((r) => r.station_id === station);
  const detailRow = detail?.ok ? detail.value.rows[0] : null;
  const row = detailRow
    ? {
        ...detailRow,
        suspected_id_split: listed?.suspected_id_split ?? false,
        split_partner: listed?.split_partner ?? null,
      }
    : null;
  const gbfs = detail?.ok ? detail.value.gbfs : null;
  const base = withParams("/stations", params, { station_id: "" });
  const mapHref = `${base}${base.includes("?") ? "&" : "?"}station_id=`;
  return (
    <main className="product-page">
      <SiteHeader current="/stations" params={params} />
      <section className="intro compact">
        <p className="eyebrow">STATION EXPLORER · NYC</p>
        <h1>Станции на карте.</h1>
        <p className="lead">
          Координаты — модальная пара опубликованных координат поездок станции в последнем NY-месяце
          периода. Подозрительные ID <code>dddd.d</code>/<code>dddd.d0</code> показаны с пометкой и
          не объединяются.
        </p>
      </section>
      <form className="filters panel" action="/stations" method="get" aria-label="Фильтры станций">
        {params.get("publication_id") && (
          <input type="hidden" name="publication_id" value={params.get("publication_id") ?? ""} />
        )}
        <label>
          С даты (NY)
          <input type="date" name="start_date" defaultValue={params.get("start_date") ?? ""} />
        </label>
        <label>
          До даты, не включая
          <input type="date" name="end_date" defaultValue={params.get("end_date") ?? ""} />
        </label>
        <label>
          Велосипед
          <select name="vehicle" defaultValue={params.get("vehicle") ?? ""}>
            <option value="">Все</option>
            <option>classic_bike</option>
            <option>electric_bike</option>
          </select>
        </label>
        <label>
          Пользователь
          <select name="membership" defaultValue={params.get("membership") ?? ""}>
            <option value="">Все</option>
            <option>member</option>
            <option>casual</option>
          </select>
        </label>
        <label>
          Поиск station_id / названия
          <input name="q" maxLength={80} defaultValue={q} placeholder="например 5980.1" />
        </label>
        <label>
          Станция
          <input name="station_id" maxLength={80} defaultValue={station ?? ""} />
        </label>
        <button className="refresh" type="submit">
          Применить
        </button>
        <Link href="/stations">Сбросить</Link>
      </form>
      {failure ? (
        <StatePanel state={failure.state} title="Станции недоступны">
          <p data-stations-state={failure.state}>{failure.text}</p>
        </StatePanel>
      ) : (
        list && (
          <>
            <section
              className="panel"
              data-stations-state={list.status}
              data-stations-total={list.total_stations}
              data-geo-id={list.geo?.geo_id}
            >
              <Kind kind="fact" />
              <h2>
                Карта · <Term name="net_trip_flow">net trip flow</Term>
                {stationPeriod(list.filters.start_date, list.filters.end_date)}
              </h2>
              {list.status === "empty" && !list.publication ? (
                <p role="status">
                  Публикация F3 для выбранной области отсутствует. Данные карты недоступны.
                </p>
              ) : (
                <p>
                  {show(mapped.length)} станций с координатами, {show(missing.length)} без координат
                  (не на карте), {show(Number(list.coverage.suspected_id_split_stations ?? 0))} с
                  пометкой ID-split (ромб). Цвет: синий — прибытий больше, красный — отправлений
                  больше. Нажмите на точку для карточки станции. Базовая карта улиц не подключена:
                  нет утверждённого провайдера тайлов.
                </p>
              )}
              {list.geo ? (
                <Chart
                  label="Карта станций по координатам с net trip flow"
                  spec={{
                    kind: "map",
                    href: mapHref,
                    selected: station,
                    points: mapped.map((r) => ({
                      id: r.station_id,
                      name: r.station_name,
                      lat: r.lat as number,
                      lon: r.lon as number,
                      value: r.net_flow,
                      flagged: r.suspected_id_split,
                    })),
                  }}
                />
              ) : (
                <p role="status">{list.message ?? "Координаты не опубликованы."}</p>
              )}
              <details>
                <summary>
                  Найдено {show(matches.length)} станций{q ? ` по «${q}»` : ""} — таблица
                </summary>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Станция</th>
                        <th>Название</th>
                        <th>Отправления</th>
                        <th>Прибытия</th>
                        <th>Net flow</th>
                        <th>Координаты</th>
                      </tr>
                    </thead>
                    <tbody>
                      {matches.slice(0, 200).map((r) => (
                        <tr key={r.station_id} data-station-row={r.station_id}>
                          <td>
                            <Link href={`${mapHref}${encodeURIComponent(r.station_id)}`}>
                              {r.station_id}
                            </Link>
                            {r.suspected_id_split && (
                              <span className="badge warn" data-id-split={r.station_id}>
                                ID-split? пара {r.split_partner}
                              </span>
                            )}
                          </td>
                          <td>{r.station_name ?? "N/A"}</td>
                          <td>{show(r.departures)}</td>
                          <td>{show(r.arrivals)}</td>
                          <td>{show(r.net_flow)}</td>
                          <td>
                            {r.coordinate_status === "ok"
                              ? `${r.lat?.toFixed(5)}, ${r.lon?.toFixed(5)} (${r.coordinate_month})`
                              : "отсутствуют"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <p className="note">
                Источник координат: {list.geo?.version ?? "N/A"} · {list.geo?.policy ?? ""}
              </p>
            </section>
            {station && (
              <StationDetail
                station={station}
                row={row}
                detail={detail?.ok ? detail.value : null}
                detailError={detail && !detail.ok ? detail.text : null}
                gbfs={gbfs}
                series={series?.ok ? series.value : null}
                routes={routes?.ok ? routes.value : null}
                inbound={inbound?.ok ? inbound.value : null}
                risk={risk?.ok ? risk.value : null}
                forecastState={
                  forecast?.ok
                    ? forecastState(
                        forecast.value.status,
                        forecast.value.publication?.valid_until as string | undefined,
                        new Date(),
                      )
                    : "unavailable"
                }
                params={params}
              />
            )}
          </>
        )
      )}
    </main>
  );
}

function StationDetail({
  station,
  row,
  detail,
  detailError,
  gbfs,
  series,
  routes,
  inbound,
  risk,
  forecastState: forecast,
  params,
}: {
  station: string;
  row: StationData["rows"][number] | null;
  detail: StationData | null;
  detailError: string | null;
  gbfs: Record<string, unknown> | null;
  series: AnalyticsData | null;
  routes: AnalyticsData | null;
  inbound: AnalyticsData | null;
  risk: Awaited<ReturnType<typeof getRisk>> | null;
  forecastState: string;
  params: URLSearchParams;
}) {
  if (detailError)
    return (
      <StatePanel state="error" title={`Станция ${station}`}>
        <p data-station-detail="error">{detailError}</p>
      </StatePanel>
    );
  if (!row)
    return (
      <StatePanel state="empty" title={`Станция ${station}`}>
        <p data-station-detail="empty">
          {detail?.message ?? "Нет наблюдений станции в выбранном периоде. Это не нулевой спрос."}
        </p>
      </StatePanel>
    );
  const riskStation = risk?.stations[0];
  return (
    <section className="station-detail" data-station-detail={row.station_id}>
      <section className="panel">
        <Kind kind="fact" />
        <h2>
          {row.station_id} · {row.station_name ?? "название N/A"}
        </h2>
        {row.suspected_id_split && (
          <p className="warning-text" data-id-split-detail={row.split_partner}>
            Возможный ID-split: та же точка и название вероятны у {row.split_partner} (формат
            raw-источника). Показано без объединения; тождество оператором не подтверждено.{" "}
            <Link href={withParams("/stations", params, { station_id: row.split_partner ?? "" })}>
              Открыть пару ↗
            </Link>
          </p>
        )}
        <dl>
          <dt>
            <Term name="departures">Отправления</Term>
          </dt>
          <dd data-station-departures={row.departures ?? "NA"}>{show(row.departures)}</dd>
          <dt>
            <Term name="arrivals">Прибытия</Term>
          </dt>
          <dd data-station-arrivals={row.arrivals ?? "NA"}>{show(row.arrivals)}</dd>
          <dt>
            <Term name="net_trip_flow">Net trip flow</Term>
          </dt>
          <dd>{show(row.net_flow)}</dd>
          <dt>Координаты</dt>
          <dd>
            {row.coordinate_status === "ok"
              ? `${row.lat}, ${row.lon} (NY-месяц ${row.coordinate_month}; изменений в периоде: ${row.coordinate_changes})`
              : "отсутствуют — точка не показана на карте"}
          </dd>
          <dt>Период</dt>
          <dd>
            {String(detail?.filters.start_date)} → {String(detail?.filters.end_date)}, NY, конец не
            включён
          </dd>
        </dl>
        <Link href={withParams("/analytics", params, { station_id: row.station_id })}>
          Глубокая аналитика станции ↗
        </Link>
      </section>
      <section className="panels">
        <article className="panel" data-gbfs-station={String(gbfs?.status ?? "unavailable")}>
          <Kind kind="realtime" />
          <h2>
            GBFS · <Term name="status_freshness_sec">свежесть</Term>
          </h2>
          {gbfs?.status === "unavailable" || !gbfs ? (
            <p>{String(gbfs?.reason ?? "Статус недоступен.")}</p>
          ) : (
            <>
              <p className={gbfs.status === "stale" ? "warning-text" : ""}>
                {gbfs.status === "stale"
                  ? "Устаревшее наблюдение — не текущая доступность."
                  : "Свежее наблюдение в пределах TTL."}
              </p>
              <dl>
                <dt>Велосипеды / доки</dt>
                <dd>
                  {show(gbfs.rentable_bikes as number | null)} /{" "}
                  {show(gbfs.returnable_docks as number | null)}
                </dd>
                <dt>Source updated</dt>
                <dd>{nyTime(gbfs.source_updated)}</dd>
                <dt>Возраст</dt>
                <dd>{show(Number(gbfs.status_freshness_sec) / 3600)} ч</dd>
              </dl>
            </>
          )}
        </article>
        <article className="panel" data-station-scenario={risk?.status ?? "unavailable"}>
          <Kind kind="simulation" />
          <h2>Сценарии F5 и прогноз F4</h2>
          <p>
            {!risk || risk.status === "empty"
              ? "Станция вне когорты опубликованного сценария F5."
              : riskStation?.eligible
                ? `В сценарии (архивный replay): capacity ${riskStation.capacity}, bikes ${riskStation.bikes}, docks ${riskStation.docks} на as-of.`
                : `Исключена из сценария: ${riskStation?.reasons.join(", ")}.`}{" "}
            <Link href={withParams("/scenarios", new URLSearchParams(), { station_id: station })}>
              Сценарии ↗
            </Link>
          </p>
          <p>
            Прогноз:{" "}
            {forecast === "valid"
              ? "действителен"
              : forecast === "expired"
                ? "истёк — значения не показываются"
                : "недоступен для станции"}
            .{" "}
            <Link href={withParams("/forecasts", new URLSearchParams(), { station_id: station })}>
              Прогнозы ↗
            </Link>
          </p>
        </article>
      </section>
      <section className="panel">
        <Kind kind="fact" />
        <h2>Отправления и прибытия по дням</h2>
        <Chart
          label="Отправления и прибытия станции по NY-дням"
          spec={{
            kind: "line",
            unit: "поездок",
            categories: (series?.rows ?? []).map((r) => r.key),
            series: [
              {
                name: "Отправления",
                values: (series?.rows ?? []).map((r) => r.metrics?.departures ?? null),
              },
              {
                name: "Прибытия",
                values: (series?.rows ?? []).map((r) => r.metrics?.arrivals ?? null),
              },
            ],
          }}
        />
      </section>
      <section className="panels">
        {[
          ["Маршруты отсюда", routes, "to_station"],
          ["Маршруты сюда", inbound, "from_station"],
        ].map(([title, data, other]) => {
          const value = data as AnalyticsData | null;
          return (
            <article className="panel" key={String(title)}>
              <Kind kind="fact" />
              <h2>
                <Term name="route_volume">{String(title)}</Term>
              </h2>
              {!value ? (
                <p>Маршруты недоступны для этого периода.</p>
              ) : (
                <>
                  <p>
                    Покрытие {percent(value.coverage.eligible_coverage as number | null)}; показано{" "}
                    {value.rows.length} из {show(value.total_groups)} пар.
                  </p>
                  <ol>
                    {value.rows.map((r) => {
                      const target = other === "to_station" ? r.to_station : r.from_station;
                      return (
                        <li key={r.key}>
                          <Link
                            href={withParams("/stations", params, { station_id: target ?? "" })}
                          >
                            {target}
                          </Link>{" "}
                          — {show(r.n)}
                        </li>
                      );
                    })}
                  </ol>
                </>
              )}
            </article>
          );
        })}
      </section>
      <section className="panel">
        <h2>История координат и названий</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>NY-месяц</th>
                <th>Сторона</th>
                <th>lat, lon</th>
                <th>Название</th>
                <th>Событий / с координатами / модальных</th>
              </tr>
            </thead>
            <tbody>
              {(detail?.history ?? []).map((h) => (
                <tr key={`${String(h.local_month)}-${String(h.event_type)}`}>
                  <td>{String(h.local_month)}</td>
                  <td>{h.event_type === "start" ? "начала" : "окончания"}</td>
                  <td>{h.lat == null ? "отсутствуют" : `${String(h.lat)}, ${String(h.lon)}`}</td>
                  <td>{String(h.station_name ?? "N/A")}</td>
                  <td>
                    {show(h.events as number)} / {show(h.coordinate_rows as number)} /{" "}
                    {show(h.modal_rows as number)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}
