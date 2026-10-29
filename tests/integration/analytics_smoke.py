"""Published F3 HTTP/HTML contract and fixture E2E smoke."""

import argparse
import re
from pathlib import Path

import httpx

from citibike.serving.analytics_publish import publish
from tests.fixtures_analytics import analytical_gold


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".cache/f3-smoke"))
    args = parser.parse_args()
    version = publish(analytical_gold(args.data_dir))
    with httpx.Client(timeout=30) as client:
        params = {"publication_id": version}
        kpi = client.get("http://127.0.0.1:8000/v2/analytics/kpi", params=params)
        kpi.raise_for_status()
        assert kpi.json()["metrics"]["completed_published_rides"] == 5
        html = client.get("http://127.0.0.1:3000/analytics", params=params)
        html.raise_for_status()
        match = re.search(r'data-analytics-completed-rides="(\d+)"', html.text)
        assert match is not None and int(match[1]) == 5 and version in html.text
        filtered = client.get(
            "http://127.0.0.1:3000/analytics",
            params={
                **params,
                "start_date": "2024-10-01",
                "end_date": "2024-11-01",
                "station_id": "001",
            },
        )
        assert (
            filtered.status_code == 200
            and 'data-analytics-completed-rides="1"' in filtered.text
        )
        invalid = client.get("http://127.0.0.1:3000/analytics?vehicle=invalid")
        assert (
            invalid.status_code == 200
            and 'data-analytics-state="error"' in invalid.text
        )
        missing = client.get("http://127.0.0.1:3000/analytics?station_id=missing")
        assert (
            missing.status_code == 200
            and 'data-analytics-state="empty"' in missing.text
        )
        partial = client.get(
            "http://127.0.0.1:3000/analytics",
            params={**params, "start_date": "2024-10-02", "end_date": "2025-01-02"},
        )
        assert partial.status_code == 200
        assert 'data-analytics-completed-rides="3"' in partial.text
        assert 'data-route-state="error"' in partial.text
    print(
        {
            "publication_id": version,
            "http_html_filters_empty_error": "passed",
            "expected_completed_rides": 5,
        }
    )


if __name__ == "__main__":
    main()
