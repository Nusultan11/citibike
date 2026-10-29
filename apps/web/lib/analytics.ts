export type Metrics = {
  completed_published_rides: number | null;
  average_duration_seconds: number | null;
  departures: number | null;
  arrivals: number | null;
  net_flow: number | null;
  route_coverage: number | null;
};
export type AnalyticsRow = {
  key: string;
  metrics: Metrics | null;
  station_id: string | null;
  from_station: string | null;
  to_station: string | null;
  dimension: string | null;
  category: string | null;
  n: number | null;
  known_share: number | null;
  unknown_count: number | null;
};
export type AnalyticsData = {
  status: "ok" | "empty" | "unavailable" | "stale";
  message: string | null;
  publication: {
    publication_id: string;
    source: string;
    source_snapshot_id: string;
    data_as_of: string;
    timezone: string;
    scope: string;
    pipeline_version: string;
    geo_mapping_version: string;
    quality_status: string;
    data_mode: string;
    population: string;
    period_start: string;
    period_end: string;
  } | null;
  filters: { start_date: string; end_date: string; publication_id: string };
  metrics: Metrics | null;
  rows: AnalyticsRow[];
  total_groups: number | null;
  availability: Record<string, unknown>;
  coverage: Record<string, unknown>;
};
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function checkedMetrics(value: unknown): void {
  if (value === null) return;
  if (!object(value)) throw new Error("Некорректные метрики API");
  for (const key of [
    "completed_published_rides",
    "average_duration_seconds",
    "departures",
    "arrivals",
    "net_flow",
    "route_coverage",
  ]) {
    if (!(value[key] === null || (typeof value[key] === "number" && Number.isFinite(value[key]))))
      throw new Error("Некорректное значение метрики API");
  }
}
export async function analytics(endpoint: string, params: URLSearchParams): Promise<AnalyticsData> {
  const base = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
  const response = await fetch(`${base}/v2/analytics/${endpoint}?${params}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    if (response.status === 422)
      throw new Error("Фильтры не поддержаны: проверьте период, размер часового окна и станцию.");
    throw new Error("Аналитический API недоступен. Значения не заменены нулями.");
  }
  const value: unknown = await response.json();
  if (
    !object(value) ||
    !["ok", "empty", "unavailable", "stale"].includes(String(value.status)) ||
    !Array.isArray(value.rows) ||
    !object(value.filters)
  )
    throw new Error("Некорректный ответ аналитического API");
  if (value.publication !== null) {
    if (
      !object(value.publication) ||
      typeof value.publication.publication_id !== "string" ||
      !/^[0-9a-f]{64}$/.test(value.publication.publication_id) ||
      value.publication.scope !== "NYC" ||
      value.publication.timezone !== "America/New_York"
    )
      throw new Error("Некорректная публикация API");
    if (
      params.has("publication_id") &&
      params.get("publication_id") !== value.publication.publication_id
    )
      throw new Error("Версии ответов API не совпадают");
  }
  checkedMetrics(value.metrics);
  for (const row of value.rows) {
    if (!object(row) || typeof row.key !== "string") throw new Error("Некорректная строка API");
    checkedMetrics(row.metrics);
  }
  return value as AnalyticsData;
}
