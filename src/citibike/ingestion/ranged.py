"""Resumable bounded HTTP ranges of a pinned official S3 object version."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from citibike.ingestion.archive import checksum, store_source
from citibike.storage import identity, read_json, write_json


def download_ranges(
    item: dict[str, Any], root: Path, *, chunk_bytes: int = 16 * 1024 * 1024
) -> Path:
    # The caller selects only an exact key from the complete official inventory.
    if (
        item["url"]
        != "https://s3.amazonaws.com/tripdata/"
        + item["period"].replace("-", "")
        + "-citibike-tripdata.zip"
        or not item["etag"]
        or type(item["bytes"]) is not int
        or not 0 < item["bytes"] <= 2_000_000_000
        or not 128 <= chunk_bytes <= 64 * 1024 * 1024
    ):
        raise ValueError("Pinned official object required")
    for path in (root / "bronze" / "NYC").glob("*/manifest.json"):
        manifest = read_json(path)
        if (
            manifest["source_url"] == item["url"]
            and manifest.get("etag") == item["etag"]
            and manifest["bytes"] == item["bytes"]
        ):
            if checksum(path.parent / "source.zip") != manifest["checksum"]:
                raise ValueError("Immutable Bronze changed")
            return path.parent
    cache = root / "downloads" / identity([item["url"], item["etag"], item["bytes"]])
    cache.mkdir(parents=True, exist_ok=True)
    step = chunk_bytes
    ranges = [
        (start, min(start + step, item["bytes"]) - 1)
        for start in range(0, item["bytes"], step)
    ]
    with httpx.Client(
        timeout=httpx.Timeout(60, connect=15), limits=httpx.Limits(max_connections=4)
    ) as client:
        # Resolve/connect in the caller thread before using the shared pool.
        # Some Windows network environments fail DNS resolution in worker threads.
        head = client.head(item["url"], headers={"If-Match": item["etag"]})
        head.raise_for_status()
        if (
            head.headers.get("etag") != item["etag"]
            or int(head.headers.get("content-length", "-1")) != item["bytes"]
        ):
            raise ValueError("S3 HEAD differs from pinned inventory")

        def part(bounds: tuple[int, int]) -> Path:
            start, end = bounds
            target = cache / f"{start}-{end}.part"
            receipt = target.with_suffix(".json")
            if (
                target.exists()
                and receipt.exists()
                and checksum(target) == read_json(receipt)["checksum"]
                and target.stat().st_size == end - start + 1
            ):
                return target
            for attempt in range(3):
                try:
                    with client.stream(
                        "GET",
                        item["url"],
                        headers={
                            "Range": f"bytes={start}-{end}",
                            "If-Match": item["etag"],
                        },
                    ) as response:
                        response.raise_for_status()
                        if (
                            response.status_code != 206
                            or response.headers.get("content-range")
                            != f"bytes {start}-{end}/{item['bytes']}"
                            or response.headers.get("etag") != item["etag"]
                        ):
                            raise ValueError("S3 range/version mismatch")
                        size = 0
                        with target.open("wb") as output:
                            for chunk in response.iter_bytes(65536):
                                size += len(chunk)
                                if size > end - start + 1:
                                    raise ValueError("Oversized range")
                                output.write(chunk)
                        if size != end - start + 1:
                            raise ValueError("Incomplete range")
                    write_json(
                        receipt,
                        {
                            "checksum": checksum(target),
                            "etag": item["etag"],
                            "start": start,
                            "end": end,
                        },
                    )
                    return target
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code < 500
                        and exc.response.status_code != 429
                    ):
                        raise
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
            raise RuntimeError("Unreachable range state")

        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                parts = list(executor.map(part, ranges))
        except httpx.ConnectError:
            print(
                f"Retrying remaining {item['period']} ranges sequentially", flush=True
            )
            # One bounded fallback; completed chunks are verified and reused.
            parts = [part(bounds) for bounds in ranges]
    temporary = cache / "source.zip.part"
    import shutil

    with temporary.open("wb") as output:
        for path in parts:
            with path.open("rb") as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
    if temporary.stat().st_size != item["bytes"]:
        raise ValueError("Assembled object byte count mismatch")
    destination = store_source(
        temporary,
        root,
        url=item["url"],
        period=item["period"],
        source="Citi Bike Trip History (official NYC)",
        headers=httpx.Headers(
            {"etag": item["etag"], "last-modified": item["last_modified"]}
        ),
    )
    # Remove only this downloader's individually named, verified temporary files.
    for path in [temporary, *parts, *(p.with_suffix(".json") for p in parts)]:
        if not path.resolve().is_relative_to(cache.resolve()):
            raise ValueError("Download cache cleanup escaped its owned directory")
        path.unlink(missing_ok=True)
    cache.rmdir()
    return destination
