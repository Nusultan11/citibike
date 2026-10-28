from pathlib import Path
from typing import Any

import httpx
import pytest

from citibike.ingestion.archive import download
from citibike.ingestion.inventory import (
    discover,
    parse_page,
    select_window,
    shift_month,
)
from citibike.storage import read_json
from tests.fixtures import fixture_archive, ride, write_zip


def listing(keys: list[str], truncated: bool = False) -> bytes:
    contents = "".join(
        f'<Contents><Key>{key}</Key><Size>100</Size><ETag>"etag"</ETag>'
        "<LastModified>2026-10-05T12:00:00Z</LastModified></Contents>"
        for key in keys
    )
    return (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{contents}</ListBucketResult>"
    ).encode()


def test_inventory_pagination_scope_completeness(tmp_path: Path) -> None:
    periods = [shift_month("2026-09", i) for i in range(-23, 1)]
    keys = [f"{p.replace('-', '')}-citibike-tripdata.zip" for p in periods]
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        payload = (
            listing(keys[:12], True)
            if len(calls) == 1
            else listing(
                keys[12:]
                + ["JC-202609-citibike-tripdata.csv.zip", "2023-citibike-tripdata.zip"]
            )
        )
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        inventory = read_json(discover(tmp_path, client))
    assert inventory["window"] == periods
    assert len(calls) == 2 and "marker=" in calls[1]
    assert len(inventory["objects"]) == 26
    assert inventory["objects"][-2]["scope"] == "JC"
    assert inventory["objects"][-1]["packaging"] == "annual_zip"
    assert all(o["checksum"] is None for o in inventory["objects"])
    with pytest.raises(ValueError, match="Incomplete"):
        select_window(inventory["objects"][1:])


def test_unrecognized_or_ambiguous_inventory_does_not_choose_silently() -> None:
    objects, _ = parse_page(
        listing(["202609-citibike-tripdata.zip", "202609-citibike-tripdata.csv.zip"])
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        select_window(objects, 1)


def test_reissued_source_retains_both_immutable_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = fixture_archive(tmp_path / "first.zip").read_bytes()
    second = write_zip(tmp_path / "second.zip", {"one.csv": [ride(99)]}).read_bytes()
    current: dict[str, Any] = {"payload": first, "etag": '"first"'}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-Match"] == current["etag"]
        return httpx.Response(
            200, content=current["payload"], headers={"etag": str(current["etag"])}
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: original(transport=httpx.MockTransport(respond)),
    )
    url = "https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip"
    old = download(
        url, "2026-04", tmp_path, expected_etag='"first"', expected_bytes=len(first)
    )
    current.update(payload=second, etag='"second"')
    new = download(
        url, "2026-04", tmp_path, expected_etag='"second"', expected_bytes=len(second)
    )
    assert old != new
    assert (old / "source.zip").read_bytes() == first
    assert (new / "source.zip").read_bytes() == second
