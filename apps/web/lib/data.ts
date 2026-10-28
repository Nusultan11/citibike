export type Publication = {
  publication_id: string;
  source: string;
  source_url: string;
  source_version: string;
  pipeline_version: string;
  scope: "NYC";
  timezone: "America/New_York";
  period_start: string;
  period_end: string;
  data_as_of: string;
  retrieved_at: string;
  published_at: string;
  quality_status: string;
  input_rows: number;
  canonical_rows: number;
  excluded_rows: number;
  reason_counts: Record<string, number>;
  quality_counts: Record<string, number>;
  coverage: Record<string, number>;
};
export type HourPoint = { hour_utc: string; completed_published_rides: number };
export type AppliedFilters = { start: string; end: string };
export type DashboardData =
  | {
      state: "ok";
      publication: Publication;
      count: number;
      points: HourPoint[];
      filters: AppliedFilters;
    }
  | { state: "empty"; publication: Publication | null; message: string }
  | { state: "unavailable" | "error"; message: string };

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function timestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}
function publication(value: unknown): Publication {
  if (!record(value) || value.scope !== "NYC" || value.timezone !== "America/New_York") {
    throw new Error("Invalid publication scope");
  }
  for (const key of [
    "publication_id",
    "source",
    "source_url",
    "source_version",
    "pipeline_version",
    "quality_status",
  ]) {
    if (typeof value[key] !== "string") throw new Error("Invalid publication metadata");
  }
  for (const key of ["period_start", "period_end", "data_as_of", "retrieved_at", "published_at"]) {
    if (!timestamp(value[key])) throw new Error("Invalid publication time");
  }
  for (const key of ["input_rows", "canonical_rows", "excluded_rows"]) {
    if (!count(value[key])) throw new Error("Invalid quality count");
  }
  for (const key of ["reason_counts", "quality_counts", "coverage"]) {
    if (!record(value[key]) || !Object.values(value[key]).every(count))
      throw new Error("Invalid quality metadata");
  }
  const coverage = value.coverage as Record<string, number>;
  for (const key of ["known_start_station_rides", "known_end_station_rides"]) {
    if (!count(coverage[key]) || coverage[key] > (value.canonical_rows as number))
      throw new Error("Invalid station coverage");
  }
  return value as Publication;
}
class DataError extends Error {
  constructor(
    public state: "unavailable" | "error",
    message: string,
  ) {
    super(message);
  }
}
async function fetchData(path: string, query = ""): Promise<Record<string, unknown>> {
  const base = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
  let response: Response;
  try {
    response = await fetch(new URL(path + query, base), {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
  } catch {
    throw new DataError("unavailable", "Сервис данных недоступен. Попробуйте проверить снова.");
  }
  if (response.status === 503)
    throw new DataError(
      "unavailable",
      "База опубликованных данных недоступна. API продолжает работать.",
    );
  if (response.status === 422)
    throw new DataError(
      "error",
      "Некорректный период: выберите даты в пределах опубликованного месяца; конец не включается.",
    );
  if (!response.ok) throw new DataError("error", "Не удалось получить опубликованные данные.");
  const value: unknown = await response.json();
  if (!record(value)) throw new Error("Invalid response");
  return value;
}
export async function getDashboard(start?: string, end?: string): Promise<DashboardData> {
  try {
    const current = await fetchData("/v1/publication");
    if (current.status === "empty" && current.publication === null) {
      return {
        state: "empty",
        publication: null,
        message: "Данные ещё не загружены. Проверенная версия пока не опубликована.",
      };
    }
    if (current.status !== "ok") throw new Error("Invalid publication state");
    const pub = publication(current.publication);
    const params = new URLSearchParams({ publication_id: pub.publication_id });
    if (start) params.set("start_date", start);
    if (end) params.set("end_date", end);
    const query = `?${params}`;
    const [kpi, series] = await Promise.all([
      fetchData("/v1/kpi", query),
      fetchData("/v1/timeseries", query),
    ]);
    if (
      publication(kpi.metadata).publication_id !== pub.publication_id ||
      publication(series.metadata).publication_id !== pub.publication_id
    )
      throw new Error("Mixed publication versions");
    if (
      kpi.status === "empty" &&
      series.status === "empty" &&
      kpi.completed_published_rides === null
    ) {
      return {
        state: "empty",
        publication: pub,
        message: "В выбранном периоде нет наблюдаемых записей. Это не означает отсутствие спроса.",
      };
    }
    if (
      kpi.status !== "ok" ||
      series.status !== "ok" ||
      !count(kpi.completed_published_rides) ||
      !Array.isArray(series.points)
    )
      throw new Error("Invalid metric response");
    const points: HourPoint[] = [];
    for (const point of series.points) {
      if (!record(point) || !timestamp(point.hour_utc) || !count(point.completed_published_rides))
        throw new Error("Invalid hourly point");
      points.push({
        hour_utc: point.hour_utc,
        completed_published_rides: point.completed_published_rides,
      });
    }
    if (
      new Set(points.map((p) => p.hour_utc)).size !== points.length ||
      points.reduce((sum, p) => sum + p.completed_published_rides, 0) !==
        kpi.completed_published_rides
    )
      throw new Error("Hourly reconciliation failed");
    if (!record(kpi.filters) || !timestamp(kpi.filters.start) || !timestamp(kpi.filters.end))
      throw new Error("Invalid filters");
    return {
      state: "ok",
      publication: pub,
      count: kpi.completed_published_rides,
      points,
      filters: { start: kpi.filters.start, end: kpi.filters.end },
    };
  } catch (error) {
    return error instanceof DataError
      ? { state: error.state, message: error.message }
      : {
          state: "error",
          message: "Ответ сервиса данных не прошёл проверку. Значения не отображаются.",
        };
  }
}
