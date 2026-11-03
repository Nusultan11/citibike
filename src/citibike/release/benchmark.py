"""Bounded loopback HTTP load. Warmup and distributions retain all observations."""

import concurrent.futures
import time
from typing import Any

import httpx


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        f"p{p}": ordered[min(len(ordered) - 1, (len(ordered) * p + 99) // 100 - 1)]
        for p in (50, 95, 99)
    }


def measure(url: str, requests: int, concurrency: int) -> dict[str, Any]:
    if not url.startswith("http://127.0.0.1:") or not 1 <= concurrency <= 32:
        raise ValueError("Only bounded loopback staging load is allowed")
    if not 1 <= requests <= 2000:
        raise ValueError("Request budget exceeded")
    with httpx.Client(
        timeout=30, trust_env=False, limits=httpx.Limits(max_connections=concurrency)
    ) as client:

        def one(_: int) -> tuple[float, int]:
            before = time.perf_counter()
            try:
                status = client.get(url).status_code
            except httpx.HTTPError:
                status = 0
            return (time.perf_counter() - before) * 1000, status

        cold = one(0)
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            observations = list(executor.map(one, range(requests)))
        seconds = time.perf_counter() - started
    timings = [ms for ms, _ in observations]
    return {
        "url": url,
        "requests": requests,
        "concurrency": concurrency,
        "first_request_ms": cold[0],
        "first_status": cold[1],
        "cache": "first request vs repeated; OS/DB page cache NOT flushed",
        "latency_ms": distribution(timings),
        "seconds": seconds,
        "requests_per_second": requests / seconds,
        "errors": sum(status != 200 for _, status in observations),
        "statuses": {
            str(s): sum(code == s for _, code in observations)
            for s in sorted({s for _, s in observations})
        },
        "observations_ms": timings,
    }
