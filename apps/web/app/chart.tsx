"use client";

import { BarChart, HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

use([
  BarChart,
  HeatmapChart,
  LineChart,
  ScatterChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

// Validated categorical order (blue, orange, aqua); identity is never color alone:
// legends, direct table views and labels accompany every chart.
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a"];
const DIVERGING = ["#e34948", "#b5b3ac", "#2a78d6"];
const AXIS = { axisLine: { lineStyle: { color: "#9aa8a2" } }, axisLabel: { color: "#526b65" } };
const GRID_LINE = { splitLine: { lineStyle: { color: "#e3ece3" } } };

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
  | { kind: "line" | "bar"; categories: string[]; series: Series[]; unit: string }
  | { kind: "heatmap"; x: string[]; y: string[]; cells: [number, number, number][]; unit: string }
  | { kind: "map"; points: MapPoint[]; href: string; selected?: string | null };

function option(spec: ChartSpec, width: number, height: number): Record<string, unknown> {
  const base = { animation: false, aria: { enabled: true }, textStyle: { fontFamily: "Arial" } };
  if (spec.kind === "line" || spec.kind === "bar") {
    return {
      ...base,
      color: SERIES,
      legend: spec.series.length > 1 ? { top: 0, textStyle: { color: "#163e38" } } : undefined,
      grid: { left: 62, right: 16, top: spec.series.length > 1 ? 36 : 16, bottom: 58 },
      tooltip: { trigger: "axis", renderMode: "richText", valueFormatter: format(spec.unit) },
      xAxis: {
        type: "category",
        data: spec.categories,
        ...AXIS,
        axisLabel: { ...AXIS.axisLabel, hideOverlap: true, rotate: 25 },
      },
      yAxis: { type: "value", min: 0, ...AXIS, ...GRID_LINE },
      dataZoom: spec.categories.length > 40 ? [{ type: "inside" }] : undefined,
      series: spec.series.map((s, i) =>
        spec.kind === "line"
          ? {
              type: "line",
              name: s.name,
              data: s.values,
              connectNulls: false,
              showSymbol: spec.categories.length <= 40,
              symbolSize: 8,
              lineStyle: { width: 2, color: SERIES[i] },
              itemStyle: { color: SERIES[i], borderColor: "#fff", borderWidth: 2 },
            }
          : {
              type: "bar",
              name: s.name,
              data: s.values,
              barGap: "12%",
              itemStyle: { color: SERIES[i], borderRadius: [4, 4, 0, 0] },
            },
      ),
    };
  }
  if (spec.kind === "heatmap") {
    const values = spec.cells.map((c) => c[2]);
    return {
      ...base,
      grid: { left: 52, right: 16, top: 10, bottom: 70 },
      tooltip: {
        renderMode: "richText",
        formatter: (p: { value: [number, number, number] }) =>
          `${spec.y[p.value[1]]} ${spec.x[p.value[0]]}: ${format(spec.unit)(p.value[2])}`,
      },
      xAxis: { type: "category", data: spec.x, ...AXIS, splitArea: { show: false } },
      yAxis: { type: "category", data: spec.y, ...AXIS },
      visualMap: {
        min: 0,
        max: Math.max(1, ...values),
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemHeight: 120,
        calculable: false,
        inRange: { color: ["#cde2fb", "#2a78d6", "#0d366b"] },
        textStyle: { color: "#526b65" },
      },
      series: [
        {
          type: "heatmap",
          data: spec.cells,
          itemStyle: { borderColor: "#fff", borderWidth: 2 },
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
  return {
    ...base,
    grid: { left: 52, right: 14, top: 10, bottom: 50 },
    tooltip: {
      renderMode: "richText",
      formatter: (p: { value: [number, number, number | null, string, string | null, boolean] }) =>
        `${p.value[3]} ${p.value[4] ?? ""}\nnet flow: ${
          p.value[2] === null ? "N/A" : p.value[2].toLocaleString("ru-RU")
        }${p.value[5] ? "\nвозможный ID-split" : ""}`,
    },
    xAxis: {
      type: "value",
      min: west,
      max: east,
      name: "lon",
      ...AXIS,
      axisLabel: {
        ...AXIS.axisLabel,
        formatter: degrees,
        showMinLabel: false,
        showMaxLabel: false,
      },
      ...GRID_LINE,
    },
    yAxis: {
      type: "value",
      min: south,
      max: north,
      name: "lat",
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
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemHeight: 140,
      text: ["прибытий больше", "отправлений больше"],
      inRange: { color: DIVERGING },
      outOfRange: { color: "#b5b3ac" },
      textStyle: { color: "#526b65" },
      seriesIndex: 0,
    },
    series: [
      {
        type: "scatter",
        name: "Станции",
        data: map.points.filter((p) => !p.flagged && p.id !== map.selected).map(row),
        symbolSize: 8,
        itemStyle: { borderColor: "#fff", borderWidth: 1 },
      },
      {
        type: "scatter",
        name: "Возможный ID-split",
        data: map.points.filter((p) => p.flagged && p.id !== map.selected).map(row),
        symbol: "diamond",
        symbolSize: 10,
        itemStyle: { color: "transparent", borderColor: "#163e38", borderWidth: 1.5 },
      },
      {
        type: "scatter",
        name: "Выбранная станция",
        data: map.points.filter((p) => p.id === map.selected).map(row),
        symbolSize: 16,
        itemStyle: { color: "#eb6834", borderColor: "#163e38", borderWidth: 2 },
        z: 5,
      },
    ],
  };
}

const degrees = (value: number) => `${value.toFixed(2)}°`;

function format(unit: string) {
  return (value: unknown) =>
    typeof value === "number"
      ? `${value.toLocaleString("ru-RU")} ${unit}`.trim()
      : "Нет наблюдений";
}

export default function Chart({ spec, label }: { spec: ChartSpec; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const chart = init(element);
    const render = () =>
      chart.setOption(option(spec, element.clientWidth, element.clientHeight), true);
    render();
    if (spec.kind === "map")
      chart.on("click", (event) => {
        const value = event.value as unknown[];
        if (Array.isArray(value) && typeof value[3] === "string")
          window.location.assign(spec.href + encodeURIComponent(value[3]));
      });
    const observer = new ResizeObserver(() => {
      chart.resize();
      if (spec.kind === "map") render();
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [spec]);
  return (
    <div
      ref={ref}
      className={spec.kind === "map" ? "chart chart-map" : "chart"}
      role="img"
      aria-label={label}
      data-chart={spec.kind}
    />
  );
}
