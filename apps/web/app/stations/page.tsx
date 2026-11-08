import Link from "next/link";
import { type AnalyticsData, analytics } from "../../lib/analytics";
import { FINDINGS, findingPopulation } from "../../lib/findings";
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
import {
  AppliedFilters,
  EvidenceNote,
  Icon,
  Kind,
  nyTime,
  PageHead,
  percent,
  SiteFooter,
  SiteHeader,
  StatePanel,
  Status,
  show,
  TableScroll,
  Term,
} from "../ui";

export const dynamic = "force-dynamic";

function settle<T>(promise: Promise<T>) {
  return promise.then(
    (value) => ({ ok: true as const, value }),
    (cause: unknown) => ({ ok: false as const, ...message(cause) }),
  );
}

type Row = StationData["rows"][number];

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
  const row: Row | null = detailRow
    ? {
        ...detailRow,
        suspected_id_split: listed?.suspected_id_split ?? false,
        split_partner: listed?.split_partner ?? null,
      }
    : null;
  const partnerRow = row?.split_partner
    ? (list?.rows.find((r) => r.station_id === row.split_partner) ?? null)
    : null;
  const gbfs = detail?.ok ? detail.value.gbfs : null;
  const base = withParams("/stations", params, { station_id: "" });
  const mapHref = `${base}${base.includes("?") ? "&" : "?"}station_id=`;
  const forecastStatus = forecast?.ok
    ? forecastState(
        forecast.value.status,
        forecast.value.publication?.valid_until as string | undefined,
        new Date(),
      )
    : "unavailable";
  const chipParams = new URLSearchParams(params);
  if (q) chipParams.set("q", q);
  return (
    <>
      <SiteHeader current="/stations" params={params} />
      <main id="content" className="product-page">
        <PageHead
          eyebrow={
            <>
              <Icon name="stations" size={13} /> Станции · NYC
            </>
          }
          title="Карта станций и потоки"
          lead={
            <>
              Координаты — модальная пара опубликованных координат поездок станции в последнем
              NY-месяце периода. Пары ID <code>dddd.d</code> / <code>dddd.d0</code> помечены ◇ и не
              объединяются.
            </>
          }
        >
          {list && (
            <span className="chip">
              <Icon name="clock" size={13} />
              Период{stationPeriod(list.filters.start_date, list.filters.end_date)}
            </span>
          )}
        </PageHead>

        <form className="filterbar" action="/stations" method="get" aria-label="Фильтры станций">
          {params.get("publication_id") && (
            <input type="hidden" name="publication_id" value={params.get("publication_id") ?? ""} />
          )}
          <div className="filter-row">
            <label className="field grow">
              Поиск по ID или названию
              <input
                name="q"
                type="search"
                maxLength={80}
                defaultValue={q}
                placeholder="например 5980.1 или Broadway"
              />
            </label>
            <label className="field">
              Станция
              <input
                name="station_id"
                maxLength={80}
                defaultValue={station ?? ""}
                placeholder="ID станции"
              />
            </label>
            <label className="field">
              С даты (NY)
              <input type="date" name="start_date" defaultValue={params.get("start_date") ?? ""} />
            </label>
            <label className="field">
              До даты, не включая
              <input type="date" name="end_date" defaultValue={params.get("end_date") ?? ""} />
            </label>
            <label className="field">
              Велосипед
              <select name="vehicle" defaultValue={params.get("vehicle") ?? ""}>
                <option value="">Все</option>
                <option value="classic_bike">Классический</option>
                <option value="electric_bike">Электро</option>
              </select>
            </label>
            <label className="field">
              Пользователь
              <select name="membership" defaultValue={params.get("membership") ?? ""}>
                <option value="">Все</option>
                <option value="member">Подписчик</option>
                <option value="casual">Разовый</option>
              </select>
            </label>
            <div className="filter-actions">
              <Link className="btn btn-ghost" href="/stations">
                Сбросить
              </Link>
              <button className="btn btn-primary" type="submit">
                Найти
              </button>
            </div>
          </div>
          <AppliedFilters
            path="/stations"
            params={chipParams}
            keys={{
              q: "Поиск",
              station_id: "Станция",
              vehicle: "Велосипед",
              membership: "Пользователь",
            }}
          />
        </form>

        {failure ? (
          <StatePanel
            state={failure.state}
            title={failure.state === "error" ? "Фильтры не подходят" : "Станции недоступны"}
          >
            <p data-stations-state={failure.state}>{failure.text}</p>
            <p className="actions">
              <Link className="btn btn-secondary btn-sm" href="/stations">
                Сбросить фильтры
              </Link>
            </p>
          </StatePanel>
        ) : (
          list && (
            <>
              <div className="grid">
                <section
                  className={`card ${station || q ? "span-8" : "span-12"}`}
                  data-stations-state={list.status}
                  data-stations-total={list.total_stations}
                  data-geo-id={list.geo?.geo_id}
                >
                  <div className="card-head">
                    <div>
                      <h2>
                        Карта · <Term name="net_trip_flow">net trip flow</Term>
                      </h2>
                      <p className="card-sub">
                        {list.status === "empty" && !list.publication
                          ? "Публикация F3 для выбранной области отсутствует — данные карты недоступны."
                          : `${show(mapped.length)} станций на карте · ${show(missing.length)} без координат (не показаны) · ${show(Number(list.coverage.suspected_id_split_stations ?? 0))} с пометкой ID-split`}
                      </p>
                    </div>
                    <Kind kind="fact" />
                  </div>
                  {list.geo && mapped.length > 0 ? (
                    <>
                      <div className="chart-frame">
                        <Chart
                          label="Карта станций по координатам с net trip flow; нажмите точку для карточки"
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
                      </div>
                      <div className="legend">
                        <span className="legend-item gradient-legend">
                          <span>отправлений больше</span>
                          <i
                            style={{
                              background: "linear-gradient(90deg,#f2706b,#7a8294,#5b9cf0)",
                            }}
                          />
                          <span>прибытий больше</span>
                        </span>
                        <span className="legend-item">
                          <span className="swatch diamond" style={{ borderColor: "#e5b84a" }} />
                          возможный ID-split
                        </span>
                        <span className="legend-item">
                          <span className="swatch ring" /> выбранная станция
                        </span>
                        <span className="hint">
                          <Icon name="search" size={12} /> Ctrl + колесо — масштаб, перетаскивание —
                          сдвиг
                        </span>
                      </div>
                    </>
                  ) : (
                    <div className="empty-state" role="status">
                      <Icon name="stations" size={22} />
                      <strong>Координаты не опубликованы</strong>
                      <span>
                        {list.message ?? "Карта недоступна; метрики станций ниже — без точек."}
                      </span>
                    </div>
                  )}
                  <p className="note" style={{ marginTop: 10 }}>
                    Координатная карта без уличной подложки. Выберите точку мышью или ID в
                    раскрываемом списке под картой. Координаты исторические; источник —
                    опубликованные поездки.
                  </p>
                  {list.publication && (
                    <EvidenceNote source="docs/evidence/f6-product.json" status="VERIFIED">
                      Проверка координат 02.11.2026 · F3{" "}
                      {list.publication.publication_id.slice(0, 12)} · NYC,{" "}
                      {String(list.filters.start_date)} → {String(list.filters.end_date)} · source
                      ID без объединения.
                    </EvidenceNote>
                  )}
                </section>

                {(station || q) && (
                  <aside className="span-4 stack" aria-label="Станция или результаты поиска">
                    {station ? (
                      <StationSummary
                        station={station}
                        row={row}
                        detail={detail?.ok ? detail.value : null}
                        detailError={detail && !detail.ok ? detail.text : null}
                        params={params}
                      />
                    ) : (
                      <section className="card">
                        <div className="card-head">
                          <div>
                            <h2>{q ? `Найдено по «${q}»` : "Выберите станцию"}</h2>
                            <p className="card-sub">
                              {q
                                ? `Совпадений: ${show(matches.length)}; показаны первые 40 по числу поездок`
                                : "Нажмите точку карты, выберите ID под графиком с клавиатуры или найдите часть названия."}
                            </p>
                          </div>
                        </div>
                        {!q ? null : matches.length === 0 ? (
                          <div className="empty-state">
                            <strong>Ничего не найдено</strong>
                            <span>Проверьте ID или часть названия.</span>
                          </div>
                        ) : (
                          <ol className="list-clean side-list">
                            {matches.slice(0, 40).map((r) => (
                              <li key={r.station_id}>
                                <Link
                                  className="row-link"
                                  href={`${mapHref}${encodeURIComponent(r.station_id)}`}
                                >
                                  <span style={{ minWidth: 0 }}>
                                    <span className="mono">{r.station_id}</span>
                                    {r.suspected_id_split && (
                                      <span className="badge warn">◇ {r.split_partner}</span>
                                    )}
                                    <span className="cell-sub">
                                      {r.station_name ?? "название N/A"}
                                    </span>
                                  </span>
                                  <span style={{ textAlign: "right" }}>
                                    <b>{show(r.completed_published_rides)}</b>
                                    <span className="cell-sub">net {show(r.net_flow)}</span>
                                  </span>
                                </Link>
                              </li>
                            ))}
                          </ol>
                        )}
                      </section>
                    )}
                  </aside>
                )}
              </div>

              {station && row && (
                <StationSections
                  station={station}
                  row={row}
                  partner={partnerRow}
                  detail={detail?.ok ? detail.value : null}
                  gbfs={gbfs}
                  series={series?.ok ? series.value : null}
                  routes={routes?.ok ? routes.value : null}
                  inbound={inbound?.ok ? inbound.value : null}
                  risk={risk?.ok ? risk.value : null}
                  forecastState={forecastStatus}
                  params={params}
                />
              )}
            </>
          )
        )}
        <section className="card" style={{ marginTop: 16 }} data-finding="da-id-split">
          <h2>Почему похожие ID показаны раздельно</h2>
          <p className="body-text">{ID_SPLIT.takeaway}</p>
          <p className="note">{ID_SPLIT.limitation}</p>
          <EvidenceNote source={ID_SPLIT.source}>
            {ID_SPLIT.recorded} · {findingPopulation(ID_SPLIT)}
          </EvidenceNote>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

function StationSummary({
  station,
  row,
  detail,
  detailError,
  params,
}: {
  station: string;
  row: Row | null;
  detail: StationData | null;
  detailError: string | null;
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
        <p className="actions">
          <Link
            className="btn btn-secondary btn-sm"
            href={withParams("/stations", params, { station_id: "" })}
          >
            Вернуться к списку
          </Link>
        </p>
      </StatePanel>
    );
  return (
    <section className="card" data-station-detail={row.station_id}>
      <div className="card-head">
        <Kind kind="fact" />
        <Link
          className="btn btn-ghost btn-sm"
          href={withParams("/stations", params, { station_id: "" })}
          aria-label="Закрыть карточку станции"
        >
          ✕ Закрыть
        </Link>
      </div>
      <div className="station-title">
        <h2>{row.station_name ?? "Название N/A"}</h2>
      </div>
      <p className="station-id">{row.station_id}</p>
      {row.suspected_id_split && (
        <p
          className="callout tone-warning"
          style={{ marginTop: 12, display: "block" }}
          data-id-split-detail={row.split_partner}
        >
          <b>◇ Возможный ID-split</b> с {row.split_partner}: то же название и точка, формат
          raw-источника. Показано без объединения; тождество оператором не подтверждено.
        </p>
      )}
      <div className="stat-pair" style={{ marginTop: 14 }}>
        <div className="stat">
          <span>
            <Term name="departures">Отправления</Term>
          </span>
          <strong data-station-departures={row.departures ?? "NA"}>{show(row.departures)}</strong>
        </div>
        <div className="stat">
          <span>
            <Term name="arrivals">Прибытия</Term>
          </span>
          <strong data-station-arrivals={row.arrivals ?? "NA"}>{show(row.arrivals)}</strong>
        </div>
        <div className="stat">
          <span>
            <Term name="net_trip_flow">Net flow</Term>
          </span>
          <strong>{show(row.net_flow)}</strong>
        </div>
      </div>
      <dl className="facts" style={{ marginTop: 14 }}>
        <dt>Координаты</dt>
        <dd className="mono">
          {row.coordinate_status === "ok"
            ? `${row.lat}, ${row.lon}`
            : "отсутствуют — точка не показана на карте"}
        </dd>
        {row.coordinate_status === "ok" && (
          <>
            <dt>NY-месяц координат</dt>
            <dd>
              {row.coordinate_month}; изменений в периоде: {row.coordinate_changes}
            </dd>
          </>
        )}
        <dt>Период</dt>
        <dd>
          {String(detail?.filters.start_date)} → {String(detail?.filters.end_date)}, конец не
          включён
        </dd>
      </dl>
      <div className="card-foot">
        <Link
          className="link-arrow"
          href={withParams("/analytics", params, { station_id: row.station_id })}
        >
          Аналитика станции <Icon name="arrow" size={14} />
        </Link>
        {row.split_partner && (
          <Link
            className="link-arrow"
            href={withParams("/stations", params, { station_id: row.split_partner })}
          >
            Открыть пару <Icon name="arrow" size={14} />
          </Link>
        )}
      </div>
    </section>
  );
}

function StationSections({
  station,
  row,
  partner,
  detail,
  gbfs,
  series,
  routes,
  inbound,
  risk,
  forecastState: forecast,
  params,
}: {
  station: string;
  row: Row;
  partner: Row | null;
  detail: StationData | null;
  gbfs: Record<string, unknown> | null;
  series: AnalyticsData | null;
  routes: AnalyticsData | null;
  inbound: AnalyticsData | null;
  risk: Awaited<ReturnType<typeof getRisk>> | null;
  forecastState: string;
  params: URLSearchParams;
}) {
  const gbfsStatus = String(gbfs?.status ?? "unavailable");
  return (
    <section className="station-detail" aria-label={`Детали станции ${row.station_id}`}>
      {row.suspected_id_split && (
        <section className="card" style={{ marginTop: 16 }}>
          <div className="card-head">
            <div>
              <h2>Сравнение пары ID-split</h2>
              <p className="card-sub">
                Значения показаны рядом, не суммируются: объединение ID требует отдельного решения
                владельца.
              </p>
            </div>
            <Status tone="warning">не объединено</Status>
          </div>
          <TableScroll free>
            <table className="compare-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Название</th>
                  <th className="num">Отправления</th>
                  <th className="num">Прибытия</th>
                  <th className="num">Net flow</th>
                  <th>Координаты</th>
                </tr>
              </thead>
              <tbody>
                {[row, partner].map((r, i) =>
                  r ? (
                    <tr key={r.station_id} aria-current={i === 0 ? "true" : undefined}>
                      <td className="mono">{r.station_id}</td>
                      <td>{r.station_name ?? "N/A"}</td>
                      <td className="num">{show(r.departures)}</td>
                      <td className="num">{show(r.arrivals)}</td>
                      <td className="num">{show(r.net_flow)}</td>
                      <td className="mono">
                        {r.coordinate_status === "ok" ? `${r.lat}, ${r.lon}` : "отсутствуют"}
                      </td>
                    </tr>
                  ) : (
                    <tr key="missing-partner">
                      <td className="mono">{row.split_partner}</td>
                      <td colSpan={5} className="muted">
                        Нет строки пары в выбранном периоде
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </TableScroll>
        </section>
      )}

      <div className="grid" style={{ marginTop: 16 }}>
        {!!series?.rows.length && (
          <section className="card span-8">
            <div className="card-head">
              <div>
                <h2>Отправления и прибытия по дням</h2>
                <p className="card-sub">NY-дни выбранного периода; пропуски не заполнены нулями.</p>
              </div>
              <Kind kind="fact" />
            </div>
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
        )}
        <div className="span-4 stack">
          <section className="card" data-gbfs-station={gbfsStatus}>
            <div className="card-head">
              <h2>
                GBFS · <Term name="status_freshness_sec">свежесть</Term>
              </h2>
              <Kind kind="realtime" />
            </div>
            {!gbfs || (gbfsStatus !== "fresh" && gbfsStatus !== "ok") ? (
              <>
                <Status tone="neutral">недоступно</Status>
                <p className="body-text" style={{ marginTop: 8 }}>
                  {gbfsStatus === "stale"
                    ? "Наблюдение устарело; текущие велосипеды и доки неизвестны."
                    : "Нет годного GBFS-наблюдения для станции; текущая доступность неизвестна."}
                </p>
              </>
            ) : (
              <>
                <Status tone="success">свежее · в пределах TTL</Status>
                <dl className="facts" style={{ marginTop: 10 }}>
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
          </section>
          <section className="card" data-station-scenario={risk?.status ?? "unavailable"}>
            <h2>Проверить оценку для этой станции</h2>
            <p className="note">
              Текущий прогноз:{" "}
              {forecast === "valid"
                ? "действителен"
                : forecast === "expired"
                  ? "истёк"
                  : "недоступен"}
              . Сценарный результат и его as-of — на отдельной странице.
            </p>
            <div className="card-foot">
              <Link
                className="link-arrow"
                href={withParams("/scenarios", new URLSearchParams(), { station_id: station })}
              >
                Сценарии <Icon name="arrow" size={14} />
              </Link>
              <Link
                className="link-arrow"
                href={withParams("/forecasts", new URLSearchParams(), { station_id: station })}
              >
                Прогнозы <Icon name="arrow" size={14} />
              </Link>
            </div>
          </section>
        </div>

        {(
          [
            ["Маршруты отсюда", routes, "to_station"],
            ["Маршруты сюда", inbound, "from_station"],
          ] as const
        ).map(([title, value, other]) => {
          const max = Math.max(1, ...(value?.rows ?? []).map((r) => r.n ?? 0));
          if (!value?.rows.length) return null;
          return (
            <section className="card span-6" key={title}>
              <div className="card-head">
                <div>
                  <h2>
                    <Term name="route_volume">{title}</Term>
                  </h2>
                  <p className="card-sub">
                    {value
                      ? `Покрытие ${percent(value.coverage.eligible_coverage as number | null)} · показано ${value.rows.length} из ${show(value.total_groups)} пар`
                      : "Маршруты недоступны для этого периода."}
                  </p>
                </div>
                <Kind kind="fact" />
              </div>
              {value && value.rows.length > 0 && (
                <ol className="list-clean">
                  {value.rows.map((r, i) => {
                    const target = other === "to_station" ? r.to_station : r.from_station;
                    return (
                      <li key={r.key}>
                        <Link
                          className="row-link"
                          href={withParams("/stations", params, { station_id: target ?? "" })}
                        >
                          <span>
                            <span className="rank">{i + 1}</span>
                            <span className="mono">{target}</span>
                          </span>
                          <span className="cell-bar">
                            <i style={{ width: `${((r.n ?? 0) / max) * 90}px` }} />
                            <b>{show(r.n)}</b>
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ol>
              )}
            </section>
          );
        })}

        {!!detail?.history.length && (
          <details className="disclosure card span-12">
            <summary>История координат и названий</summary>
            <div className="card-head">
              <div>
                <h2>История координат и названий</h2>
                <p className="card-sub">
                  По NY-месяцам и сторонам поездки; координаты исторические, не текущие GBFS.
                </p>
              </div>
            </div>
            <TableScroll>
              <table>
                <thead>
                  <tr>
                    <th>NY-месяц</th>
                    <th>Сторона</th>
                    <th>lat, lon</th>
                    <th>Название</th>
                    <th className="num">Событий / с координатами / модальных</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail?.history ?? []).map((h) => (
                    <tr key={`${String(h.local_month)}-${String(h.event_type)}`}>
                      <td>{String(h.local_month)}</td>
                      <td>{h.event_type === "start" ? "начала" : "окончания"}</td>
                      <td className="mono">
                        {h.lat == null ? "отсутствуют" : `${String(h.lat)}, ${String(h.lon)}`}
                      </td>
                      <td>{String(h.station_name ?? "N/A")}</td>
                      <td className="num">
                        {show(h.events as number)} / {show(h.coordinate_rows as number)} /{" "}
                        {show(h.modal_rows as number)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>
          </details>
        )}
      </div>
    </section>
  );
}

const ID_SPLIT = (() => {
  const finding = FINDINGS.find((f) => f.id === "da-id-split");
  if (!finding) throw new Error("Missing station identity evidence");
  return finding;
})();
