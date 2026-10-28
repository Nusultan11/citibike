export type ApiStatus = "ok" | "unavailable";

export async function getApiStatus(): Promise<ApiStatus> {
  try {
    const baseUrl = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
    const url = new URL("/health", baseUrl);
    if (!["http:", "https:"].includes(url.protocol)) return "unavailable";
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) return "unavailable";
    const payload: unknown = await response.json();
    const healthy =
      payload !== null &&
      typeof payload === "object" &&
      "status" in payload &&
      payload.status === "ok";
    return healthy ? "ok" : "unavailable";
  } catch {
    return "unavailable";
  }
}
