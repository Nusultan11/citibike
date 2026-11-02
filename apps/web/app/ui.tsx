import Link from "next/link";
import type { ReactNode } from "react";
import { withParams } from "../lib/operations";

// Short definitions from METRICS.md; tooltips never replace the normative text.
export const DEFINITIONS = {
  completed_published_rides:
    "Число допустимых опубликованных ride_id с началом в [start, end). Не весь спрос и не наличие велосипедов.",
  average_duration:
    "Сумма допустимых длительностей / число допустимых длительностей; знаменатель явен.",
  departures: "Поездки с известной start station и началом в периоде.",
  arrivals: "Поездки с известной end station и окончанием в периоде.",
  net_trip_flow: "arrivals − departures. Не изменение фактического inventory: перевозки не видны.",
  trip_category_share:
    "Поездки категории / поездки с известной сопоставимой категорией; unknown показан отдельно.",
  route_volume: "Поездки по паре известных start/end station; доля покрытия указывается.",
  active_stations:
    "Станции, допускающие работу в выбранный момент по GBFS; N/A без полного покрытия сети.",
  observed_coverage: "Длительность подтверждённых GBFS наблюдений / целевая длительность окна.",
  status_freshness_sec: "Текущий момент − последняя source timestamp GBFS.",
  period_change:
    "(текущее − базовое) / базовое; только при равных днях, часах UTC, наборе weekday, полном покрытии и базовом ≠ 0.",
  forecast:
    "ML-оценка опубликованных поездок по станции и часу с origin, горизонтом и версией; не факт.",
  empty_full_risk:
    "Сценарная доля розыгрышей, где станция хотя бы раз пуста/полна за горизонт; Monte Carlo интервал — не калибровка.",
  simulated_unserved:
    "Ожидаемые неисполненные попытки аренды/возврата только по допущениям симулятора.",
  objective:
    "loss + λ·cost: simulated unserved + cost_weight × относительная стоимость перемещений.",
} as const;
export type Definition = keyof typeof DEFINITIONS;

export function Term({ name, children }: { name: Definition; children: ReactNode }) {
  return (
    <button type="button" className="term" title={DEFINITIONS[name]} data-term={name}>
      {children}
      <span className="term-mark" aria-hidden="true">
        ?
      </span>
      <span className="term-tip" role="tooltip">
        {DEFINITIONS[name]}
      </span>
    </button>
  );
}

const PAGES = [
  ["/", "Обзор"],
  ["/analytics", "Аналитика"],
  ["/stations", "Станции"],
  ["/forecasts", "Прогнозы"],
  ["/scenarios", "Сценарии"],
] as const;

/** Navigation keeps the shared filters so every page reads the same scope. */
export function SiteHeader({ current, params }: { current: string; params: URLSearchParams }) {
  const shared = new URLSearchParams();
  for (const key of ["start_date", "end_date", "vehicle", "membership", "publication_id"]) {
    const value = params.get(key);
    if (value) shared.set(key, value);
  }
  const station = params.get("station_id");
  return (
    <header>
      <Link className="brand" href="/">
        BikeFlow <span>Intelligence</span>
      </Link>
      <nav className="site-nav" aria-label="Разделы">
        {PAGES.map(([href, label]) => {
          const query = new URLSearchParams(shared);
          if (station && (href === "/stations" || href === "/forecasts" || href === "/analytics"))
            query.set("station_id", station);
          return (
            <Link
              key={href}
              href={withParams(href, query)}
              aria-current={current === href ? "page" : undefined}
            >
              {label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}

export function Kind({ kind }: { kind: "fact" | "forecast" | "simulation" | "realtime" }) {
  const label = {
    fact: "Факт · история поездок",
    forecast: "Прогноз · ML",
    simulation: "Симуляция · сценарий",
    realtime: "GBFS · наблюдение состояния",
  }[kind];
  return <span className={`kind kind-${kind}`}>{label}</span>;
}

export function StatePanel({
  state,
  title,
  children,
}: {
  state: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section
      className={`panel state-${state}`}
      role={state === "error" ? "alert" : "status"}
      data-state={state}
    >
      <h2>{title}</h2>
      {children}
    </section>
  );
}

const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
export const show = (n: number | null | undefined, digits = 1) =>
  n == null
    ? "N/A"
    : digits === 1
      ? number.format(n)
      : n.toLocaleString("ru-RU", { maximumFractionDigits: digits });
export const percent = (n: number | null | undefined, digits = 1) =>
  n == null ? "N/A" : `${(n * 100).toLocaleString("ru-RU", { maximumFractionDigits: digits })}%`;
const instant = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "America/New_York",
  dateStyle: "medium",
  timeStyle: "short",
});
export const nyTime = (value: unknown) =>
  typeof value === "string" && Number.isFinite(Date.parse(value))
    ? `${instant.format(new Date(value))} NY`
    : "N/A";
export const short = (id: unknown) => (typeof id === "string" ? `${id.slice(0, 12)}…` : "N/A");
