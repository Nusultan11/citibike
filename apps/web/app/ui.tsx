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

/* ───────── Icons (inline SVG, no dependency) ───────── */
const PATHS = {
  overview: "M3 3h7v7H3zM14 3h7v4h-7zM14 11h7v10h-7zM3 14h7v7H3z",
  analytics: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  stations:
    "M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12zM12 11.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  forecast: "M3 17l5-5 4 4 8-9M15 7h5v5",
  scenarios: "M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  alert:
    "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
  check: "M22 11.1V12a10 10 0 1 1-5.9-9.1M22 4 12 14l-3-3",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  database:
    "M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3zM4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  off: "M2 2l20 20M8.5 8.5A5 5 0 0 0 12 17a5 5 0 0 0 3.5-1.5M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2",
  arrow: "M5 12h14M13 6l6 6-6 6",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  layers: "M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  spark:
    "M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8",
  fact: "M4 4h16v16H4zM8 9h8M8 13h8M8 17h5",
  sim: "M4 4h6v6H4zM14 14h6v6h-6zM10 7h4a3 3 0 0 1 3 3v4M7 10v4a3 3 0 0 0 3 3h4",
  live: "M5 12.6a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M2 9a15 15 0 0 1 20 0M12 20h.01",
  bike: "M5.5 17.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM18.5 17.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM5.5 14 9 6h4M15 6h2l1.5 8M9 6l3.5 8H18",
} as const;
export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}

/* ───────── Shell ───────── */
const PAGES = [
  ["/", "Обзор", "overview"],
  ["/analytics", "Аналитика", "analytics"],
  ["/stations", "Станции", "stations"],
  ["/forecasts", "Прогнозы", "forecast"],
  ["/scenarios", "Сценарии", "scenarios"],
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
    <header className="topbar">
      <a className="skip-link" href="#content">
        К содержимому
      </a>
      <div className="topbar-inner">
        <Link className="brand" href="/" aria-label="BikeFlow Intelligence — обзор">
          <span className="brand-mark">
            <Icon name="bike" size={18} />
          </span>
          <span>
            BikeFlow <span className="brand-sub">Intelligence</span>
          </span>
        </Link>
        <nav className="site-nav" aria-label="Разделы">
          {PAGES.map(([href, label, icon]) => {
            const query = new URLSearchParams(shared);
            if (station && (href === "/stations" || href === "/forecasts" || href === "/analytics"))
              query.set("station_id", station);
            return (
              <Link
                key={href}
                href={withParams(href, query)}
                aria-current={current === href ? "page" : undefined}
              >
                <Icon name={icon} size={15} />
                {label}
              </Link>
            );
          })}
        </nav>
        <span className="topbar-meta">
          <Icon name="clock" size={13} />
          NYC · America/New_York
        </span>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      Независимый исследовательский проект на публичных данных Citi Bike, не связан с оператором.
      Факты, GBFS-наблюдения, прогнозы и симуляции помечены отдельно; N/A никогда не заменяется
      нулём.
    </footer>
  );
}

export function PageHead({
  eyebrow,
  title,
  lead,
  children,
}: {
  eyebrow: ReactNode;
  title: ReactNode;
  lead?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="page-head">
      <div className="page-head-text">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {lead && <p className="lead">{lead}</p>}
      </div>
      {children && <div className="meta-row">{children}</div>}
    </section>
  );
}

const KIND = {
  fact: ["Факт · история поездок", "Факт", "fact"],
  forecast: ["Прогноз · ML", "Прогноз", "forecast"],
  simulation: ["Симуляция · сценарий", "Симуляция", "sim"],
  realtime: ["GBFS · наблюдение", "GBFS", "live"],
} as const;

/** Information kind badge: icon + text, so meaning never relies on colour alone. */
export function Kind({ kind, compact }: { kind: keyof typeof KIND; compact?: boolean }) {
  const [label, shortLabel, icon] = KIND[kind];
  return (
    <span className={`kind kind-${kind}`} title={label}>
      <Icon name={icon} size={12} />
      {compact ? shortLabel : label}
    </span>
  );
}

export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

export function Status({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`status tone-${tone}`}>{children}</span>;
}

const TONE_ICON: Record<Tone, IconName> = {
  success: "check",
  warning: "alert",
  danger: "alert",
  info: "info",
  neutral: "info",
};

