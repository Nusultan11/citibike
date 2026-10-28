import Link from "next/link";
import { getApiStatus } from "../lib/health";

export const dynamic = "force-dynamic";

export default async function Home() {
  const status = await getApiStatus();
  const healthy = status === "ok";

  return (
    <main>
      <header>
        <Link className="brand" href="/">
          BikeFlow <span>Intelligence</span>
        </Link>
        <span className="phase">Foundation · F0</span>
      </header>
      <section className="intro">
        <p className="eyebrow">ГОРОД В ДВИЖЕНИИ</p>
        <h1>
          Основа для решений
          <br />о городском велопрокате.
        </h1>
        <p className="lead">
          Сначала — надёжная платформа. Затем — проверяемые данные, аналитика и прогнозы.
        </p>
      </section>
      <section className="panels" aria-label="Состояние платформы">
        <article className="panel" data-api-status={status}>
          <p className="eyebrow">СВЯЗЬ С ПЛАТФОРМОЙ</p>
          <h2>
            <span className={`dot ${healthy ? "online" : "offline"}`} />
            {healthy ? "API доступен" : "API недоступен"}
          </h2>
          <p>
            {healthy
              ? "Сервис отвечает на проверку работоспособности."
              : "Не удалось получить корректный ответ от сервиса. Попробуйте проверить ещё раз."}
          </p>
          <form action="/" method="get">
            <button className="refresh" type="submit">
              Проверить снова <span aria-hidden="true">↗</span>
            </button>
          </form>
          <small>Состояние проверяется при открытии страницы.</small>
        </article>
        <article className="panel data-panel">
          <p className="eyebrow">ДАННЫЕ</p>
          <h2>Данные ещё не загружены</h2>
          <p>
            Аналитические показатели пока недоступны. Загрузка и проверка реального архива поездок
            предусмотрены на этапе F1.
          </p>
          <span className="tag">Ожидается первый источник</span>
        </article>
      </section>
      <footer>Независимый исследовательский проект на публичных данных Citi Bike.</footer>
    </main>
  );
}
