export default function Loading() {
  return (
    <main id="content" aria-busy="true" aria-live="polite">
      <p className="eyebrow">BikeFlow Intelligence</p>
      <div className="skeleton sk-title" />
      <p className="lead" style={{ marginBottom: 20 }}>
        Загружаем опубликованные данные и статусы источников…
      </p>
      <div className="kpi-row">
        <div className="skeleton sk-card" />
        <div className="skeleton sk-card" />
        <div className="skeleton sk-card" />
        <div className="skeleton sk-card" />
      </div>
      <div className="grid" style={{ marginTop: 16 }}>
        <div className="skeleton sk-chart span-8" />
        <div className="skeleton sk-chart span-4" />
      </div>
    </main>
  );
}
