import Link from "next/link";
import { type DashboardData, getDashboard } from "../lib/data";
import { getApiStatus } from "../lib/health";
import Trend from "./trend";

export const dynamic = "force-dynamic";
const numbers = new Intl.NumberFormat("ru-RU");
const dates = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "America/New_York",
  dateStyle: "medium",
  timeStyle: "short",
});
const formatDate = (value: string) => dates.format(new Date(value));

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const start = typeof params.start_date === "string" ? params.start_date : undefined;
  const end = typeof params.end_date === "string" ? params.end_date : undefined;
  const invalid = Array.isArray(params.start_date) || Array.isArray(params.end_date);
  const dataRequest: Promise<DashboardData> = invalid
    ? Promise.resolve({ state: "error", message: "Каждая дата должна быть задана один раз." })
    : getDashboard(start, end);
  const [status, data] = await Promise.all([getApiStatus(), dataRequest]);
  const healthy = status === "ok";
  const pub = "publication" in data ? data.publication : null;

  return (
    <main>
      <header>
        <Link className="brand" href="/">
          BikeFlow <span>Intelligence</span>
        </Link>
        <Link className="phase" href="/analytics">
          Историческая аналитика · F3 ↗
        </Link>
      </header>
      <section className="intro">
        <p className="eyebrow">ГОРОД В ДВИЖЕНИИ</p>
        <h1>
          Поездки, которые
          <br />
          можно проверить.
        </h1>
        <p className="lead">
          История опубликованных поездок Citi Bike. Реальные архивные данные, отдельные от текущей
          доступности велосипедов и скрытого спроса.
        </p>
      </section>
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
          <p className="eyebrow">NYC · ОПУБЛИКОВАННЫЕ ПОЕЗДКИ</p>
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
      {pub && (
        <form className="filters panel" action="/" method="get">
          <label>
            С даты (Нью-Йорк)
            <input
              type="date"
              name="start_date"
              min={pub.period_start.slice(0, 10)}
              max={pub.period_end.slice(0, 10)}
              defaultValue={start ?? pub.period_start.slice(0, 10)}
              required
            />
          </label>
          <label>
            До даты, не включая (Нью-Йорк)
            <input
              type="date"
              name="end_date"
              min={pub.period_start.slice(0, 10)}
              max={pub.period_end.slice(0, 10)}
              defaultValue={end ?? pub.period_end.slice(0, 10)}
              required
            />
          </label>
          <button className="refresh" type="submit">
            Применить период
          </button>
        </form>
      )}
      {data.state === "ok" && (
        <section className="panel trend-panel">
          <p className="eyebrow">ВРЕМЕННОЙ ТРЕНД</p>
          <h2>Поездки по часу начала</h2>
          <p>
            Ключи часов — UTC, подписи календаря — America/New_York. Пропуск наблюдения не
            обозначается нулевым спросом.
          </p>
          <Trend points={data.points} filters={data.filters} />
          <details>
            <summary>Почасовые значения</summary>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Час, Нью-Йорк</th>
                    <th>UTC</th>
                    <th>Опубликованные поездки</th>
                  </tr>
                </thead>
                <tbody>
                  {data.points.map((point) => (
                    <tr key={point.hour_utc}>
                      <td>{formatDate(point.hour_utc)}</td>
                      <td>{point.hour_utc}</td>
                      <td>{numbers.format(point.completed_published_rides)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <details>
            <summary>Определение показателя</summary>
            <p>
              completed_published_rides — число канонических допустимых опубликованных ride_id с
              временем начала в полуоткрытом периоде [start, end). Точные дубликаты считаются один
              раз; конфликтующие ID и недостоверные временные значения изолированы. Это не весь
              спрос и не количество доступных велосипедов.
            </p>
          </details>
        </section>
      )}
      {pub && (
        <section className="panel provenance">
          <p className="eyebrow">ИСТОЧНИК И КАЧЕСТВО ВСЕГО АРХИВА</p>
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
            <dt>Актуальность по времени события</dt>
            <dd>{formatDate(pub.data_as_of)}</dd>
            <dt>Получено / опубликовано</dt>
            <dd>
              {formatDate(pub.retrieved_at)} / {formatDate(pub.published_at)}
            </dd>
            <dt>Входных / канонических строк</dt>
            <dd>
              {numbers.format(pub.input_rows)} / {numbers.format(pub.canonical_rows)}
            </dd>
            <dt>Исключено из канонических</dt>
            <dd>
              {numbers.format(pub.excluded_rows)}: дубликаты{" "}
              {numbers.format(pub.quality_counts.duplicate_exact ?? 0)}, конфликты{" "}
              {numbers.format(pub.quality_counts.conflicting_id ?? 0)}, некорректные{" "}
              {numbers.format(pub.quality_counts.invalid ?? 0)}
            </dd>
            <dt>Поездки без start / end station</dt>
            <dd>
              {numbers.format(pub.canonical_rows - pub.coverage.known_start_station_rides)} /{" "}
              {numbers.format(pub.canonical_rows - pub.coverage.known_end_station_rides)}. Валидные
              поездки включены в общий счётчик, но не в соответствующую станционную метрику.
            </dd>
          </dl>
          <details>
            <summary>Версии и происхождение</summary>
            <p className="hash">
              Source SHA-256: {pub.source_version}
              <br />
              Pipeline: {pub.pipeline_version}
              <br />
              Publication: {pub.publication_id}
            </p>
          </details>
        </section>
      )}
      <footer>
        Независимый исследовательский проект. Исторические поездки не смешиваются с realtime GBFS.
        Данные предоставлены Citi Bike без гарантии полноты.
      </footer>
    </main>
  );
}
