import assert from "node:assert/strict";
import { test } from "node:test";
import { drillHref } from "./chart-navigation.ts";

test("mouse and keyboard drill preserve scope and NY day boundaries across DST", () => {
  const base = "/analytics?station_id=5980.10&publication_id=p&vehicle=electric_bike&grain=month";
  const month = new URL(drillHref({ base, to: "day" }, "2026-03") ?? "", "http://localhost");
  assert.equal(month.searchParams.get("start_date"), "2026-03-01");
  assert.equal(month.searchParams.get("end_date"), "2026-04-01");
  for (const day of ["2026-03-08", "2025-11-02"]) {
    const url = new URL(drillHref({ base, to: "hour" }, day) ?? "", "http://localhost");
    assert.equal(url.searchParams.get("start_date"), day);
    assert.equal(
      url.searchParams.get("end_date"),
      day === "2026-03-08" ? "2026-03-09" : "2025-11-03",
    );
    assert.equal(url.searchParams.get("grain"), "hour");
    assert.equal(url.searchParams.get("station_id"), "5980.10");
    assert.equal(url.searchParams.get("publication_id"), "p");
    assert.equal(url.searchParams.get("vehicle"), "electric_bike");
  }
  assert.equal(drillHref({ base, to: "hour" }, "2026-03-08T07:00Z"), null);
  assert.equal(drillHref({ base, to: "day" }, "2026-03-08"), null);
  assert.equal(drillHref({ base, to: "hour" }, "2026-02-30"), null);
});
