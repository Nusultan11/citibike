"""HTTP smoke checks against running services, including API outage."""

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


def check_services(
    api_url: str,
    web_url: str,
    expected: str,
    data_state: str,
    expected_count: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> None:
    try:
        with urlopen(f"{api_url.rstrip('/')}/health", timeout=10) as response:
            api_ok = response.status == 200 and json.load(response) == {"status": "ok"}
    except (URLError, TimeoutError):
        api_ok = False
    assert api_ok == (expected == "ok"), f"API state differs from {expected}"

    params = {k: v for k, v in {"start_date": start, "end_date": end}.items() if v}
    query = "?" + urlencode(params) if params else ""
    business = None
    if api_ok:
        try:
            with urlopen(
                f"{api_url.rstrip('/')}/v1/kpi{query}", timeout=10
            ) as response:
                business = json.load(response)
        except HTTPError as error:
            assert error.code == (422 if data_state == "error" else 503)
            assert json.load(error)["error"]["code"] in {
                "validation_error",
                "invalid_filter",
                "database_unavailable",
            }
    with urlopen(web_url + query, timeout=20) as response:
        assert response.status == 200
        page = response.read().decode("utf-8")
    assert "BikeFlow Intelligence" in page
    assert f'data-api-status="{expected}"' in page, "Incorrect web API status"
    assert f'data-data-state="{data_state}"' in page, "Incorrect data state"
    if data_state == "ok":
        assert business is not None and business["status"] == "ok"
        value = business["completed_published_rides"]
        if expected_count is not None:
            assert value == expected_count
        assert f'data-completed-rides="{value}"' in page
        version = business["metadata"]["publication_id"]
        assert f'data-publication-id="{version}"' in page
        pinned = {**params, "publication_id": version}
        with urlopen(
            f"{api_url.rstrip('/')}/v1/timeseries?{urlencode(pinned)}", timeout=10
        ) as response:
            series = json.load(response)
        assert series["metadata"]["publication_id"] == version
        assert sum(p["completed_published_rides"] for p in series["points"]) == value
    else:
        assert "data-completed-rides=" not in page, (
            "Missing data must not show a fabricated number"
        )
        if data_state == "empty":
            assert business is not None and business["status"] == "empty"
            assert business["completed_published_rides"] is None
    with urlopen(f"{web_url.rstrip('/')}/health", timeout=10) as response:
        assert response.status == 200
        assert json.load(response) == {"status": "ok"}
    print(
        f"HTTP smoke passed: API={expected}; web=200; data={data_state}; "
        f"count={business['completed_published_rides'] if business else None}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:3000")
    parser.add_argument("--expect-api", choices=["ok", "unavailable"], default="ok")
    parser.add_argument(
        "--expect-data", choices=["ok", "empty", "unavailable", "error"], default="ok"
    )
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    check_services(
        args.api_url,
        args.web_url,
        args.expect_api,
        args.expect_data,
        args.expected_count,
        args.start_date,
        args.end_date,
    )


if __name__ == "__main__":
    main()
