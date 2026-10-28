"""HTTP smoke checks against running services, including API outage."""

import argparse
import json
from urllib.error import URLError
from urllib.request import urlopen


def check_services(api_url: str, web_url: str, expected: str) -> None:
    try:
        with urlopen(f"{api_url.rstrip('/')}/health", timeout=10) as response:
            api_ok = response.status == 200 and json.load(response) == {"status": "ok"}
    except (URLError, TimeoutError):
        api_ok = False
    assert api_ok == (expected == "ok"), f"API state differs from {expected}"

    with urlopen(web_url, timeout=15) as response:
        assert response.status == 200
        page = response.read().decode("utf-8")
    assert "BikeFlow Intelligence" in page
    assert "Данные ещё не загружены" in page
    assert f'data-api-status="{expected}"' in page, "Incorrect web API status"
    with urlopen(f"{web_url.rstrip('/')}/health", timeout=10) as response:
        assert response.status == 200
        assert json.load(response) == {"status": "ok"}
    print(f"HTTP smoke passed: API={expected}; web=200; business data unavailable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:3000")
    parser.add_argument("--expect-api", choices=["ok", "unavailable"], default="ok")
    args = parser.parse_args()
    check_services(args.api_url, args.web_url, args.expect_api)


if __name__ == "__main__":
    main()
