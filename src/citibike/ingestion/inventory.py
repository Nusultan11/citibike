"""Official S3 ListObjects discovery, scope completeness and versioned Bronze."""

import argparse
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from citibike.contracts import NY
from citibike.ingestion.archive import SourceManifest, download
from citibike.ingestion.ranged import download_ranges
from citibike.storage import identity, read_json, write_json

LISTING = "https://s3.amazonaws.com/tripdata"
INVENTORY_VERSION = "f2-inventory-1"
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}


def shift_month(period: str, offset: int) -> str:
    year, month = map(int, period.split("-"))
    index = year * 12 + month - 1 + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def parse_page(content: bytes) -> tuple[list[dict[str, Any]], str | None]:
    root = ET.fromstring(content)
    objects: list[dict[str, Any]] = []
    for item in root.findall("s:Contents", NS):

        def field(name: str, element: ET.Element = item) -> str:
            return element.findtext(f"s:{name}", default="", namespaces=NS)

        key = field("Key")
        monthly = re.match(r"^(JC-)?(\d{4})(\d{2})[-_]", key)
        annual = re.match(r"^(JC-)?(\d{4})(?:-citibike|\.zip)", key)
        is_zip = key.lower().endswith(".zip")
        period = f"{monthly[2]}-{monthly[3]}" if monthly else None
        scope = "JC" if key.startswith("JC-") else "NYC"
        objects.append(
            {
                "key": key,
                "url": f"{LISTING}/{quote(key)}",
                "scope": scope,
                "period": period,
                "year": annual[2] if annual else None,
                "bytes": int(field("Size")),
                "etag": field("ETag"),
                "last_modified": field("LastModified"),
                "availability": "listed",
                "packaging": ("monthly_zip" if monthly else "annual_zip")
                if is_zip and (monthly or annual)
                else "other",
                "zip_entries": None,
                "csv_files": None,
                "checksum": None,
            }
        )
    truncated = root.findtext("s:IsTruncated", namespaces=NS) == "true"
    marker = root.findtext("s:NextMarker", namespaces=NS)
    if truncated and not marker:
        marker = objects[-1]["key"] if objects else None
        if not marker:
            raise ValueError("Truncated official listing without a continuation")
    return objects, marker if truncated else None


def select_window(objects: list[dict[str, Any]], months: int = 24) -> list[str]:
    current = datetime.now(NY).strftime("%Y-%m")
    periods = {
        o["period"]
        for o in objects
        if o["scope"] == "NYC"
        and o["packaging"] == "monthly_zip"
        and o["period"] < current
    }
    if not periods:
        raise ValueError("No completed NYC month is listed")
    latest = max(periods)
    window = [shift_month(latest, i) for i in range(1 - months, 1)]
    missing = sorted(set(window) - periods)
    if missing:
        raise ValueError(f"Incomplete required NYC window: {missing}")
    for period in window:
        candidates = [
            o
            for o in objects
            if o["period"] == period
            and o["scope"] == "NYC"
            and o["packaging"] == "monthly_zip"
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Ambiguous packaging for {period}; explicit review required"
            )
    return window


def discover(root: Path, client: httpx.Client | None = None) -> Path:
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    try:
        objects: list[dict[str, Any]] = []
        pages: list[bytes] = []
        marker = None
        seen = set()
        while True:
            response = None
            for attempt in range(3):
                try:
                    response = client.get(
                        LISTING, params={"marker": marker} if marker else {}
                    )
                    response.raise_for_status()
                    break
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
            assert response is not None
            pages.append(response.content)
            batch, marker = parse_page(response.content)
            objects.extend(batch)
            if not marker:
                break
            if marker in seen or len(pages) > 100:
                raise ValueError("Listing continuation loop")
            seen.add(marker)
        if len({o["key"] for o in objects}) != len(objects):
            raise ValueError("Duplicate keys across listing pages")
        window = select_window(objects)
        checked = datetime.now(UTC).isoformat()
        report = {
            "version": INVENTORY_VERSION,
            "checked_at": checked,
            "source_url": LISTING,
            "official_interface": "https://s3.amazonaws.com/tripdata/index.html",
            "scope": "NYC",
            "window": window,
            "objects": objects,
            "limitations": (
                "Full official listing; contents and SHA verified only "
                "for downloaded selected objects. JC excluded by scope."
            ),
        }
        destination = root / "inventory" / identity(report)
        for index, content in enumerate(pages):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"listing-{index}.xml").write_bytes(content)
        write_json(destination / "inventory.json", report)
        return destination / "inventory.json"
    finally:
        if own:
            client.close()


def ingest_window(
    inventory: Path,
    root: Path,
    workers: int = 1,
    ranged: bool = False,
    period: str | None = None,
) -> Path:
    report = read_json(inventory)
    window = report["window"]
    selected = [
        o for o in report["objects"] if o["scope"] == "NYC" and o["period"] in window
    ]

    def ingest(item: dict[str, Any]) -> dict[str, Any]:
        before = time.monotonic()
        try:
            bronze = (
                download_ranges(item, root)
                if ranged
                else download(
                    item["url"],
                    item["period"],
                    root,
                    expected_etag=item["etag"],
                    expected_bytes=item["bytes"],
                )
            )
            manifest = SourceManifest.model_validate_json(
                (bronze / "manifest.json").read_text(encoding="utf-8")
            )
            result = {
                **item,
                **manifest.model_dump(mode="json"),
                "source_version": manifest.checksum,
                "bronze": str(bronze),
                "availability": "verified",
                "elapsed_seconds": round(time.monotonic() - before, 3),
            }
            print(
                f"Verified {item['period']}: {len(manifest.csv_files)} CSV, "
                f"{manifest.bytes} bytes",
                flush=True,
            )
        except Exception as exc:
            result = {
                **item,
                "availability": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            print(f"Failed {item['period']}: {type(exc).__name__}", flush=True)
        write_json(root / "inventory" / "objects" / f"{identity(item)}.json", result)
        return result

    if period is not None and period not in window:
        raise ValueError("Retry period must belong to the selected window")
    ordered = sorted(
        [o for o in selected if period is None or o["period"] == period],
        key=lambda o: o["period"],
    )
    if workers == 1:
        downloaded = [ingest(item) for item in ordered]
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            downloaded = list(executor.map(ingest, ordered))
    # A concurrent repair may have replaced a failed receipt with a verified one.
    downloaded = [
        read_json(root / "inventory/objects" / f"{identity(o)}.json")
        if (root / "inventory/objects" / f"{identity(o)}.json").exists()
        else {**o, "availability": "pending"}
        for o in sorted(selected, key=lambda item: item["period"])
    ]
    result = {
        "inventory": str(inventory),
        "checked_at": datetime.now(UTC).isoformat(),
        "window": window,
        "objects": downloaded,
        "complete": len(downloaded) == len(window)
        and all(o["availability"] == "verified" for o in downloaded),
    }
    path = root / "inventory" / "receipts" / f"{identity(result)}.json"
    write_json(path, result)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--ingest", action="store_true")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    parser.add_argument("--ranged", action="store_true")
    parser.add_argument("--period")
    args = parser.parse_args()
    path = args.inventory or discover(args.data_dir)
    print(path, flush=True)
    if args.ingest:
        receipt = ingest_window(
            path, args.data_dir, args.workers, args.ranged, args.period
        )
        print(receipt, flush=True)
        if not read_json(receipt)["complete"] and args.period is None:
            raise SystemExit("Historical ingest incomplete; see receipt")


if __name__ == "__main__":
    main()
