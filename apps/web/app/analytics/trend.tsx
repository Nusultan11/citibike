"use client";

import { LineChart } from "echarts/charts";
import { AriaComponent, GridComponent, TooltipComponent } from "echarts/components";
import { init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";
import type { AnalyticsRow } from "../../lib/analytics";

use([LineChart, AriaComponent, GridComponent, TooltipComponent, CanvasRenderer]);

export default function AnalyticsTrend({ rows }: { rows: AnalyticsRow[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = init(ref.current);
    chart.setOption({
      animation: false,
      aria: {
        enabled: true,
        description:
          "Допустимые поездки по времени начала. Часы UTC различают DST; дни и месяцы America/New_York. Пропуски не означают ноль.",
      },
      grid: { left: 65, right: 20, top: 25, bottom: 65 },
      tooltip: { trigger: "axis", renderMode: "richText" },
      xAxis: {
        type: "category",
        data: rows.map((r) => r.key),
        axisLabel: { hideOverlap: true, rotate: 25 },
      },
      yAxis: { type: "value", min: 0 },
      series: [
        {
          type: "line",
          data: rows.map((r) => r.metrics?.completed_published_rides ?? null),
          connectNulls: false,
          showSymbol: true,
          symbolSize: 3,
          lineStyle: { color: "#16725c", width: 2 },
        },
      ],
    });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(ref.current);
    return () => {
      resize.disconnect();
      chart.dispose();
    };
  }, [rows]);
  return (
    <div
      ref={ref}
      style={{ height: 310, width: "100%" }}
      role="img"
      aria-label="Динамика допустимых поездок. Часовые подписи UTC, календарные дни и месяцы Нью-Йорка."
    />
  );
}
