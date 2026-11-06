import Link from "next/link";

export default function NotFound() {
  return (
    <main id="content">
      <section className="callout tone-neutral" role="status">
        <span className="callout-icon" aria-hidden="true">
          ?
        </span>
        <div>
          <h2>Раздел не найден</h2>
          <p>Такой страницы нет. Доступны обзор, аналитика, станции, прогнозы и сценарии.</p>
          <p className="actions">
            <Link className="btn btn-secondary btn-sm" href="/">
              На обзор
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
