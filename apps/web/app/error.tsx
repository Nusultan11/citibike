"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <main id="content">
      <section className="callout tone-danger" role="alert" data-state="error">
        <span className="callout-icon" aria-hidden="true">
          !
        </span>
        <div>
          <h2>Страница не отрисовалась</h2>
          <p>
            Произошла ошибка при подготовке данных. Значения не показываются, чтобы не выдать
            неполный результат за верный.
          </p>
          <p className="actions">
            <button className="btn btn-secondary btn-sm" type="button" onClick={() => reset()}>
              Повторить
            </button>{" "}
            <a className="btn btn-ghost btn-sm" href="/">
              На обзор
            </a>
          </p>
        </div>
      </section>
    </main>
  );
}
