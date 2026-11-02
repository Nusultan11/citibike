// F6 typed clients and pure presentation rules shared by all product pages.
// Rules here decide what may be shown; they never create values.

export const FILTER_KEYS = [
  "start_date",
  "end_date",
  "vehicle",
  "membership",
  "station_id",
  "publication_id",
] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];
export type SearchInput = Record<string, string | string[] | undefined>;

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const ENUMS: Partial<Record<FilterKey, readonly string[]>> = {
  vehicle: ["classic_bike", "electric_bike", "unknown"],
  membership: ["member", "casual", "unknown"],
};

/** One shared parser: the same params reach every API call on every page. */
export function readFilters(input: SearchInput): { params: URLSearchParams; error: string } {
  const params = new URLSearchParams();
  let error = "";
  for (const key of FILTER_KEYS) {
    const value = input[key];
    if (Array.isArray(value)) error = "Каждый фильтр должен быть задан один раз.";
    else if (value) {
      const allowed = ENUMS[key];
      if (allowed && !allowed.includes(value)) error = `Недопустимое значение фильтра ${key}.`;
      else if ((key === "start_date" || key === "end_date") && !DATE.test(value))
        error = "Даты задаются в формате ГГГГ-ММ-ДД.";
      else params.set(key, value);
    }
  }
  const start = params.get("start_date");
  const end = params.get("end_date");
  if (!error && (start === null) !== (end === null))
    error = "Укажите обе даты периода или ни одной.";
  if (!error && start && end && start >= end)
    error = "Период пустой или перевёрнут: конец не включается и должен быть позже начала.";
  return { params, error };
}

export function withParams(path: string, params: URLSearchParams, extra?: Record<string, string>) {
  const next = new URLSearchParams(params);
  for (const [key, value] of Object.entries(extra ?? {})) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  const query = next.toString();
  return query ? `${path}?${query}` : path;
}

