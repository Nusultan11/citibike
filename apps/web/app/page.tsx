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
  readFilters,
  withParams,
} from "../lib/operations";
import Chart from "./chart";
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
  const [status, data, summary, risk, forecast] = await Promise.all([
    getApiStatus(),
    dataRequest,
    settle(error ? failed : overview(params)),
    settle(getRisk(new URLSearchParams())),
    settle(getForecasts(new URLSearchParams({ limit: "1" }))),
  ]);
  const healthy = status === "ok";
  const pub = "publication" in data ? data.publication : null;
  const f3 = summary.ok ? summary.value : null;
  const metrics = f3?.kpi.metrics ?? null;
  const electric = f3?.categories.rows.find((r) => r.key === "vehicle:electric_bike");
  const comparison = (f3?.compare as (AnalyticsData & { comparison?: Comparison }) | null)
    ?.comparison;
  const forecastStatus = forecast.ok
    ? forecastState(
        forecast.value.status,
        forecast.value.publication?.valid_until as string | undefined,
        new Date(),
      )
    : "unavailable";
  const riskStatus = risk.ok ? risk.value.status : "unavailable";
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
          title="Как устроен велопрокат Нью-Йорка?"
          lead="BikeFlow — независимый исследовательский проект на публичных данных Citi Bike. Мы проверяем историю поездок, изучаем закономерности, сравниваем прогнозы и моделируем перевозки. Это не сервис оператора."
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
              className="kpi-row overview-kpis"
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
                  <Term name="trip_category_share">Доля электровелосипедов</Term>
                  <Kind kind="fact" compact />
                </div>
                <strong className="kpi">{percent(electric?.known_share)}</strong>
                <p className="kpi-desc">
                  Среди поездок с известным типом велосипеда; unknown:{" "}
                  {show(electric?.unknown_count)}.
                </p>
              </article>
            </section>

            <EvidenceNote source="docs/evidence/f3-analytics.json" status="VERIFIED">
              История опубликованных поездок NYC · {range(f3.window.start, f3.window.end)} ·
              America/New_York, конец не включён · F3 {short(f3.kpi.publication?.publication_id)}.
              Независимая сверка источника зафиксирована 29.10.2026; фильтры выше задают текущий
              срез.
            </EvidenceNote>
            <div className="grid" style={{ marginTop: 16 }}>
              <section className="card span-12">
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
                  <TableScroll>
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
                  </TableScroll>
                </details>
              </section>
            </div>
          </>
        )}

        <section className="card" style={{ marginTop: 16 }} aria-labelledby="explore-title">
          <h2 id="explore-title">От данных к проверяемому решению</h2>
          <p className="body-text">
            Поездки показывают совершённые перемещения, не весь спрос. Прогноз — оценка; симуляция —
            проверка допущений, не доказательство экономии. Каждый исследовательский результат
            сопровождается источником и ограничением.
          </p>
          <nav className="explore-grid" aria-label="Что посмотреть дальше">
            <Link href="/analytics">
              <strong>1. Analytics</strong>
              <span>История, качество, пики и маршруты. Меняйте период и раскрывайте часы.</span>
            </Link>
            <Link href="/stations">
              <strong>2. Stations</strong>
              <span>
                Найдите станцию на карте; сравните отправления и прибытия без объединения ID.
              </span>
            </Link>
            <Link href="/forecasts" data-forecast-state={forecastStatus}>
              <strong>3. Forecasts</strong>
              <span>
                Сравнение моделей и причины выбора Seasonal Naive. Публикация:{" "}
                {FORECAST_LABEL[forecastStatus]}.
              </span>
            </Link>
            <Link href="/scenarios" data-risk-queue={riskStatus}>
              <strong>4. Scenarios</strong>
              <span>
                Когда перевозки не выигрывают у no-action. Публикация: {RISK_LABEL[riskStatus]}.
              </span>
            </Link>
          </nav>
        </section>

        <details className="disclosure card f1-slice" aria-label="Техническая проверка F1">
          <summary>Техническая проверка: первый сквозной срез F1</summary>
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
        </details>
      </main>
      <SiteFooter />
    </>
  );
}

type Comparison = { relative_change: number | null; reason: string | null };
