import Link from "next/link";
import { type AnalyticsData, analytics } from "../lib/analytics";
import { type DashboardData, getDashboard } from "../lib/data";
import { getApiStatus } from "../lib/health";
import {
  addDays,
  comparisonWindow,
  defaultWindow,
  forecastState,
  getForecasts,
  getRisk,
  message,
  nyDate,
  type RiskData,
  readFilters,
  withParams,
} from "../lib/operations";
import Chart, { SERIES } from "./chart";
import Trend from "./trend";
import {
  AppliedFilters,
  Delta,
  Icon,
  Kind,
  nyTime,
  PageHead,
  percent,
  RiskBar,
  range,
  SiteFooter,
  SiteHeader,
  Sparkline,
  Status,
  short,
  show,
  Term,
  type Tone,
} from "./ui";

export const dynamic = "force-dynamic";
const numbers = new Intl.NumberFormat("ru-RU");
const dates = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "America/New_York",
  dateStyle: "medium",
  timeStyle: "short",
});
const formatDate = (value: string) => dates.format(new Date(value));

type Overview = {
  kpi: AnalyticsData;
  compare: AnalyticsData | null;
  current: AnalyticsData;
  baseline: AnalyticsData | null;
  categories: AnalyticsData;
  window: { start: string; end: string };
  baseWindow: { start: string; end: string; rule: string } | null;
  lastDay: string;
};

async function overview(params: URLSearchParams): Promise<Overview | null> {
  const publication = await analytics("publication", new URLSearchParams(params));
  const pub = publication.publication;
  if (!pub) return null;
  const window =
    params.get("start_date") && params.get("end_date")
      ? { start: params.get("start_date") as string, end: params.get("end_date") as string }
      : defaultWindow(pub.period_end);
  const scoped = new URLSearchParams(params);
  scoped.set("publication_id", pub.publication_id);
  scoped.set("start_date", window.start);
  scoped.set("end_date", window.end);
  const baseWindow = comparisonWindow(window.start, window.end, nyDate(pub.period_start));
  const daily = new URLSearchParams(scoped);
  daily.set("grain", "day");
  const previous = baseWindow ? new URLSearchParams(daily) : null;
  previous?.set("start_date", baseWindow?.start ?? "");
  previous?.set("end_date", baseWindow?.end ?? "");
  const comparing = new URLSearchParams(scoped);
  if (baseWindow) {
    comparing.set("compare_start", baseWindow.start);
    comparing.set("compare_end", baseWindow.end);
  }
  const [kpi, compare, current, baseline, categories] = await Promise.all([
    analytics("kpi", scoped),
    baseWindow ? analytics("compare", comparing) : Promise.resolve(null),
    analytics("timeseries", daily),
    previous ? analytics("timeseries", previous) : Promise.resolve(null),
    analytics("categories", scoped),
  ]);
  return {
    kpi,
    compare,
    current,
    baseline,
    categories,
    window,
    baseWindow,
    lastDay: nyDate(pub.period_end),
  };
}

function settle<T>(promise: Promise<T>) {
  return promise.then(
    (value) => ({ ok: true as const, value }),
    (cause: unknown) => ({ ok: false as const, ...message(cause) }),
  );
}

