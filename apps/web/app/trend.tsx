"use client";

import { LineChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";
import type { AppliedFilters, HourPoint } from "../lib/data";

use([LineChart, GridComponent, TooltipComponent, DataZoomComponent, AriaComponent, CanvasRenderer]);

export default function Trend({
  points,
  filters,
}: {
  points: HourPoint[];
  filters: AppliedFilters;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = init(ref.current);
    const values = new Map(
      points.map((p) => [Date.parse(p.hour_utc), p.completed_published_rides]),
    );
    const data: [number, number | null][] = [];
    for (let t = Date.parse(filters.start); t < Date.parse(filters.end); t += 3600000)
      data.push([t, values.get(t) ?? null]);
    const date = new Intl.DateTimeFormat("ru-RU", {
      timeZone: "America/New_York",
      month: "short",
      day: "numeric",
      hour: "2-digit",
    });
    const detail = new Intl.DateTimeFormat("ru-RU", {
      timeZone: "America/New_York",
      dateStyle: "medium",
      timeStyle: "long",
    });
    chart.setOption({
      useUTC: true,
      animation: false,
      aria: {
        enabled: true,
        description:
          "Число допустимых опубликованных поездок по часу начала. Не показатель текущей доступности велосипедов.",
      },
      grid: { left: 55, right: 15, top: 20, bottom: 65 },
      xAxis: {
        type: "time",
        axisLabel: {
          formatter: (value: number) => date.format(new Date(value)),
          hideOverlap: true,
        },
      },
      yAxis: { type: "value", min: 0 },
      tooltip: {
        trigger: "axis",
        formatter: (params: unknown) => {
          const point = Array.isArray(params) ? params[0] : params;
          if (!point || typeof point !== "object" || !("value" in point)) return "";
          const value: unknown = point.value;
          if (!Array.isArray(value) || typeof value[0] !== "number") return "";
          const rides =
            typeof value[1] === "number" ? value[1].toLocaleString("ru-RU") : "Нет наблюдений";
          return `${detail.format(value[0])} (America/New_York)<br/>Поездки: ${rides}`;
        },
      },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 5 }],
      series: [
        {
          type: "line",
          name: "Опубликованные поездки",
          data,
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: "#16725c", width: 2 },
          itemStyle: { color: "#16725c" },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [points, filters]);
  return (
    <div
      className="trend"
      ref={ref}
      role="img"
      aria-label="Почасовой тренд опубликованных поездок; численные значения доступны в таблице ниже"
    />
  );
}
