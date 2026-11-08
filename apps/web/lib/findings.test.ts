import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import {
  EVIDENCE_REVISION,
  evidenceHref,
  FINDING_AREAS,
  FINDINGS,
  FORECAST_STUDY,
  findingPage,
  findingPopulation,
} from "./findings.ts";

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
    for (const source of [finding.source, ...(finding.relatedSources ?? [])]) {
      assert.ok(existsSync(new URL(source, repo)), `missing source ${source}`);
      assert.equal(new URL(evidenceHref(source)).hostname, "github.com");
      assert.ok(evidenceHref(source).includes(`/blob/${EVIDENCE_REVISION}/docs/evidence/`));
    }
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

const read = (file: string) => readFileSync(new URL(`docs/evidence/${file}`, repo), "utf8");
const finding = (id: string) => {
  const value = FINDINGS.find((f) => f.id === id);
  assert.ok(value, id);
  return value;
};

test("F2 findings agree with recorded row accounting, missing hours and label population", () => {
  const text = read("f2-analysis.md");
  const rows = [
    ...text.matchAll(/^\| (\d{4}-\d{2}) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$/gm),
  ];
  assert.equal(rows.length, 24);
  assert.equal(
    rows.reduce((n, row) => n + Number(row[3]), 0),
    92085994,
  );
  assert.equal(
    rows.reduce((n, row) => n + Number(row[4]), 0),
    92076567,
  );
  assert.equal(
    rows.reduce((n, row) => n + Number(row[6]), 0),
    54,
  );
  assert.match(text, /'invalid': 8886, 'conflicting_id': 58, 'duplicate_exact': 483/);
  assert.match(text, /'independent_row_mismatches': 0/);
  assert.equal(finding("f2-reconciliation").value.replaceAll(" ", ""), "92076567");
  assert.match(text, /zero 19189766\/38602207 \(49\.71% eligible\)/);
  assert.match(finding("f2-sparsity").value, /49,7/);
  assert.match(text, /216 departures \/ 221 arrivals/);
  assert.match(text, /вторник 17:00 NY.*6\.219.*18:00.*5\.997/);
  assert.equal(finding("f2-weekly").status, "limited");
  assert.match(finding("f2-weekly").limitation, /два годовых цикла/);
  assert.match(finding("f2-gaps").limitation, /не установлена/);
});

test("annual and ID-split claims match decision research without upgrading station identity", () => {
  const d = JSON.parse(read("f4-decision-research.json"));
  const sections = d.analytics.sections;
  const annual = sections["1_trends"].result.annual;
  assert.deepEqual(
    annual.map((r: { rides: number }) => r.rides),
    [46697958, 45378096],
  );
  assert.equal(((annual[1].rides / annual[0].rides - 1) * 100).toFixed(1), "-2.8");
  const categories = sections["5_categories"].result.annual as {
    period: string;
    vehicle: string;
    rides: number;
  }[];
  assert.deepEqual(
    annual.map((a: { period: string; rides: number }) =>
      (
        (100 *
          categories
            .filter((r) => r.period === a.period && r.vehicle === "electric_bike")
            .reduce((n, r) => n + r.rides, 0)) /
        a.rides
      ).toFixed(1),
    ),
    ["69.6", "72.3"],
  );
  const ginis = sections["3_concentration"].result.monthly.map((r: { gini: number }) => r.gini);
  assert.equal(Math.min(...ginis).toFixed(2), "0.58");
  assert.equal(Math.max(...ginis).toFixed(2), "0.62");
  assert.match(finding("da-annual").takeaway, /0,58–0,62/);
  assert.equal(d.corrective.lineage.summary.pairs, 105);
  assert.equal(d.corrective.lineage.summary.supported, 105);
  const top = sections["4_imbalance"].result.excluded_stations.slice(0, 40);
  assert.equal(top.length, 40);
  assert.ok(top.every((r: { reason: string }) => r.reason.startsWith("id_split:")));
  assert.match(finding("da-id-split").limitation, /оператором — нет/);
});

test("forecast research retains frozen scores, inconclusive results and unverified promotion", () => {
  const text = read("f4-forecasting.md");
  for (const [target, model, mae] of [
    ["departures", "seasonal_naive", "1.9903"],
    ["arrivals", "seasonal_naive", "1.9610"],
    ["departures", "lightgbm_bounded", "1.5778"],
    ["arrivals", "lightgbm_bounded", "1.5988"],
  ])
    assert.ok(text.includes(`| ${target} | ${model} | 44773 | ${mae} |`));
  assert.match(finding("f4-final").limitation, /promotion NOT VERIFIED/);
  const feature = JSON.parse(read("f4-feature-research.json"));
  assert.equal(feature.outcome_counts.success, 176);
  assert.equal(feature.group_effects.length, 44);
  for (const [verdict, count] of [
    ["inconclusive", 40],
    ["degrades", 3],
    ["improves", 1],
  ])
    assert.equal(
      feature.group_effects.filter((r: { verdict: string }) => r.verdict === verdict).length,
      count,
    );
  const decision = JSON.parse(read("f4-decision-research.json"));
  assert.equal(decision.outcome_counts.success, 872);
  assert.ok(
    decision.gates.every(
      (g: { promote: boolean; degraded_segments: string[] }) =>
        !g.promote && g.degraded_segments.length > 0,
    ),
  );
  assert.ok(
    finding("f4-research").relatedSources?.includes("docs/evidence/f4-decision-research.json"),
  );
  const diagnostics = JSON.parse(read("f4-ml-diagnostics.json"));
  assert.equal(diagnostics.results.anomalies.events, 593);
  const controls = Object.values(diagnostics.results.controls.cases) as { status: string }[];
  assert.equal(controls.length, 12);
  assert.ok(controls.every((c) => c.status === "PASS"));
  assert.match(diagnostics.study_decision, /^inconclusive/);
  assert.equal(finding("f4-diagnostics").status, "not_verified");
  assert.match(finding("f4-diagnostics").limitation, /Precision@K.*NOT VERIFIED/);
});

