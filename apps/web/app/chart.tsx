"use client";

import { BarChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkPointComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef, useState } from "react";
import { drillHref } from "../lib/chart-navigation";

use([
  BarChart,
  HeatmapChart,
  LineChart,
  ScatterChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkPointComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

// Canvas cannot read CSS variables: these mirror the tokens in globals.css.
// Categorical order (blue, orange, aqua) validated for the dark surface #141821
// (CVD ΔE ≥ 9.4, ≥ 3:1 contrast); identity is never colour alone — legends,
// direct table views and labels accompany every chart.
export const SERIES = ["#3987e5", "#d95926", "#199e70"];
const DIVERGING = ["#f2706b", "#7a8294", "#5b9cf0"];
const SEQUENTIAL = ["#18243a", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"];
const INK = { primary: "#eef1f6", secondary: "#b6bdcb", muted: "#8b93a5" };
const AXIS = {
  axisLine: { lineStyle: { color: "#323947" } },
  axisTick: { show: false },
  axisLabel: { color: INK.muted, fontSize: 11 },
};
const GRID_LINE = { splitLine: { lineStyle: { color: "#232937" } } };
const TOOLTIP = {
  renderMode: "richText",
  backgroundColor: "#232a39",
  borderColor: "rgba(255,255,255,0.14)",
  borderWidth: 1,
  padding: [8, 10],
  textStyle: { color: INK.primary, fontSize: 12 },
};

export type Series = { name: string; values: (number | null)[] };
export type MapPoint = {
  id: string;
  name: string | null;
  lat: number;
  lon: number;
  value: number | null;
  flagged: boolean;
};
export type ChartSpec =
  | {
      kind: "line" | "bar";
      categories: string[];
      series: Series[];
      unit: string;
      /** Optional per-category drill-down targets (same length as categories). */
      links?: (string | null)[];
      /** Compact drill-down: categories are NY day / month keys; URLs are built here. */
      drill?: { base: string; to: "hour" | "day" };
      /** Mark the maximum of the first series. */
      peak?: boolean;
    }
  | { kind: "heatmap"; x: string[]; y: string[]; cells: [number, number, number][]; unit: string }
  | { kind: "map"; points: MapPoint[]; href: string; selected?: string | null };

function option(spec: ChartSpec, width: number, height: number): Record<string, unknown> {
  const base = {
    animation: false,
    aria: { enabled: false },
    backgroundColor: "transparent",
    textStyle: { fontFamily: "Inter, Segoe UI, system-ui, sans-serif", color: INK.secondary },
  };
  if (spec.kind === "line" || spec.kind === "bar") {
    const many = spec.series.length > 1;
    const narrow = width < 480;
    return {
      ...base,
      color: SERIES,
      legend: many
        ? {
            top: 0,
            left: 0,
            icon: spec.kind === "line" ? "roundRect" : "rect",
            itemWidth: 14,
            itemHeight: spec.kind === "line" ? 4 : 10,
            textStyle: { color: INK.secondary, fontSize: 12 },
          }
        : undefined,
      grid: {
        left: narrow ? 44 : 56,
        right: 12,
        top: many ? 40 : 18,
        bottom: spec.categories.length > 40 ? 64 : 44,
      },
      tooltip: {
        ...TOOLTIP,
        trigger: "axis",
        axisPointer: {
          type: spec.kind === "bar" ? "shadow" : "line",
          lineStyle: { color: "#4a5263" },
        },
        valueFormatter: format(spec.unit),
      },
      xAxis: {
        type: "category",
        data: spec.categories,
        boundaryGap: spec.kind === "bar",
        ...AXIS,
        axisLabel: { ...AXIS.axisLabel, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        min: 0,
        ...AXIS,
        ...GRID_LINE,
        axisLabel: { ...AXIS.axisLabel, formatter: compact },
      },
      dataZoom:
        spec.categories.length > 40
          ? [
              { type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false },
              {
                type: "slider",
                height: 16,
                bottom: 6,
                borderColor: "transparent",
                backgroundColor: "#1a1f2a",
                fillerColor: "rgba(91,141,239,0.18)",
                handleStyle: { color: "#8db1ff" },
                dataBackground: {
                  lineStyle: { color: "#3a4252" },
                  areaStyle: { color: "#232937" },
                },
                textStyle: { color: INK.muted },
              },
            ]
          : undefined,
      series: spec.series.map((s, i) =>
        spec.kind === "line"
          ? {
              type: "line",
              name: s.name,
              data: s.values,
              connectNulls: false,
              showSymbol: spec.categories.length <= 31,
              symbol: ["circle", "rect", "triangle"][i] ?? "circle",
              symbolSize: 7,
              lineStyle: { width: 2, color: SERIES[i], type: i === 1 && many ? "dashed" : "solid" },
              itemStyle: { color: SERIES[i], borderColor: "#141821", borderWidth: 2 },
              emphasis: { focus: "series" },
              areaStyle:
                !many && i === 0
                  ? {
                      color: {
                        type: "linear",
                        x: 0,
                        y: 0,
                        x2: 0,
                        y2: 1,
                        colorStops: [
                          { offset: 0, color: "rgba(57,135,229,0.28)" },
                          { offset: 1, color: "rgba(57,135,229,0)" },
                        ],
                      },
                    }
                  : undefined,
              markPoint: spec.peak && i === 0 ? peakMark(spec.unit) : undefined,
              cursor: spec.links || spec.drill ? "pointer" : "default",
            }
          : {
              type: "bar",
              name: s.name,
              data: s.values,
              barGap: "18%",
              barMaxWidth: 34,
              itemStyle: { color: SERIES[i], borderRadius: [4, 4, 0, 0] },
              emphasis: { focus: "series" },
              markPoint: spec.peak && i === 0 ? peakMark(spec.unit) : undefined,
              cursor: spec.links || spec.drill ? "pointer" : "default",
            },
      ),
    };
  }
  if (spec.kind === "heatmap") {
    const values = spec.cells.map((c) => c[2]);
    return {
      ...base,
      grid: { left: 40, right: 8, top: 8, bottom: 62 },
      tooltip: {
        ...TOOLTIP,
        formatter: (p: { value: [number, number, number] }) =>
          `${spec.y[p.value[1]]}, ${spec.x[p.value[0]]}:00 NY\n${format(spec.unit)(p.value[2])}`,
      },
      xAxis: {
        type: "category",
        data: spec.x,
        ...AXIS,
        axisLabel: { ...AXIS.axisLabel, interval: width < 520 ? 3 : 1 },
        splitArea: { show: false },
      },
      yAxis: { type: "category", data: spec.y, inverse: true, ...AXIS },
      visualMap: {
        min: 0,
        max: Math.max(1, ...values),
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemHeight: 140,
        itemWidth: 10,
        calculable: false,
        text: ["больше", "меньше"],
        inRange: { color: SEQUENTIAL },
        textStyle: { color: INK.muted, fontSize: 11 },
      },
      series: [
        {
          type: "heatmap",
          data: spec.cells,
          itemStyle: { borderColor: "#10131a", borderWidth: 2, borderRadius: 3 },
          emphasis: { itemStyle: { borderColor: INK.primary, borderWidth: 1 } },
        },
      ],
    };
  }
  const map = spec as Extract<ChartSpec, { kind: "map" }>;
  // Equirectangular view with cos(latitude) aspect so distances are not stretched.
  const lats = map.points.map((p) => p.lat);
  const lons = map.points.map((p) => p.lon);
  let [south, north] = [Math.min(...lats), Math.max(...lats)];
  let [west, east] = [Math.min(...lons), Math.max(...lons)];
  const k = Math.cos((((south + north) / 2) * Math.PI) / 180);
  const plotW = Math.max(width - 70, 50);
  const plotH = Math.max(height - 60, 50);
  const ratio = plotW / plotH;
  const spanLat = Math.max(north - south, 0.005) * 1.05;
  const spanLon = Math.max(east - west, 0.005) * 1.05;
  const needLon = (spanLat * ratio) / k;
  const finalLon = Math.max(spanLon, needLon);
  const finalLat = Math.max(spanLat, (finalLon * k) / ratio);
  const cLat = (south + north) / 2;
  const cLon = (west + east) / 2;
  [south, north] = [cLat - finalLat / 2, cLat + finalLat / 2];
  [west, east] = [cLon - finalLon / 2, cLon + finalLon / 2];
  const flows = map.points.map((p) => Math.abs(p.value ?? 0)).sort((a, b) => a - b);
  const limit = Math.max(1, flows[Math.floor(flows.length * 0.95)] ?? 1);
  const row = (p: MapPoint) => [p.lon, p.lat, p.value, p.id, p.name, p.flagged];
  const zoom = { type: "inside", filterMode: "none", zoomOnMouseWheel: "ctrl" };
  return {
    ...base,
    grid: { left: 48, right: 12, top: 10, bottom: 48 },
    tooltip: {
      ...TOOLTIP,
      formatter: (p: { value: [number, number, number | null, string, string | null, boolean] }) =>
        `${p.value[3]} · ${p.value[4] ?? "название N/A"}\nnet flow: ${
          p.value[2] === null ? "N/A" : p.value[2].toLocaleString("ru-RU")
        }${p.value[5] ? "\n◇ возможный ID-split" : ""}\nнажмите, чтобы открыть карточку`,
    },
    dataZoom: [
      { ...zoom, xAxisIndex: 0 },
      { ...zoom, yAxisIndex: 0 },
    ],
    xAxis: {
      type: "value",
      min: west,
      max: east,
      splitNumber: width < 480 ? 2 : 5,
      ...AXIS,
      axisLabel: {
        ...AXIS.axisLabel,
        formatter: degrees,
        hideOverlap: true,
        showMinLabel: false,
        showMaxLabel: false,
      },
      ...GRID_LINE,
    },
    yAxis: {
      type: "value",
      min: south,
      max: north,
      ...AXIS,
      axisLabel: {
        ...AXIS.axisLabel,
        formatter: degrees,
        showMinLabel: false,
        showMaxLabel: false,
      },
      ...GRID_LINE,
    },
    visualMap: {
      type: "continuous",
      dimension: 2,
      min: -limit,
      max: limit,
      show: false,
      inRange: { color: DIVERGING },
      outOfRange: { color: "#7a8294" },
      seriesIndex: 0,
    },
    series: [
      {
        type: "scatter",
        name: "Станции",
        data: map.points.filter((p) => !p.flagged && p.id !== map.selected).map(row),
        symbolSize: width < 480 ? 6 : 7,
        itemStyle: { borderColor: "#0b0e14", borderWidth: 0.6, opacity: 0.92 },
        emphasis: { scale: 1.8, itemStyle: { borderColor: INK.primary, borderWidth: 1.5 } },
        cursor: "pointer",
      },
      {
        type: "scatter",
        name: "Возможный ID-split",
        data: map.points.filter((p) => p.flagged && p.id !== map.selected).map(row),
        symbol: "diamond",
        symbolSize: 11,
        itemStyle: { color: "transparent", borderColor: "#e5b84a", borderWidth: 1.5 },
        emphasis: { scale: 1.6 },
        cursor: "pointer",
      },
      {
        type: "scatter",
        name: "Выбранная станция",
        data: map.points.filter((p) => p.id === map.selected).map(row),
        symbolSize: 18,
        itemStyle: { color: SERIES[1], borderColor: INK.primary, borderWidth: 2.5 },
        z: 5,
      },
    ],
  };
}

const degrees = (value: number) => `${value.toFixed(2)}°`;
const compactFormat = new Intl.NumberFormat("ru-RU", { notation: "compact" });
const compact = (value: number) => compactFormat.format(value);

function peakMark(unit: string) {
  return {
    symbol: "pin",
    symbolSize: 38,
    data: [{ type: "max", name: "пик" }],
    itemStyle: { color: "#2b3446", borderColor: "#8db1ff", borderWidth: 1 },
    label: {
      color: INK.primary,
      fontSize: 10,
      formatter: (p: { value: number }) => compact(p.value),
    },
    tooltip: { valueFormatter: format(unit) },
  };
}

function format(unit: string) {
  return (value: unknown) =>
    typeof value === "number"
      ? `${value.toLocaleString("ru-RU")} ${unit}`.trim()
      : "Нет наблюдений";
}

export default function Chart({
  spec,
  label,
  size,
}: {
  spec: ChartSpec;
  label: string;
  size?: "tall" | "short" | "heat";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [inspect, setInspect] = useState(false);
  const [index, setIndex] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const chart = init(element, undefined, { renderer: "canvas" });
    const render = () =>
      chart.setOption(option(spec, element.clientWidth, element.clientHeight), true);
    render();
    let painted = 0;
    const frame = requestAnimationFrame(() => {
      painted = requestAnimationFrame(() => {
        element.dataset.readyMs = performance.now().toFixed(1);
        element.dataset.visibility = document.visibilityState;
      });
    });
    if (spec.kind === "map")
      chart.on("click", (event) => {
        const value = event.value as unknown[];
        if (Array.isArray(value) && typeof value[3] === "string")
          window.location.assign(spec.href + encodeURIComponent(value[3]));
      });
    else if ((spec.kind === "line" || spec.kind === "bar") && (spec.links || spec.drill)) {
      const { links, drill, categories } = spec;
      const go = (index: number) => {
        const target = links ? links[index] : drill ? drillHref(drill, categories[index]) : null;
        if (target) window.location.assign(target);
      };
      chart.on("click", (event) => {
        if (typeof event.dataIndex === "number" && event.componentType === "series")
          go(event.dataIndex);
      });
      // Clicking anywhere in the plot column drills into that category, not only on the mark.
      chart.getZr().on("click", (event) => {
        if (event.target) return;
        const point = [event.offsetX, event.offsetY];
        if (!chart.containPixel("grid", point)) return;
        const index = chart.convertFromPixel({ seriesIndex: 0 }, point)?.[0];
        if (typeof index === "number") go(Math.round(index));
      });
    }
    const observer = new ResizeObserver(() => {
      chart.resize();
      if (spec.kind === "map" || spec.kind === "heatmap") render();
    });
    observer.observe(element);
    return () => {
      cancelAnimationFrame(frame);
      cancelAnimationFrame(painted);
      observer.disconnect();
      chart.dispose();
    };
  }, [spec]);
  const className =
    spec.kind === "map" ? "chart chart-map" : `chart${size ? ` chart-${size}` : ""}`;
  return (
    <>
      <div
        ref={ref}
        className={className}
        role="img"
        aria-label={label}
        data-chart={spec.kind}
        data-drilldown={
          (spec.kind === "line" || spec.kind === "bar") && (spec.links || spec.drill)
            ? "category"
            : undefined
        }
      />
      <details className="chart-access" onToggle={(event) => setInspect(event.currentTarget.open)}>
        <summary>Данные и переходы с клавиатуры</summary>
        {inspect && <ChartInspector spec={spec} label={label} index={index} onIndex={setIndex} />}
      </details>
    </>
  );
}

function ChartInspector({
  spec,
  label,
  index,
  onIndex,
}: {
  spec: ChartSpec;
  label: string;
  index: number;
  onIndex: (value: number) => void;
}) {
  const categories =
    spec.kind === "map"
      ? spec.points.map((p) => `${p.id} · ${p.name ?? "название N/A"}`)
      : spec.kind === "heatmap"
        ? spec.cells.map(([x, y]) => `${spec.y[y]}, ${spec.x[x]}:00 NY`)
        : spec.categories;
  const current = Math.min(index, Math.max(0, categories.length - 1));
  const target =
    spec.kind === "map" && spec.points[current]
      ? spec.href + encodeURIComponent(spec.points[current].id)
      : spec.kind === "line" || spec.kind === "bar"
        ? (spec.links?.[current] ??
          (spec.drill ? drillHref(spec.drill, categories[current]) : null))
        : null;
  return (
    <fieldset className="stack chart-controls">
      <legend>{label}</legend>
      <label className="field">
        {spec.kind === "map" ? "Станция на карте" : "Точка графика"}
        <select value={current} onChange={(event) => onIndex(Number(event.target.value))}>
          {categories.map((category, i) => (
            <option key={category} value={i}>
              {category}
            </option>
          ))}
        </select>
      </label>
      <div aria-live="polite" className="chart-values">
        {(spec.kind === "line" || spec.kind === "bar") &&
          spec.series.map((s) => (
            <p key={s.name}>
              {s.name}: {format(spec.unit)(s.values[current])}
            </p>
          ))}
        {spec.kind === "heatmap" && <p>{format(spec.unit)(spec.cells[current]?.[2])}</p>}
        {spec.kind === "map" && (
          <p>
            net flow: {format("")(spec.points[current]?.value)}
            {spec.points[current]?.flagged ? " · возможный ID-split" : ""}
          </p>
        )}
      </div>
      {target && (
        <a className="btn btn-secondary" href={target}>
          {spec.kind === "map" ? "Открыть станцию" : "Открыть подробный период"}
        </a>
      )}
      <button className="btn btn-ghost" type="button" onClick={() => onIndex(0)}>
        К первой точке
      </button>
    </fieldset>
  );
}
