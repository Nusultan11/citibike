"""Official, streamed monthly downloads; content-addressed immutable Bronze."""

import csv
import hashlib
import io
import re
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from citibike.contracts import NY


class CsvObject(BaseModel):
    path: str
    bytes: int
    crc32: str
    columns: list[str]


class SourceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    source_url: str
    scope: str = "NYC"
    period: str
    checksum: str
    bytes: int
    retrieved_at: datetime
    source_modified_at: str | None = None
    etag: str | None = None
    csv_files: list[CsvObject]
    zip_entries: list[dict[str, Any]]
    access_policy: str = "https://citibikenyc.com/data-sharing-policy"


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_zip(path: Path) -> tuple[list[CsvObject], list[dict[str, Any]]]:
    csv_files: list[CsvObject] = []
    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member names")
        if sum(i.file_size for i in archive.infolist()) > 25_000_000_000:
            raise ValueError("ZIP expanded size exceeds the F1 safety limit")
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename:
                raise ValueError("Unsafe ZIP member path")
            if item.flag_bits & 1:
                raise ValueError("Encrypted ZIP is unsupported")
            metadata = "__MACOSX" in name.parts or name.name.startswith("._")
            is_csv = (
                not item.is_dir() and name.suffix.lower() == ".csv" and not metadata
            )
            entries.append(
                {
                    "path": item.filename,
                    "bytes": item.file_size,
                    "compressed_bytes": item.compress_size,
                    "kind": "csv" if is_csv else "directory_or_metadata",
                }
            )
            if is_csv:
                with archive.open(item) as raw:
                    reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
                    columns = next(reader, None)
                if not columns or len(columns) != len(set(columns)):
                    raise ValueError("Missing or duplicate CSV column names")
                csv_files.append(
                    CsvObject(
                        path=item.filename,
                        bytes=item.file_size,
                        crc32=f"{item.CRC:08x}",
                        columns=columns,
                    )
                )
        if not csv_files:
            raise ValueError("Archive contains no CSV")
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC validation failed")
    return sorted(csv_files, key=lambda x: x.path), entries


def store_source(
    temporary: Path,
    root: Path,
    *,
    url: str,
    period: str,
    source: str,
    headers: httpx.Headers | None = None,
) -> Path:
    digest = checksum(temporary)
    destination = root / "bronze" / "NYC" / digest
    if destination.exists():
        existing = SourceManifest.model_validate_json(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        if checksum(destination / "source.zip") != digest or existing.period != period:
            raise ValueError("Existing immutable source is inconsistent")
        return destination
    csv_files, entries = inspect_zip(temporary)
    manifest = SourceManifest(
        source=source,
        source_url=url,
        period=period,
        checksum=digest,
        bytes=temporary.stat().st_size,
        retrieved_at=datetime.now(UTC),
        source_modified_at=headers.get("last-modified") if headers else None,
        etag=headers.get("etag") if headers else None,
        csv_files=csv_files,
        zip_entries=entries,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as working:
        staging = Path(working) / "object"
        staging.mkdir()
        (staging / "source.zip").write_bytes(b"")
        # Copy in bounded chunks; never rewrite an already published source.
        import shutil

        shutil.copyfile(temporary, staging / "source.zip")
        (staging / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        staging.rename(destination)
    return destination


def download(url: str, period: str, root: Path) -> Path:
    parts = urlsplit(url)
    expected = f"{period.replace('-', '')}-citibike-tripdata.zip"
    if (
        not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period)
        or parts.scheme != "https"
        or parts.netloc != "s3.amazonaws.com"
        or parts.path != f"/tripdata/{expected}"
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Only the official NYC monthly archive URL is accepted; JC is out of scope"
        )
    if period >= datetime.now(NY).strftime("%Y-%m"):
        raise ValueError("Current or future month is not a complete published period")
    # Reuse the already verified immutable object without another network download.
    for manifest_path in (root / "bronze" / "NYC").glob("*/manifest.json"):
        manifest = SourceManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if manifest.source_url == url and manifest.period == period:
            if checksum(manifest_path.parent / "source.zip") != manifest.checksum:
                raise ValueError("Bronze checksum changed")
            return manifest_path.parent
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as working:
        temporary = Path(working) / "download.part"
        with httpx.Client(timeout=httpx.Timeout(60, connect=15)) as client:
            for attempt in range(3):
                try:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        count = 0
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes(1024 * 1024):
                                count += len(chunk)
                                if count > 2_000_000_000:
                                    raise ValueError("Download exceeds F1 size limit")
                                output.write(chunk)
                        length = response.headers.get("content-length")
                        if count == 0 or (length is not None and count != int(length)):
                            raise ValueError("Empty or incomplete HTTP object")
                        return store_source(
                            temporary,
                            root,
                            url=url,
                            period=period,
                            source="Citi Bike Trip History (official NYC)",
                            headers=response.headers,
                        )
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code != 429
                        and exc.response.status_code < 500
                    ):
                        raise
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
    raise RuntimeError("Unreachable download state")


def ingest_fixture(path: Path, period: str, root: Path) -> Path:
    return store_source(
        path,
        root,
        url="fixture://controlled-ci",
        period=period,
        source="Controlled test fixture — not real Citi Bike data",
    )