const FORECAST_TONE: Record<string, Tone> = {
  valid: "success",
  expired: "warning",
  empty: "neutral",
  unavailable: "warning",
};
const RISK_TONE: Record<string, Tone> = {
  current_scenario: "success",
  archival_replay: "warning",
  empty: "neutral",
  unavailable: "warning",
};
const RISK_LABEL: Record<string, string> = {
  current_scenario: "текущий сценарий",
  archival_replay: "архивный replay",
  empty: "не опубликован",
  unavailable: "недоступен",
};
const FORECAST_LABEL: Record<string, string> = {
  valid: "действителен",
  expired: "истёк",
  empty: "не опубликован",
  unavailable: "недоступен",
};

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const { params, error } = readFilters(input);
  const start = params.get("start_date") ?? undefined;
  const end = params.get("end_date") ?? undefined;
  const invalid = Array.isArray(input.start_date) || Array.isArray(input.end_date);
  const dataRequest: Promise<DashboardData> = invalid
    ? Promise.resolve({ state: "error", message: "Каждая дата должна быть задана один раз." })
    : getDashboard(start, end);
  const failed = Promise.reject(new Error(error));
  failed.catch(() => undefined);
  const [status, data, summary, risk, forecast, gbfs] = await Promise.all([
    getApiStatus(),
    dataRequest,
    settle(error ? failed : overview(params)),
    settle(getRisk(new URLSearchParams())),
    settle(getForecasts(new URLSearchParams({ limit: "1" }))),
    settle(analytics("availability", new URLSearchParams())),
  ]);
  const healthy = status === "ok";
  const pub = "publication" in data ? data.publication : null;
  const f3 = summary.ok ? summary.value : null;
  const metrics = f3?.kpi.metrics ?? null;
  const electric = f3?.categories.rows.find((r) => r.key === "vehicle:electric_bike");
  const member = f3?.categories.rows.find((r) => r.key === "membership:member");
  const comparison = (f3?.compare as (AnalyticsData & { comparison?: Comparison }) | null)
    ?.comparison;
  const forecastStatus = forecast.ok
    ? forecastState(
        forecast.value.status,
        forecast.value.publication?.valid_until as string | undefined,
        new Date(),
      )
    : "unavailable";
  const queue = risk.ok ? riskQueue(risk.value) : [];
  const riskStatus = risk.ok ? risk.value.status : "unavailable";
  const availability = gbfs.ok ? gbfs.value.availability : null;
  const gbfsFreshness = availability ? String(availability.freshness_status ?? "N/A") : "N/A";
  const currentValues = f3?.current.rows.map((r) => r.metrics?.completed_published_rides ?? null);
  const best = (f3?.current.rows ?? []).reduce<{ key: string; n: number } | null>((acc, r) => {
    const n = r.metrics?.completed_published_rides;
    return n != null && (!acc || n > acc.n) ? { key: r.key, n } : acc;
  }, null);
  const presets = f3
    ? [7, 28, 91].map((days) => ({
        days,
        href: withParams("/", params, {
          start_date: addDays(f3.lastDay, -days),
          end_date: f3.lastDay,
        }),
        active: f3.window.end === f3.lastDay && f3.window.start === addDays(f3.lastDay, -days),
      }))
    : [];

  return (
    <>
      <SiteHeader current="/" params={params} />
      <main id="content" className="product-page">
        <PageHead
          eyebrow={
            <>
              <Icon name="overview" size={13} /> Обзор · NYC
            </>
          }
          title="Сеть и спрос — в одном экране"
          lead="Опубликованные поездки, наблюдения GBFS, прогноз и сценарии — с источником, периодом и свежестью у каждого числа."
        >
          {f3 && (
            <span className="chip">
              <Icon name="clock" size={13} /> {range(f3.window.start, f3.window.end)}
            </span>
          )}
          {f3?.kpi.publication && (
            <span className="chip">
              <Icon name="database" size={13} /> F3{" "}
              <span className="mono">{short(f3.kpi.publication.publication_id)}</span>
            </span>
          )}
        </PageHead>

        <form className="filterbar static" action="/" method="get" aria-label="Период обзора">
          <div className="filter-row">
            <label className="field">
              С даты (NY)
              <input type="date" name="start_date" defaultValue={f3?.window.start ?? start} />
            </label>
            <label className="field">
              До даты, не включая
              <input type="date" name="end_date" defaultValue={f3?.window.end ?? end} />
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
            {presets.length > 0 && (
              <div className="field">
                Быстрый период
                <nav className="segmented" aria-label="Быстрый выбор периода">
                  {presets.map((p) => (
                    <Link key={p.days} href={p.href} aria-current={p.active ? "true" : undefined}>
                      {p.days} дн.
                    </Link>
                  ))}
                </nav>
              </div>
            )}
            <div className="filter-actions">
              <Link className="btn btn-ghost" href="/">
                Сбросить
              </Link>
              <button className="btn btn-primary" type="submit">
                Применить
              </button>
            </div>
          </div>
          <AppliedFilters
            path="/"
            params={params}
            keys={{ vehicle: "Велосипед", membership: "Пользователь" }}
          />
        </form>

        {!summary.ok ? (
          <section className="callout tone-danger" role="alert" data-overview-state={summary.state}>
            <span className="callout-icon">
              <Icon name={summary.state === "unavailable" ? "off" : "alert"} size={17} />
            </span>
            <div>
              <h2>
                {summary.state === "unavailable"
                  ? "Аналитика сейчас недоступна"
                  : "Фильтры не подходят"}
              </h2>
              <p>{summary.text}</p>
              <p className="actions">
                <Link className="btn btn-secondary btn-sm" href="/">
                  Сбросить фильтры
                </Link>
              </p>
            </div>
          </section>
        ) : !f3 ? (
          <section className="callout tone-neutral" data-overview-state="empty">
            <span className="callout-icon">
              <Icon name="database" size={17} />
            </span>
            <div>
              <h2>Аналитика ещё не опубликована</h2>
              <p>Нет активной F3 публикации — показатели не заменяются нулями.</p>
            </div>
          </section>
        ) : (
          <>
            <section
              className="kpi-row"
              aria-label="Основные показатели"
              data-overview-state={metrics ? "ok" : "empty"}
              data-overview-publication={f3.kpi.publication?.publication_id}
            >
              <article className="card kpi-card hero">
                <div className="kpi-label">
                  <Term name="completed_published_rides">Поездки за период</Term>
                  <Kind kind="fact" compact />
                </div>
                <strong className="kpi" data-overview-rides={metrics?.completed_published_rides}>
                  {show(metrics?.completed_published_rides)}
                </strong>
                <div className="kpi-split">
                  <span data-overview-change={comparison?.relative_change ?? "NA"}>
                    <Delta value={comparison?.relative_change} />
                  </span>
                  <span className="kpi-desc">
                    к{" "}
                    {f3.baseWindow
                      ? `${range(f3.baseWindow.start, f3.baseWindow.end)} (${f3.baseWindow.rule})`
                      : "— нет сопоставимого периода"}
                  </span>
                </div>
                {comparison?.reason && <p className="kpi-desc">{comparison.reason}</p>}
                {currentValues && (
                  <Sparkline values={currentValues} label="Поездки по дням текущего периода" />
                )}
              </article>
              <article className="card kpi-card">
                <div className="kpi-label">
                  <Term name="average_duration">Средняя длительность</Term>
                  <Kind kind="fact" compact />
                </div>
                <strong className="kpi">
                  {metrics?.average_duration_seconds == null ? (
                    "N/A"
                  ) : (
                    <>
                      {show(metrics.average_duration_seconds / 60)}
                      <span className="kpi-unit">мин</span>
                    </>
                  )}
                </strong>
                <p className="kpi-desc">
                  Знаменатель — поездки с допустимой длительностью в том же периоде.
                </p>
              </article>
              <article className="card kpi-card">
                <div className="kpi-label">
                  <Term name="trip_category_share">Электро · подписчики</Term>
                  <Kind kind="fact" compact />
                </div>
                <div className="kpi-split">
                  <strong className="kpi">{percent(electric?.known_share)}</strong>
                  <span className="sep">/</span>
                  <strong className="kpi">{percent(member?.known_share)}</strong>
                </div>
                <div className="stack" style={{ gap: 8 }}>
                  <ShareBar label="Электро" share={electric?.known_share} color={SERIES[0]} />
                  <ShareBar label="Подписчики" share={member?.known_share} color={SERIES[2]} />
                </div>
                <p className="kpi-desc">
                  Доля среди известных категорий; unknown: {show(electric?.unknown_count)} /{" "}
                  {show(member?.unknown_count)}.
                </p>
              </article>
              <article
                className="card kpi-card card-dashed"
                data-network-state={gbfs.ok ? gbfs.value.status : "unavailable"}
              >
                <div className="kpi-label">
                  <Term name="active_stations">Работающие станции</Term>
                  <Kind kind="realtime" compact />
                </div>
                <strong className="kpi">
                  {show(availability ? (availability.active_stations as number | null) : null)}
                </strong>
                <div className="kpi-split">
                  <Status tone={freshTone(gbfsFreshness)}>GBFS {gbfsFreshness}</Status>
                  {availability && (
                    <span className="kpi-desc">
                      покрытие {percent(availability.observed_coverage as number | null, 3)}
                    </span>
                  )}
                </div>
                <p className="kpi-desc">
                  {gbfs.ok
                    ? (gbfs.value.message ??
                      "Отдельно от истории поездок: наблюдение состояния, не факт спроса.")
                    : gbfs.text}
                </p>
              </article>
            </section>

            <div className="grid" style={{ marginTop: 16 }}>
              <section className="card span-8">
                <div className="card-head">
                  <div>
                    <h2>Поездки по дням: текущий vs базовый период</h2>
                    <p className="card-sub">
                      День N сопоставлен дню N, одинаковое число NY-дней. Пропуски не равны нулю.
                      Нажмите на день — откроется почасовая аналитика.
                    </p>
                  </div>
                  <Kind kind="fact" />
                </div>
                <Chart
                  label="Поездки по NY-дням текущего и базового периода"
                  spec={{
                    kind: "line",
                    unit: "поездок",
                    peak: true,
                    categories: f3.current.rows.map((r) => r.key),
                    drill: { base: withParams("/analytics", params), to: "hour" },
                    series: [
                      {
                        name: "Текущий период",
                        values: currentValues ?? [],
                      },
                      ...(f3.baseline
                        ? [
                            {
                              name: `Базовый (${f3.baseWindow?.rule})`,
                              values: f3.baseline.rows.map(
                                (r) => r.metrics?.completed_published_rides ?? null,
                              ),
                            },
                          ]
                        : []),
                    ],
                  }}
                />
                <div className="card-foot">
                  <span>
                    {best ? (
                      <>
                        Пиковый день периода: <b>{best.key}</b> — {show(best.n)} поездок
                      </>
                    ) : (
                      "Нет наблюдённых дней"
                    )}
                  </span>
                  <Link
                    className="link-arrow"
                    href={withParams("/analytics", params, {
                      start_date: f3.window.start,
                      end_date: f3.window.end,
                    })}
                  >
                    Глубокая аналитика периода <Icon name="arrow" size={14} />
                  </Link>
                </div>
                <details className="disclosure">
                  <summary>Таблица значений по дням</summary>
                  <div className="table-scroll">
                    <table className="num-cols">
                      <thead>
                        <tr>
                          <th>День</th>
                          <th>Текущий</th>
                          <th>Базовый день</th>
                          <th>Базовый</th>
                        </tr>
                      </thead>
                      <tbody>
                        {f3.current.rows.map((r, i) => (
                          <tr key={r.key}>
                            <td>
                              <Link
                                href={withParams("/analytics", params, {
                                  start_date: r.key,
                                  end_date: f3.current.rows[i + 1]?.key ?? f3.window.end,
                                  grain: "hour",
                                })}
                              >
                                {r.key}
                              </Link>
                            </td>
                            <td>{show(r.metrics?.completed_published_rides)}</td>
                            <td>{f3.baseline?.rows[i]?.key ?? "—"}</td>
                            <td>
                              {show(f3.baseline?.rows[i]?.metrics?.completed_published_rides)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </section>

              <div className="span-4 stack">
                <RiskQueue
                  risk={risk.ok ? risk.value : null}
                  text={risk.ok ? "" : risk.text}
                  queue={queue}
                />
                <section className="card" data-forecast-state={forecastStatus}>
                  <div className="card-head">
                    <h2>
                      <Term name="forecast">Прогноз F4</Term>
                    </h2>
                    <Kind kind="forecast" />
                  </div>
                  <Status tone={FORECAST_TONE[forecastStatus]}>
                    {FORECAST_LABEL[forecastStatus]}
                  </Status>
                  <p className="body-text" style={{ marginTop: 10 }}>
                    {forecastStatus === "valid"
                      ? `Действует до ${nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)}.`
                      : forecastStatus === "expired"
                        ? `Окно закрылось ${nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)} — значения не показываются как свежие.`
                        : forecastStatus === "empty"
                          ? "Прогноз ещё не опубликован."
                          : forecast.ok
                            ? "Прогноз недоступен."
                            : forecast.text}
                  </p>
                  <div className="card-foot">
                    <span>Модель: Seasonal Naive</span>
                    <Link className="link-arrow" href="/forecasts">
                      Версия и provenance <Icon name="arrow" size={14} />
                    </Link>
                  </div>
                </section>
              </div>
            </div>
          </>
        )}

        <div className="section-title">
          <h2>Источники и свежесть</h2>
          <p>Каждый слой обновляется отдельно — статус показан для каждого.</p>
        </div>
        <section className="source-grid" aria-label="Источники и свежесть">
          <SourceCell
            label="F3 Gold · поездки"
            tone={f3?.kpi.publication?.quality_status === "pass" ? "success" : "warning"}
            status={f3?.kpi.publication?.quality_status ?? "N/A"}
            version={short(f3?.kpi.publication?.publication_id)}
            when={`Silver as-of ${nyTime(f3?.kpi.publication?.data_as_of)}`}
          />
          <SourceCell
            label="GBFS · наблюдения"
            tone={gbfs.ok ? freshTone(gbfsFreshness) : "warning"}
            status={gbfs.ok ? gbfsFreshness : "unavailable"}
            version={String(availability?.availability_version ?? "N/A")}
            when={`source ${nyTime(availability?.last_captured_status_updated)}`}
          />
          <SourceCell
            label="F4 · прогноз"
            tone={FORECAST_TONE[forecastStatus]}
            status={forecastStatus}
            version={short(forecast.ok ? forecast.value.publication?.publication_id : null)}
            when={`valid_until ${nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)}`}
          />
          <SourceCell
            label="F5 · сценарии"
            tone={RISK_TONE[riskStatus]}
            status={riskStatus}
            version={short(risk.ok ? risk.value.publication?.publication_id : null)}
            when={`as-of ${nyTime(risk.ok ? risk.value.publication?.data_as_of : null)}`}
          />
        </section>

        <section className="f1-slice" aria-label="Первый вертикальный срез F1">
          <div className="section-title">
            <h2>Первый вертикальный срез F1</h2>
            <p>
              Отдельная публикация одного архива (апрель 2026); даты вне окна дают ошибку, а не
              ноль.
            </p>
          </div>
          <div className="grid">
            <article className="card span-4" data-api-status={status}>
              <div className="card-head">
                <h3>Связь с платформой</h3>
              </div>
              <h2 style={{ fontSize: 18 }}>
                <span className={`dot ${healthy ? "online" : "offline"}`} />
                {healthy ? "API доступен" : "API недоступен"}
              </h2>
              <p className="body-text" style={{ marginTop: 8 }}>
                {healthy
                  ? "Сервис отвечает. Готовность бизнес-данных проверяется отдельно."
                  : "Не удалось связаться с API. Показатели не подменяются нулём."}
              </p>
              <form action="/" method="get" style={{ marginTop: 14 }}>
                <button className="btn btn-secondary btn-sm" type="submit">
                  Проверить снова
                </button>
              </form>
            </article>
            <article className="card span-8" data-data-state={data.state}>
              <div className="card-head">
                <h3>Опубликованные поездки F1 · NYC</h3>
                <Kind kind="fact" />
              </div>
              {data.state === "ok" ? (
                <>
                  <strong
                    className="kpi"
                    data-completed-rides={data.count}
                    data-publication-id={data.publication.publication_id}
                  >
                    {numbers.format(data.count)}
                  </strong>
                  <p className="kpi-desc">
                    {formatDate(data.filters.start)} → {formatDate(data.filters.end)} ·
                    America/New_York · конец интервала не включён.
                  </p>
                </>
              ) : (
                <>
                  <h2 style={{ fontSize: 18 }}>
                    {data.state === "empty"
                      ? "Нет опубликованных данных"
                      : data.state === "error"
                        ? "Ошибка запроса"
                        : "Данные недоступны"}
                  </h2>
                  <p className="body-text" style={{ marginTop: 6 }}>
                    {data.message}
                  </p>
                  {data.state === "error" && (
                    <p style={{ marginTop: 10 }}>
                      <Link className="btn btn-secondary btn-sm" href="/">
                        Сбросить фильтры
                      </Link>
                    </p>
                  )}
                </>
              )}
            </article>
            {data.state === "ok" && (
              <section className="card span-12">
                <div className="card-head">
                  <div>
                    <h3>Поездки по часу начала</h3>
                    <p className="card-sub">
                      Ключи часов — UTC, подписи — America/New_York. Пропуск наблюдения не равен
                      нулевому спросу.
                    </p>
                  </div>
                </div>
                <Trend points={data.points} filters={data.filters} />
              </section>
            )}
            {pub && (
              <section className="card span-12">
                <div className="card-head">
                  <h3>
                    {pub.quality_status === "pass"
                      ? "Проверенный архив"
                      : "Опубликовано с ограничениями качества"}
                  </h3>
                  <Status tone={pub.quality_status === "pass" ? "success" : "warning"}>
                    {pub.quality_status}
                  </Status>
                </div>
                <dl className="facts">
                  <dt>Источник</dt>
                  <dd>
                    {pub.source} · NYC · America/New_York ·{" "}
                    {pub.source_url.startsWith("https://") ? (
                      <a href={pub.source_url}>официальный архив ↗</a>
                    ) : (
                      "контролируемый тестовый источник, не реальные поездки"
                    )}
                  </dd>
                  <dt>Доступный период</dt>
                  <dd>
                    {formatDate(pub.period_start)} → {formatDate(pub.period_end)}
                  </dd>
                  <dt>Входных / канонических строк</dt>
                  <dd>
                    {numbers.format(pub.input_rows)} / {numbers.format(pub.canonical_rows)}
                  </dd>
                  <dt>Publication</dt>
                  <dd className="hash">{pub.publication_id}</dd>
                </dl>
              </section>
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

type Comparison = { relative_change: number | null; reason: string | null };
type Coverage = { eligible: number; requested: number };

function freshTone(value: string): Tone {
  if (value === "fresh" || value === "ok") return "success";
  if (value === "stale") return "warning";
  return "neutral";
}

function ShareBar({
  label,
  share,
  color,
}: {
  label: string;
  share: number | null | undefined;
  color: string;
}) {
  return (
    <div>
      <div className="share-legend">
        <span>{label}</span>
        <span>{percent(share)}</span>
      </div>
      <div className="share-bar" aria-hidden="true">
        {share != null && <span style={{ width: `${share * 100}%`, background: color }} />}
      </div>
    </div>
  );
}

function SourceCell({
  label,
  tone,
  status,
  version,
  when,
}: {
  label: string;
  tone: Tone;
  status: string;
  version: string;
  when: string;
}) {
  return (
    <div className="source-cell">
      <span className="label">{label}</span>
      <Status tone={tone}>{status}</Status>
      <span className="value mono">{version}</span>
      <span className="note">{when}</span>
    </div>
  );
}

function RiskQueue({
  risk,
  text,
  queue,
}: {
  risk: RiskData | null;
  text: string;
  queue: ReturnType<typeof riskQueue>;
}) {
  const coverage = risk?.publication?.coverage as Coverage | undefined;
  return (
    <section className="card" data-risk-queue={risk ? risk.status : "unavailable"}>
      <div className="card-head">
        <div>
          <h2>
            <Term name="empty_full_risk">Очередь сценарных рисков</Term>
          </h2>
          <p className="card-sub">No-action · масштаб 1 · uniform; Wilson 95%</p>
        </div>
        <Kind kind="simulation" />
      </div>
      {!risk ? (
        <p className="body-text">{text}</p>
      ) : risk.status === "empty" ? (
        <p className="body-text">Сценарии F5 не опубликованы.</p>
      ) : (
        <>
          <Status tone={RISK_TONE[risk.status]}>{RISK_LABEL[risk.status]}</Status>
          <p className="note" style={{ marginTop: 8 }}>
            {risk.status === "archival_replay"
              ? "Архивный recorded-as-of replay — не текущая операционная рекомендация."
              : "Сценарий в пределах TTL начального GBFS состояния."}{" "}
            As-of {nyTime(risk.publication?.data_as_of)}; покрытие {String(coverage?.eligible)}/
            {String(coverage?.requested)} станций.
          </p>
          <ol className="list-clean" style={{ marginTop: 10 }}>
            {queue.map((q) => (
              <li key={q.station_id}>
                <Link
                  className="row-link"
                  href={`/stations?station_id=${encodeURIComponent(q.station_id)}`}
                >
                  <span>
                    <span className="mono">{q.station_id}</span>
                    <span className="cell-sub">{q.label}</span>
                  </span>
                  <span style={{ display: "grid", gap: 4, justifyItems: "end" }}>
                    <b>{percent(q.fraction, 0)}</b>
                    <RiskBar value={q.fraction} low={q.low} high={q.high} />
                  </span>
                </Link>
              </li>
            ))}
          </ol>
          <div className="card-foot">
            <span>Wilson — точность Монте-Карло, не калибровка</span>
            <Link className="link-arrow" href="/scenarios">
              Все сценарии <Icon name="arrow" size={14} />
            </Link>
          </div>
        </>
      )}
    </section>
  );
}

function riskQueue(risk: RiskData) {
  return risk.station_outcomes
    .filter((o) => o.policy === "no_action" && o.status === "ok")
    .map((o) => {
      const full = (o.full_fraction ?? 0) >= (o.empty_fraction ?? 0);
      return {
        station_id: o.station_id,
        label: full ? "переполнение" : "опустошение",
        fraction: full ? o.full_fraction : o.empty_fraction,
        low: full ? o.full_low : o.empty_low,
        high: full ? o.full_high : o.empty_high,
      };
    })
    .sort((a, b) => (b.fraction ?? 0) - (a.fraction ?? 0))
    .slice(0, 5);
}
