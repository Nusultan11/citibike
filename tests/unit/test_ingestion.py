import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from citibike.contracts import NY
from citibike.ingestion import archive
from tests.fixtures import fixture_archive

URL = "https://s3.amazonaws.com/tripdata/202604-citibike-tripdata.zip"


def mock_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)


def test_stream_retry_and_immutable_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = fixture_archive(tmp_path / "source.zip").read_bytes()
    calls = []
    sleeps: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("Injected network timeout", request=request)
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    mock_client(monkeypatch, respond)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    result = archive.download(URL, "2026-04", tmp_path / "data")
    assert sleeps == [1, 2] and len(calls) == 3
    assert (result / "source.zip").read_bytes() == payload
    assert archive.download(URL, "2026-04", tmp_path / "data") == result
    assert len(calls) == 3


@pytest.mark.parametrize("status,expected_calls", [(404, 1), (429, 3), (500, 3)])
def test_http_failure_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_calls: int,
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    mock_client(monkeypatch, respond)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    with pytest.raises(httpx.HTTPStatusError):
        archive.download(URL, "2026-04", tmp_path / "data")
    assert len(calls) == expected_calls
    assert not list((tmp_path / "data").rglob("manifest.json"))
    assert not list((tmp_path / "data").rglob("*.part"))


@pytest.mark.parametrize("payload,length", [(b"", "0"), (b"truncated", "999")])
def test_incomplete_object_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, length: str
) -> None:
    mock_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200, content=payload, headers={"content-length": length}
        ),
    )
    with pytest.raises(ValueError, match="incomplete"):
        archive.download(URL, "2026-04", tmp_path / "data")
    assert not list((tmp_path / "data").rglob("manifest.json"))


def test_zip_paths_and_current_month_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as zipped:
        zipped.writestr("../escape.csv", "x\ny\n")
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        archive.ingest_fixture(path, "2026-04", tmp_path / "data")
    period = datetime.now(NY).strftime("%Y-%m")
    month = period.replace("-", "")
    url = f"https://s3.amazonaws.com/tripdata/{month}-citibike-tripdata.zip"
    with pytest.raises(ValueError, match="not a complete"):
        archive.download(url, period, tmp_path / "data")
