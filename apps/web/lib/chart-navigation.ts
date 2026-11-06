/** Shared mouse/keyboard drill target. Dates remain NY calendar keys, not UTC hours. */
export function drillHref(drill: { base: string; to: "hour" | "day" }, key?: string) {
  if (!key) return null;
  if (drill.to === "hour" && !/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  if (drill.to === "day" && !/^\d{4}-\d{2}(-01)?$/.test(key)) return null;
  const start = drill.to === "hour" ? key : `${key.slice(0, 7)}-01`;
  const end = new Date(`${start}T00:00:00Z`);
  if (!Number.isFinite(end.getTime()) || end.toISOString().slice(0, 10) !== start) return null;
  if (drill.to === "hour") end.setUTCDate(end.getUTCDate() + 1);
  else end.setUTCMonth(end.getUTCMonth() + 1);
  const url = new URL(drill.base, "http://localhost");
  url.searchParams.set("start_date", start);
  url.searchParams.set("end_date", end.toISOString().slice(0, 10));
  url.searchParams.set("grain", drill.to);
  return `${url.pathname}${url.search}`;
}
