import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { test } from "node:test";
import { FINDING_AREAS, FINDINGS } from "./findings.ts";

const repo = new URL("../../../", import.meta.url);

test("research findings cite committed evidence and keep explicit status", () => {
  const ids = new Set<string>();
  for (const finding of FINDINGS) {
    assert.ok(!ids.has(finding.id), `duplicate finding ${finding.id}`);
    ids.add(finding.id);
    assert.ok(finding.area in FINDING_AREAS, `unknown area ${finding.area}`);
    assert.ok(["verified", "limited", "not_verified"].includes(finding.status));
    assert.match(finding.recorded, /^\d{4}-\d{2}-\d{2}$/);
    assert.match(finding.source, /^docs\/evidence\/[\w.-]+\.(json|md)$/);
    assert.ok(existsSync(new URL(finding.source, repo)), `missing source ${finding.source}`);
    for (const text of [finding.value, finding.title, finding.takeaway, finding.limitation])
      assert.ok(text.trim().length > 0, `empty text in ${finding.id}`);
  }
  // Every area shown in the filter has at least one finding.
  for (const area of Object.keys(FINDING_AREAS))
    assert.ok(
      FINDINGS.some((f) => f.area === area),
      `no findings for ${area}`,
    );
});
