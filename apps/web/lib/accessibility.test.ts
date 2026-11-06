import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
const tokens = Object.fromEntries(
  [...css.matchAll(/--([\w-]+): (#[\da-f]{6});/g)].map((m) => [m[1], m[2]]),
);
function luminance(hex: string) {
  return [0.2126, 0.7152, 0.0722].reduce((n, weight, i) => {
    const value = Number.parseInt(hex.slice(1 + 2 * i, 3 + 2 * i), 16) / 255;
    return n + weight * (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  }, 0);
}
function contrast(a: string, b: string) {
  const [lo, hi] = [luminance(a), luminance(b)].sort((a, b) => a - b);
  return (hi + 0.05) / (lo + 0.05);
}

test("actual dark palette meets AA text, control-boundary and focus contrast", () => {
  const surfaces = ["bg", "surface-1", "surface-2", "surface-3", "surface-inset"];
  for (const bg of surfaces) {
    for (const fg of ["text-1", "text-2", "text-3", "accent-strong"])
      assert.ok(contrast(tokens[fg], tokens[bg]) >= 4.5, `${fg} on ${bg}`);
    for (const fg of ["border-strong", "accent-ring", "series-1", "series-2", "series-3"])
      assert.ok(contrast(tokens[fg], tokens[bg]) >= 3, `${fg} on ${bg}`);
  }
  const primary = css.match(/\.btn-primary \{([^}]+)\}/)?.[1] ?? "";
  const text = primary.match(/color: var\(--([\w-]+)\)/)?.[1];
  assert.ok(text);
  assert.ok(contrast(tokens[text], tokens.accent) >= 4.5);
  assert.ok(contrast(tokens[text], "#6d9bf5") >= 4.5, "primary hover");
});
