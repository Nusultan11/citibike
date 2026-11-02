import Link from "next/link";
import { type AnalyticsData, analytics } from "../lib/analytics";
import { type DashboardData, getDashboard } from "../lib/data";
import { getApiStatus } from "../lib/health";
import {
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
import Chart from "./chart";
import Trend from "./trend";
import { Kind, nyTime, percent, SiteHeader, short, show, Term } from "./ui";

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
  return { kpi, compare, current, baseline, categories, window, baseWindow };
}

function settle<T>(promise: Promise<T>) {
  return promise.then(
    (value) => ({ ok: true as const, value }),
    (cause: unknown) => ({ ok: false as const, ...message(cause) }),
  );
}

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

  return (
    <main className="product-page">
      <SiteHeader current="/" params={params} />
      <section className="intro compact">
        <p className="eyebrow">ОБЗОР · NYC</p>
        <h1>Сеть, которую можно проверить.</h1>
        <p className="lead">
          Факты опубликованных поездок, отдельно — наблюдения GBFS, прогнозы и сценарные симуляции.
          Каждый блок несёт свой источник, период и свежесть.
        </p>
      </section>

      <form className="filters panel" action="/" method="get" aria-label="Период обзора">
        <label>
          С даты (Нью-Йорк)
          <input type="date" name="start_date" defaultValue={f3?.window.start ?? start} />
        </label>
        <label>
          До даты, не включая
          <input type="date" name="end_date" defaultValue={f3?.window.end ?? end} />
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
        <button className="refresh" type="submit">
          Применить
        </button>
        <Link href="/">Сбросить</Link>
      </form>

      {!summary.ok ? (
        <section className="panel state-error" role="alert" data-overview-state={summary.state}>
          <h2>{summary.state === "unavailable" ? "Аналитика недоступна" : "Ошибка фильтров"}</h2>
          <p>{summary.text}</p>
          <Link href="/">Сбросить фильтры</Link>
        </section>
      ) : !f3 ? (
        <section className="panel" data-overview-state="empty">
          <h2>Аналитика ещё не опубликована</h2>
          <p>Нет активной F3 публикации; показатели не заменяются нулями.</p>
        </section>
      ) : (
        <>
          <section
            className="kpi-grid"
            aria-label="Основные показатели"
            data-overview-state={metrics ? "ok" : "empty"}
            data-overview-publication={f3?.kpi.publication?.publication_id}
          >
            <article className="panel kpi-card">
              <Kind kind="fact" />
              <h2>
                <Term name="completed_published_rides">Допустимые поездки</Term>
              </h2>
              <strong className="kpi" data-overview-rides={metrics?.completed_published_rides}>
                {show(metrics?.completed_published_rides)}
              </strong>
              <p>
                <Term name="period_change">Изменение</Term>:{" "}
                <b data-overview-change={comparison?.relative_change ?? "NA"}>
                  {comparison?.relative_change == null
                    ? "N/A"
                    : `${comparison.relative_change > 0 ? "+" : ""}${percent(comparison.relative_change)}`}
                </b>{" "}
                к {f3?.baseWindow ? `${f3.baseWindow.start} → ${f3.baseWindow.end}` : "—"} (
                {f3?.baseWindow?.rule ?? "нет сопоставимого периода"})
                {comparison?.reason ? `. ${comparison.reason}` : ""}
              </p>
            </article>
            <article className="panel kpi-card">
              <Kind kind="fact" />
              <h2>
                <Term name="average_duration">Средняя длительность</Term>
              </h2>
              <strong className="kpi">
                {metrics?.average_duration_seconds == null
                  ? "N/A"
                  : `${show(metrics.average_duration_seconds / 60)} мин`}
              </strong>
              <p>Знаменатель: поездки с допустимой длительностью в том же периоде.</p>
            </article>
            <article className="panel kpi-card">
              <Kind kind="fact" />
              <h2>
                <Term name="trip_category_share">Доля e-bike / member</Term>
              </h2>
              <strong className="kpi">
                {percent(electric?.known_share)} / {percent(member?.known_share)}
              </strong>
              <p>
                Знаменатель — известные категории; unknown: {show(electric?.unknown_count)} /{" "}
                {show(member?.unknown_count)}.
              </p>
            </article>
            <article
              className="panel kpi-card realtime"
              data-network-state={gbfs.ok ? gbfs.value.status : "unavailable"}
            >
              <Kind kind="realtime" />
              <h2>
                <Term name="active_stations">Работающие станции</Term>
              </h2>
              <strong className="kpi">
                {show(gbfs.ok ? (gbfs.value.availability.active_stations as number | null) : null)}
              </strong>
              <p>
                {gbfs.ok
                  ? `GBFS ${String(gbfs.value.availability.freshness_status ?? "N/A")}: покрытие ${percent(gbfs.value.availability.observed_coverage as number | null, 3)}; ${gbfs.value.message ?? ""}`
                  : gbfs.text}
              </p>
            </article>
          </section>

          <section className="panel">
            <Kind kind="fact" />
            <h2>Сопоставимый тренд: поездки по дням</h2>
            <p>
              {f3?.window.start} → {f3?.window.end} против{" "}
              {f3?.baseWindow ? `${f3.baseWindow.start} → ${f3.baseWindow.end}` : "N/A"}: одинаковое
              число NY-дней, день N сопоставлен дню N. Пропуски не равны нулю.
            </p>
            {f3 && (
              <Chart
                label="Поездки по NY-дням текущего и базового периода"
                spec={{
                  kind: "line",
                  unit: "поездок",
                  categories: f3.current.rows.map((r, i) => `${i + 1}: ${r.key.slice(5)}`),
                  series: [
                    {
                      name: "Текущий период",
                      values: f3.current.rows.map(
                        (r) => r.metrics?.completed_published_rides ?? null,
                      ),
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
            )}
            <details>
              <summary>Значения по дням</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>День</th>
                      <th>Текущий</th>
                      <th>Базовый день</th>
                      <th>Базовый</th>
                    </tr>
                  </thead>
                  <tbody>
                    {f3?.current.rows.map((r, i) => (
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
                        <td>{show(f3.baseline?.rows[i]?.metrics?.completed_published_rides)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
        </>
      )}

      <section className="panels">
        <article className="panel" data-risk-queue={risk.ok ? risk.value.status : "unavailable"}>
          <Kind kind="simulation" />
          <h2>
            <Term name="empty_full_risk">Очередь сценарных рисков</Term>
          </h2>
          {!risk.ok ? (
            <p>{risk.text}</p>
          ) : risk.value.status === "empty" ? (
            <p>Сценарии F5 не опубликованы.</p>
          ) : (
            <>
              <p className={risk.value.status === "archival_replay" ? "warning-text" : ""}>
                {risk.value.status === "archival_replay"
                  ? "Архивный recorded-as-of replay — не текущая операционная рекомендация."
                  : "Сценарий в пределах TTL начального GBFS состояния."}{" "}
                As-of {nyTime(risk.value.publication?.data_as_of)}; покрытие{" "}
                {String((risk.value.publication?.coverage as Coverage)?.eligible)}/
                {String((risk.value.publication?.coverage as Coverage)?.requested)} станций.
              </p>
              <ol className="queue">
                {queue.map((q) => (
                  <li key={q.station_id}>
                    <Link href={`/stations?station_id=${encodeURIComponent(q.station_id)}`}>
                      {q.station_id}
                    </Link>{" "}
                    — {q.label} {percent(q.fraction, 0)} (no-action, scale 1, uniform; Wilson 95%{" "}
                    {percent(q.low, 0)}–{percent(q.high, 0)})
                  </li>
                ))}
              </ol>
              <Link href="/scenarios">Все сценарии и политики ↗</Link>
            </>
          )}
        </article>
        <article className="panel" data-forecast-state={forecastStatus}>
          <Kind kind="forecast" />
          <h2>
            <Term name="forecast">Прогноз F4</Term>
          </h2>
          <p>
            {forecastStatus === "valid"
              ? `Действительный прогноз до ${nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)}.`
              : forecastStatus === "expired"
                ? `Недоступен: опубликованный прогноз истёк ${nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)}. Значения не показываются как свежие.`
                : forecastStatus === "empty"
                  ? "Прогноз не опубликован."
                  : forecast.ok
                    ? "Прогноз недоступен."
                    : forecast.text}
          </p>
          <Link href="/forecasts">Статус и версия прогноза ↗</Link>
        </article>
      </section>

      <section className="panel provenance" aria-label="Источники и свежесть">
        <p className="eyebrow">ИСТОЧНИКИ И СВЕЖЕСТЬ</p>
        <table>
          <thead>
            <tr>
              <th>Слой</th>
              <th>Версия</th>
              <th>Актуальность</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>F3 Gold · история поездок</td>
              <td>{short(f3?.kpi.publication?.publication_id)}</td>
              <td>Silver as-of {nyTime(f3?.kpi.publication?.data_as_of)}</td>
              <td>{f3?.kpi.publication?.quality_status ?? "N/A"}</td>
            </tr>
            <tr>
              <td>GBFS наблюдения (F2/F3)</td>
              <td>{String(gbfs.ok ? gbfs.value.availability.availability_version : "N/A")}</td>
              <td>
                source{" "}
                {nyTime(gbfs.ok ? gbfs.value.availability.last_captured_status_updated : null)}
              </td>
              <td>{gbfs.ok ? String(gbfs.value.availability.freshness_status) : "unavailable"}</td>
            </tr>
            <tr>
              <td>F4 прогноз</td>
              <td>{short(forecast.ok ? forecast.value.publication?.publication_id : null)}</td>
              <td>
                valid_until {nyTime(forecast.ok ? forecast.value.publication?.valid_until : null)}
              </td>
              <td>{forecastStatus}</td>
            </tr>
            <tr>
              <td>F5 сценарии</td>
              <td>{short(risk.ok ? risk.value.publication?.publication_id : null)}</td>
              <td>as-of {nyTime(risk.ok ? risk.value.publication?.data_as_of : null)}</td>
              <td>{risk.ok ? risk.value.status : "unavailable"}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section className="f1-slice" aria-label="Первый вертикальный срез F1">
        <p className="eyebrow">ПЕРВЫЙ ВЕРТИКАЛЬНЫЙ СРЕЗ F1 · ОТДЕЛЬНАЯ ПУБЛИКАЦИЯ</p>
        <p className="lead">
          Отдельная публикация одного архива (апрель 2026). Фильтр периода применяется и здесь; даты
          вне окна F1 дают ошибку периода, а не ноль.
        </p>
        <section className="panels" aria-label="Состояние и опубликованные данные">
          <article className="panel" data-api-status={status}>
            <p className="eyebrow">СВЯЗЬ С ПЛАТФОРМОЙ</p>
            <h2>
              <span className={`dot ${healthy ? "online" : "offline"}`} />
              {healthy ? "API доступен" : "API недоступен"}
            </h2>
            <p>
              {healthy
                ? "Сервис отвечает. Готовность бизнес-данных проверяется отдельно."
                : "Не удалось связаться с API. Показатели не подменяются нулём."}
            </p>
            <form action="/" method="get">
              <button className="refresh" type="submit">
                Проверить снова ↗
              </button>
            </form>
            <small>Состояние проверяется при открытии страницы.</small>
          </article>
          <article className="panel data-panel" data-data-state={data.state}>
            <p className="eyebrow">NYC · ОПУБЛИКОВАННЫЕ ПОЕЗДКИ F1</p>
            {data.state === "ok" ? (
              <>
                <h2>Допустимые поездки за период</h2>
                <strong
                  className="kpi"
                  data-completed-rides={data.count}
                  data-publication-id={data.publication.publication_id}
                >
                  {numbers.format(data.count)}
                </strong>
                <p>
                  {formatDate(data.filters.start)} → {formatDate(data.filters.end)}
                  <br />
                  America/New_York · конец интервала не включён.
                </p>
              </>
            ) : (
              <>
                <h2>
                  {data.state === "empty"
                    ? "Нет опубликованных данных"
                    : data.state === "error"
                      ? "Ошибка запроса"
                      : "Данные недоступны"}
                </h2>
                <p>{data.message}</p>
                {data.state === "error" && <Link href="/">Сбросить фильтры</Link>}
              </>
            )}
          </article>
        </section>
        {data.state === "ok" && (
          <section className="panel trend-panel">
            <p className="eyebrow">ВРЕМЕННОЙ ТРЕНД F1</p>
            <h2>Поездки по часу начала</h2>
            <p>
              Ключи часов — UTC, подписи календаря — America/New_York. Пропуск наблюдения не
              обозначается нулевым спросом.
            </p>
            <Trend points={data.points} filters={data.filters} />
          </section>
        )}
        {pub && (
          <section className="panel provenance">
            <p className="eyebrow">ИСТОЧНИК И КАЧЕСТВО F1</p>
            <h2>
              {pub.quality_status === "pass"
                ? "Проверенный архив"
                : "Опубликовано с ограничениями качества"}
            </h2>
            <p>{pub.source} · NYC · America/New_York</p>
            {pub.source_url.startsWith("https://") ? (
              <a href={pub.source_url}>Официальный исходный архив ↗</a>
            ) : (
              <p>Контролируемый тестовый источник, не реальные поездки.</p>
            )}
            <dl>
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
      </section>
      <footer>
        Независимый исследовательский проект на публичных данных Citi Bike, не связан с оператором.
        Факты, GBFS-наблюдения, прогнозы и симуляции помечены отдельно.
      </footer>
    </main>
  );
}

type Comparison = { relative_change: number | null; reason: string | null };
type Coverage = { eligible: number; requested: number };

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