export function addDays(day: string, days: number): string {
  const value = new Date(`${day}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}

export function daysBetween(start: string, end: string): number {
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 864e5);
}

/** NY calendar date of an instant (period bounds are NY midnights). */
export function nyDate(instant: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(
    new Date(instant),
  );
}

/**
 * Comparable baseline: the same NY dates 52 weeks earlier keep an identical weekday
 * mix; if that falls before the publication, the adjacent previous period is used.
 * The API still decides comparability (DST hours, coverage, zero baseline).
 */
export function comparisonWindow(start: string, end: string, first: string) {
  const shifted = { start: addDays(start, -364), end: addDays(end, -364), rule: "52 недели назад" };
  if (shifted.start >= first) return shifted;
  const length = daysBetween(start, end);
  const previous = { start: addDays(start, -length), end: start, rule: "предыдущий период" };
  return previous.start >= first ? previous : null;
}

/** Default overview window: the last 28 complete NY days of the publication. */
export function defaultWindow(periodEnd: string) {
  const end = nyDate(periodEnd);
  return { start: addDays(end, -28), end };
}

export type HourCell = { weekday: number; hour: number; value: number; hours: number };

/**
 * Aggregate UTC-keyed hourly counts into NY weekday × local hour sums, only over
 * observed hours. DST: the repeated 01:00 contributes two UTC hours, the skipped
 * 02:00 none; `hours` keeps the observed denominator explicit.
 */
export function localHourProfile(rows: { key: string; value: number | null }[]): HourCell[] {
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "2-digit",
    hourCycle: "h23",
  });
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const cells = new Map<string, HourCell>();
  for (const row of rows) {
    if (row.value === null) continue;
    const parts = Object.fromEntries(
      format.formatToParts(new Date(row.key)).map((p) => [p.type, p.value]),
    );
    const weekday = days.indexOf(parts.weekday);
    const hour = Number(parts.hour);
    const id = `${weekday}:${hour}`;
    const cell = cells.get(id) ?? { weekday, hour, value: 0, hours: 0 };
    cell.value += row.value;
    cell.hours += 1;
    cells.set(id, cell);
  }
  return [...cells.values()].sort((a, b) => a.weekday - b.weekday || a.hour - b.hour);
}

const SHORT = /^\d+\.\d$/;
/** dddd.d ↔ dddd.d0 raw-source lineage pairs are flagged, never merged. */
export function splitPartner(id: string, universe: Set<string>): string | null {
  if (SHORT.test(id) && universe.has(`${id}0`)) return `${id}0`;
  if (id.endsWith("0") && SHORT.test(id.slice(0, -1)) && universe.has(id.slice(0, -1)))
    return id.slice(0, -1);
  return null;
}

export type ForecastState = "valid" | "expired" | "unavailable" | "empty";
/** A forecast is shown only when published, ok and not past valid_until. */
export function forecastState(
  status: string,
  validUntil: string | null | undefined,
  now: Date,
): ForecastState {
  if (status === "empty") return "empty";
  if (!validUntil || !Number.isFinite(Date.parse(validUntil))) return "unavailable";
  if (status === "stale" || Date.parse(validUntil) <= now.getTime()) return "expired";
  return status === "ok" ? "valid" : "unavailable";
}

export class ApiError extends Error {
  state: "unavailable" | "error";
  constructor(state: "unavailable" | "error", message: string) {
    super(message);
    this.state = state;
  }
}

export function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export async function getJson(
  path: string,
  params?: URLSearchParams,
  timeout = 10000,
): Promise<Record<string, unknown>> {
  const base = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
  const query = params?.toString();
  let response: Response;
  try {
    response = await fetch(new URL(query ? `${path}?${query}` : path, base), {
      cache: "no-store",
      signal: AbortSignal.timeout(timeout),
    });
  } catch {
    throw new ApiError("unavailable", "Сервис данных недоступен. Значения не показываются.");
  }
  if (response.status === 503)
    throw new ApiError("unavailable", "Serving база недоступна. Значения не заменены нулями.");
  if (response.status === 422)
    throw new ApiError("error", "Фильтры не поддержаны опубликованной версией или периодом.");
  if (!response.ok) throw new ApiError("error", "Не удалось получить опубликованные данные.");
  const value: unknown = await response.json();
  if (!object(value)) throw new ApiError("error", "Некорректный ответ API.");
  return value;
}

export type ForecastPoint = {
  station_id: string;
  forecast_origin_utc: string;
  target_hour_utc: string;
  horizon_h: number;
  prediction_type: "departures" | "arrivals";
  predicted_value: number | null;
  status: "ok" | "unavailable";
  reason: string | null;
  model_id: string;
};
export type ForecastData = {
  status: "ok" | "empty" | "stale" | "unavailable";
  publication: Record<string, unknown> | null;
  total_rows: number;
  rows: ForecastPoint[];
};
export async function getForecasts(params: URLSearchParams): Promise<ForecastData> {
  const value = await getJson("/v3/forecasts", params);
  if (!["ok", "empty", "stale", "unavailable"].includes(String(value.status)))
    throw new ApiError("error", "Некорректный статус прогноза.");
  if (!Array.isArray(value.rows)) throw new ApiError("error", "Некорректные строки прогноза.");
  for (const row of value.rows)
    if (
      !object(row) ||
      !(row.predicted_value === null || Number.isFinite(row.predicted_value)) ||
      (row.status === "ok" && (typeof row.predicted_value !== "number" || row.predicted_value < 0))
    )
      throw new ApiError("error", "Прогноз не прошёл проверку формата.");
  return value as unknown as ForecastData;
}

export type StationRow = {
  station_id: string;
  station_name: string | null;
  lat: number | null;
  lon: number | null;
  coordinate_month: string | null;
  coordinate_status: "ok" | "missing" | "unavailable";
  coordinate_changes: number;
  suspected_id_split: boolean;
  split_partner: string | null;
  completed_published_rides: number | null;
  departures: number | null;
  arrivals: number | null;
  net_flow: number | null;
};
export type StationData = {
  status: "ok" | "empty" | "unavailable";
  message: string | null;
  publication: { publication_id: string; data_as_of: string; period_start: string } | null;
  geo: { geo_id: string; version: string; policy: string; created_at: string } | null;
  filters: Record<string, string | number | null>;
  total_stations: number;
  rows: StationRow[];
  coverage: Record<string, unknown>;
  gbfs: Record<string, unknown> | null;
  history: Record<string, unknown>[];
};
export async function getStations(params: URLSearchParams, station?: string): Promise<StationData> {
  const value = await getJson(
    station ? `/v4/stations/${encodeURIComponent(station)}` : "/v4/stations",
    params,
    15000,
  );
  if (!Array.isArray(value.rows)) throw new ApiError("error", "Некорректный ответ станций.");
  for (const row of value.rows) {
    if (!object(row) || typeof row.station_id !== "string")
      throw new ApiError("error", "Некорректная строка станции.");
    for (const key of ["departures", "arrivals", "net_flow", "completed_published_rides"])
      if (!(row[key] === null || Number.isSafeInteger(row[key])))
        throw new ApiError("error", "Некорректная метрика станции.");
  }
  if (
    params.has("publication_id") &&
    object(value.publication) &&
    value.publication.publication_id !== params.get("publication_id")
  )
    throw new ApiError("error", "Версии ответов API не совпадают.");
  return value as unknown as StationData;
}

export type RiskData = {
  status: "current_scenario" | "archival_replay" | "empty" | "unavailable";
  message: string | null;
  publication: Record<string, unknown> | null;
  currency: {
    state: string;
    as_of_age_seconds: number;
    ttl_seconds: number;
    reason: string;
  } | null;
  filters: { publication_id: string | null; scale: number | null; event_order: string | null };
  outcomes: {
    scale: number;
    event_order: string;
    policy: "no_action" | "heuristic" | "optimizer";
    repeats: number;
    totals: Record<string, { mean: number; p05: number; p95: number }>;
    movement_cost: number;
    moved_bikes: number;
    vehicles: number;
    constraint_violations: number;
    gain_vs_no_action: number | null;
  }[];
  stations: {
    station_id: string;
    eligible: boolean;
    reasons: string[];
    capacity: number | null;
    bikes: number | null;
    docks: number | null;
    lat: number | null;
    lon: number | null;
  }[];
  station_outcomes: {
    policy: string;
    station_id: string;
    status: string;
    empty_fraction: number | null;
    empty_low: number | null;
    empty_high: number | null;
    full_fraction: number | null;
    full_low: number | null;
    full_high: number | null;
    repeats: number;
  }[];
  transfers: {
    policy: string;
    source: string;
    destination: string;
    bikes: number;
    vehicles: number;
    minutes: number;
    distance_km: number;
  }[];
};
export async function getRisk(params: URLSearchParams): Promise<RiskData> {
  const value = await getJson("/v4/risk", params);
  if (
    !["current_scenario", "archival_replay", "empty", "unavailable"].includes(
      String(value.status),
    ) ||
    !Array.isArray(value.outcomes) ||
    !Array.isArray(value.stations)
  )
    throw new ApiError("error", "Некорректный ответ сценариев.");
  return value as unknown as RiskData;
}

/** loss + λ·cost per cell; lowest objective wins, ties are reported as ties. */
export function bestPolicies(outcomes: RiskData["outcomes"]) {
  const cells = new Map<string, RiskData["outcomes"]>();
  for (const o of outcomes) {
    const key = `${o.scale}|${o.event_order}`;
    cells.set(key, [...(cells.get(key) ?? []), o]);
  }
  const result = new Map<string, string[]>();
  for (const [key, values] of cells) {
    const best = Math.min(...values.map((v) => v.totals.scenario_objective.mean));
    result.set(
      key,
      values
        .filter((v) => Math.abs(v.totals.scenario_objective.mean - best) <= 1e-9)
        .map((v) => v.policy),
    );
  }
  return result;
}

export function message(cause: unknown): { state: "unavailable" | "error"; text: string } {
  if (cause instanceof ApiError) return { state: cause.state, text: cause.message };
  return {
    state: "error",
    text: cause instanceof Error ? cause.message : "Ответ API не прошёл проверку.",
  };
}
