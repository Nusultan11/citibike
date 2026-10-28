from pathlib import Path
from typing import Any

import httpx
import pytest

from citibike.ingestion.ranged import download_ranges
from tests.fixtures import fixture_archive


def test_ranges_resume_and_reject_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = fixture_archive(tmp_path / "fixture.zip").read_bytes()
    item: dict[str, Any] = {
        "url": "https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip",
        "period": "2026-04",
        "etag": '"version"',
        "bytes": len(payload),
        "last_modified": "2026-05-04T12:00:00Z",
    }
    attempts: dict[int, int] = {}
    broken = True

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(
                200, headers={"etag": item["etag"], "content-length": str(len(payload))}
            )
        start, end = map(
            int, request.headers["Range"].removeprefix("bytes=").split("-")
        )
        attempts[start] = attempts.get(start, 0) + 1
        assert request.headers["If-Match"] == item["etag"]
        return httpx.Response(
            206,
            content=payload[start : end + 1],
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "etag": '"changed"' if broken and start == 128 else item["etag"],
            },
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: original(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ValueError, match="range/version"):
        download_ranges(item, tmp_path, chunk_bytes=128)
    assert not list((tmp_path / "bronze").rglob("manifest.json"))
    broken = False
    bronze = download_ranges(item, tmp_path, chunk_bytes=128)
    assert (bronze / "source.zip").read_bytes() == payload
    assert attempts[0] == 1 and attempts[128] == 2
    assert download_ranges(item, tmp_path) == bronze
