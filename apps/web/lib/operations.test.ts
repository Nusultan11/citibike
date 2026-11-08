import assert from "node:assert/strict";
import { test } from "node:test";
import {
  bestPolicies,
  comparisonWindow,
  defaultWindow,
  forecastState,
  localHourProfile,
  observedMaximum,
  type RiskData,
  readFilters,
  sameTripSeries,
  splitPartner,
  stationPeriod,
} from "./operations.ts";

test("duplicate trip charts require the same observed buckets and values", () => {
  const months = [
    { key: "2026-02-01", value: null },
    { key: "2026-03-01", value: 2945320 },
  ];
  assert.equal(
    sameTripSeries(
      months,
      months.map((p) => ({ ...p })),
    ),
    true,
  );
  assert.equal(sameTripSeries(months, [{ key: "2026-03-01", value: 2945320 }]), false);
  assert.equal(sameTripSeries(months, [{ key: "2026-02-01", value: 0 }, months[1]]), false);
  assert.equal(sameTripSeries(months, [months[0], { key: "2026-03-02", value: 2945320 }]), false);
  assert.equal(sameTripSeries(months, [months[0], { key: "2026-03-01", value: 2945321 }]), false);
  assert.equal(sameTripSeries([], []), false);
});

test("chart descriptions use observed maxima without inventing zero or breaking ties", () => {
  assert.equal(observedMaximum([]), null);
  assert.equal(observedMaximum([{ key: "absent", value: null }]), null);
  const zero = { key: "observed-zero", value: 0 };
  assert.equal(observedMaximum([{ key: "absent", value: null }, zero]), zero);
  const peak = { key: "2026-03-10", value: 152807 };
  assert.equal(observedMaximum([zero, peak, { key: "2026-03-11", value: 152807 }]), peak);
});

test("absent F3 period stays unavailable without fictional dates or null arrows", () => {
  assert.equal(stationPeriod(null, null), " · период не опубликован");
  assert.equal(stationPeriod("2026-04-01", null), " · период не опубликован");
  assert.equal(stationPeriod("2026-04-01", "2026-05-01"), " за 2026-04-01 → 2026-05-01");
});

test("filters: duplicates, enum, reversed and half-specified periods are errors", () => {
  assert.match(readFilters({ start_date: ["a", "b"] }).error, /один раз/);
  assert.match(readFilters({ vehicle: "scooter" }).error, /Недопустимое/);
  assert.match(readFilters({ start_date: "2026-04-02", end_date: "2026-04-01" }).error, /пуст/);
  assert.match(readFilters({ start_date: "2026-04-02" }).error, /обе даты/);
  assert.match(readFilters({ start_date: "2026/04/02", end_date: "2026-04-03" }).error, /ГГГГ/);
  const ok = readFilters({ start_date: "2026-09-03", end_date: "2026-10-01", station_id: "1.1" });
  assert.equal(ok.error, "");
  assert.equal(ok.params.get("station_id"), "1.1");
});

test("comparison keeps weekday mix (52 weeks) or falls back to adjacent period", () => {
  assert.deepEqual(comparisonWindow("2026-09-03", "2026-10-01", "2024-10-01"), {
    start: "2025-09-04",
    end: "2025-10-02",
    rule: "52 недели назад",
  });
  assert.deepEqual(comparisonWindow("2024-11-01", "2024-11-08", "2024-10-01"), {
    start: "2024-10-25",
    end: "2024-11-01",
    rule: "предыдущий период",
  });
  assert.equal(comparisonWindow("2024-10-01", "2024-10-08", "2024-10-01"), null);
  assert.deepEqual(defaultWindow("2026-10-01T04:00:00+00:00"), {
    start: "2026-09-03",
    end: "2026-10-01",
  });
});

test("DST: NY fall-back hour has two UTC keys, spring-forward hour has none", () => {
  const fall = localHourProfile([
    { key: "2025-11-02T05:00:00Z", value: 3 }, // 01:00 EDT
    { key: "2025-11-02T06:00:00Z", value: 4 }, // 01:00 EST again
    { key: "2025-11-02T07:00:00Z", value: null }, // unobserved, not zero
  ]);
  assert.deepEqual(fall, [{ weekday: 6, hour: 1, value: 7, hours: 2 }]);
  const spring = localHourProfile([
    { key: "2026-03-08T06:00:00Z", value: 1 }, // 01:00 EST
    { key: "2026-03-08T07:00:00Z", value: 2 }, // 03:00 EDT; 02:00 does not exist
  ]);
  assert.deepEqual(
    spring.map((c) => c.hour),
    [1, 3],
  );
});

test("station ID split pairs are flagged both ways only when the partner exists", () => {
  const universe = new Set(["5980.1", "5980.10", "6611.07", "7000.1"]);
  assert.equal(splitPartner("5980.1", universe), "5980.10");
  assert.equal(splitPartner("5980.10", universe), "5980.1");
  assert.equal(splitPartner("7000.1", universe), null);
  assert.equal(splitPartner("6611.07", universe), null);
});

test("expired or stale forecasts are never valid", () => {
  const now = new Date("2026-11-02T00:00:00Z");
  assert.equal(forecastState("stale", "2026-10-30T12:00:00Z", now), "expired");
  assert.equal(forecastState("ok", "2026-10-30T12:00:00Z", now), "expired");
  assert.equal(forecastState("ok", "2026-11-03T00:00:00Z", now), "valid");
  assert.equal(forecastState("unavailable", "2026-11-03T00:00:00Z", now), "unavailable");
  assert.equal(forecastState("ok", null, now), "unavailable");
  assert.equal(forecastState("empty", null, now), "empty");
});

test("best policy uses loss + λ·cost and keeps ties", () => {
  const cell = (policy: string, objective: number) =>
    ({
      scale: 1,
      event_order: "uniform",
      policy,
      totals: { scenario_objective: { mean: objective, p05: 0, p95: 0 } },
    }) as unknown as RiskData["outcomes"][number];
  const best = bestPolicies([
    cell("no_action", 1.5),
    cell("heuristic", 7.4),
    cell("optimizer", 1.5),
  ]);
  assert.deepEqual(best.get("1|uniform"), ["no_action", "optimizer"]);
});