export function Callout({
  tone,
  title,
  children,
  icon,
  ...rest
}: {
  tone: Tone;
  title: ReactNode;
  children?: ReactNode;
  icon?: IconName;
} & Record<`data-${string}`, string | number | undefined>) {
  return (
    <section className={`callout tone-${tone}`} {...rest}>
      <span className="callout-icon">
        <Icon name={icon ?? TONE_ICON[tone]} size={17} />
      </span>
      <div>
        <h2>{title}</h2>
        {children}
      </div>
    </section>
  );
}

const STATE_TONE: Record<string, Tone> = {
  error: "danger",
  unavailable: "warning",
  expired: "warning",
  empty: "neutral",
};

/** Error / unavailable / empty block with role, tone and an explicit state marker. */
export function StatePanel({
  state,
  title,
  children,
}: {
  state: string;
  title: string;
  children: ReactNode;
}) {
  const tone = STATE_TONE[state] ?? "info";
  return (
    <section
      className={`callout tone-${tone} state-${state}`}
      role={state === "error" ? "alert" : "status"}
      data-state={state}
    >
      <span className="callout-icon">
        <Icon name={state === "unavailable" ? "off" : TONE_ICON[tone]} size={17} />
      </span>
      <div>
        <h2>{title}</h2>
        {children}
      </div>
    </section>
  );
}

/** Relative change chip; the value comes from the API, never recomputed here. */
export function Delta({ value, label }: { value: number | null | undefined; label?: string }) {
  if (value == null) return <span className="delta na">{label ?? "N/A"}</span>;
  const up = value > 0;
  return (
    <span className={`delta ${up ? "up" : value < 0 ? "down" : "na"}`}>
      {up ? "▲" : value < 0 ? "▼" : "■"} {up ? "+" : ""}
      {percent(value)}
    </span>
  );
}

/** Server-rendered sparkline; gaps (null) break the line instead of dropping to zero. */
export function Sparkline({
  values,
  color = "var(--series-1)",
  label,
}: {
  values: (number | null)[];
  color?: string;
  label: string;
}) {
  const finite = values.filter((v): v is number => v !== null);
  if (finite.length < 2) return null;
  const max = Math.max(...finite);
  const min = Math.min(0, ...finite);
  const w = 240;
  const h = 44;
  const x = (i: number) => (values.length === 1 ? 0 : (i / (values.length - 1)) * w);
  const y = (v: number) => h - 3 - ((v - min) / (max - min || 1)) * (h - 6);
  const parts: string[] = [];
  let open = false;
  values.forEach((v, i) => {
    if (v === null) {
      open = false;
      return;
    }
    parts.push(`${open ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    open = true;
  });
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${w} ${h}`}
      width="100%"
      height={h}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      <path
        d={parts.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth={2}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/** Applied filters as removable chips — the scope of every number stays visible. */
export function AppliedFilters({
  path,
  params,
  keys,
}: {
  path: string;
  params: URLSearchParams;
  keys: Partial<Record<string, string>>;
}) {
  const items = Object.entries(keys).filter(([key]) => params.get(key));
  if (items.length === 0) return null;
  return (
    <div className="applied">
      <span>Фильтры:</span>
      {items.map(([key, label]) => (
        <Link
          key={key}
          className="chip"
          href={withParams(path, params, { [key]: "" })}
          aria-label={`Убрать фильтр ${label}`}
        >
          {label}: <b>{params.get(key)}</b> ✕
        </Link>
      ))}
    </div>
  );
}

export function RiskBar({
  value,
  low,
  high,
}: {
  value: number | null;
  low: number | null;
  high: number | null;
}) {
  if (value == null) return <span className="muted">N/A</span>;
  const tone = value >= 0.5 ? "var(--danger)" : value >= 0.2 ? "var(--warning)" : "var(--success)";
  return (
    <span
      className="risk-bar"
      style={{ width: 96 }}
      title={`${percent(value, 0)} (Wilson ${percent(low, 0)}–${percent(high, 0)})`}
    >
      <i style={{ width: `${value * 100}%`, background: tone }} />
      {low != null && high != null && (
        <b style={{ left: `${low * 100}%`, width: `${Math.max(0, (high - low) * 100)}%` }} />
      )}
    </span>
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
const day = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
  year: "numeric",
});
/** NY calendar date key (YYYY-MM-DD) → readable label; the key itself is already NY-local. */
export const dayLabel = (key: string | null | undefined) =>
  key && /^\d{4}-\d{2}-\d{2}$/.test(key)
    ? day.format(new Date(`${key}T00:00:00Z`))
    : (key ?? "N/A");
export const range = (start: string | null | undefined, end: string | null | undefined) =>
  start && end ? `${dayLabel(start)} → ${dayLabel(end)}` : "N/A";