test("F5 claim is recorded replay with limited coverage, not live operational benefit", () => {
  const scenario = JSON.parse(read("f6-product.json")).real_e2e.scenario;
  assert.equal(scenario.artifact_cells, 27);
  assert.equal(scenario.objective_mismatches, 0);
  assert.equal(scenario.no_action_best_cells, 9);
  assert.equal(scenario.artifact_coverage, "3/64");
  assert.equal(scenario.status, "archival_replay");
  assert.equal(finding("f5-replay").status, "limited");
  assert.match(finding("f5-replay").limitation, /архивная симуляция/);
});

test("each finding has one primary destination and a stated population", () => {
  const destinations = FINDINGS.map((f) => [f.id, findingPage(f)]);
  assert.equal(new Set(destinations.map(([id]) => id)).size, FINDINGS.length);
  assert.equal(destinations.filter(([, path]) => path === "/analytics").length, 5);
  assert.equal(findingPage(finding("da-id-split")), "/stations");
  for (const f of FINDINGS) {
    assert.ok(findingPopulation(f).length > 30);
    if (f.area === "forecast") assert.equal(findingPage(f), "/forecasts");
    if (f.area === "scenario") assert.equal(findingPage(f), "/scenarios");
  }
  const f3 = JSON.parse(read("f3-analytics.json"));
  assert.equal(f3.html_count, 92076054);
  assert.equal(f3.reconciliation_mismatches, 0);
  assert.equal(92076567 - f3.html_count, 513);
});

test("forecast demonstration is an exact small excerpt of frozen results for both tasks", () => {
  const source = JSON.parse(read("f4-forecasting.json"));
  assert.equal(FORECAST_STUDY.targets.length, 2);
  assert.deepEqual(
    FORECAST_STUDY.folds,
    source.validation_splits.map((r: { origin_start: string; origin_end: string }) => ({
      start: r.origin_start,
      end: r.origin_end,
    })),
  );
  for (const target of FORECAST_STUDY.targets) {
    const decision = source.selection.decisions[target.target];
    assert.equal(target.champion, "seasonal_naive");
    assert.equal(target.champion, decision.champion);
    assert.equal(target.models.length, 4);
    for (const model of target.models) {
      const final = source.final_results.find(
        (r: { prediction_type: string; model_name: string }) =>
          r.prediction_type === target.target && r.model_name === model.model,
      );
      assert.equal(model.final, final.metrics.all.mae);
      assert.equal(model.n, final.metrics.all.n);
      const folds = source.validation_results
        .filter(
          (r: { prediction_type: string; model_name: string }) =>
            r.prediction_type === target.target && r.model_name === model.model,
        )
        .map((r: { metrics: { all: { mae: number } } }) => r.metrics.all.mae);
      assert.deepEqual([...model.folds], folds);
      assert.ok(
        Math.abs(
          model.validation - folds.reduce((n: number, x: number) => n + x, 0) / folds.length,
        ) < 1e-12,
      );
      assert.deepEqual(
        [...model.horizons],
        Array.from({ length: 24 }, (_, i) => final.metrics[`horizon:${i + 1}`].mae),
      );
      const gate = decision.gates[model.model];
      if (gate) {
        assert.equal(gate.promote, false);
        assert.equal(model.degraded, gate.degraded_segments.length);
      }
    }
    for (const slice of target.slices) {
      const actual = source.corrective_validation.pooled_error_slices.find(
        (r: { prediction_type: string; name: string; slice: string }) =>
          r.prediction_type === target.target && r.name === slice.name && r.slice === slice.slice,
      );
      for (const key of ["n", "mae", "bias"] as const) assert.equal(slice[key], actual[key]);
    }
    for (const ablation of target.ablation) {
      const actual = source.corrective_validation.ablation.find(
        (r: { prediction_type: string; reference: string; group: string }) =>
          r.prediction_type === target.target &&
          r.reference === "catboost_bounded" &&
          r.group === ablation.group,
      );
      assert.ok(
        Math.abs(
          ablation.delta - actual.mae_delta_by_fold.reduce((n: number, x: number) => n + x, 0) / 4,
        ) < 1e-12,
      );
      assert.equal(actual.identical_populations, true);
    }
  }
});
