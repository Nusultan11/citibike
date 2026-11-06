"use client";

import { useEffect, useRef } from "react";

/** Read-only browser evidence. A hydration + paint upper bound, not HTTP latency.
 * Full navigations only: performance.now() is relative to navigationStart.
 */
export default function RenderTiming() {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        const element = ref.current;
        if (!element) return;
        element.dataset.firstScreenMs = performance.now().toFixed(1);
        element.dataset.visibility = document.visibilityState;
        const fcp = performance.getEntriesByName("first-contentful-paint")[0];
        if (fcp) element.dataset.fcpMs = fcp.startTime.toFixed(1);
      });
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, []);
  return <span ref={ref} hidden data-render-timing="navigation" />;
}
