"""Offline real-source reconciliation through published PostgreSQL, HTTP and UI."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import duckdb
import httpx

from citibike.audit import audit_archive
from citibike.serving.db import connect


class MetricHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[tuple[int, str]] = []
        self.text: list[str] = []
        self.active = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if "data-completed-rides" in attributes:
            self.values.append(
                (
                    int(attributes["data-completed-rides"] or "-1"),
                    attributes.get("data-publication-id") or "",
                )
            )
            self.active = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.text.append(data)


def verify(
    result: Path,
    api: str,
    web: str,
    recompute: bool = False,
    *,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    evidence_output = evidence_path or Path("docs/evidence/f1-202604.json")
    quality = json.loads((result / "quality.json").read_text(encoding="utf-8"))
    source = quality["source"]
    version = quality["dataset_id"]
    root = result.parent.parent
    if not source["source_url"].startswith("https://s3.amazonaws.com/tripdata/"):
        raise ValueError(
            "Real-data evidence requires an official source, not a fixture"
        )
    bronze = root / "bronze/NYC" / source["checksum"]
    with (bronze / "source.zip").open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != source["checksum"]:
        raise ValueError("Source checksum differs")
    cache = root / "evidence" / f"{actual}-baseline.json"
    baseline = (
        audit_archive(bronze, root / "work")
        if recompute or not cache.exists()
        else json.loads(cache.read_text(encoding="utf-8"))
    )
    assert baseline["source_checksum"] == actual
    assert baseline["dispositions"] == quality["dispositions"]
    assert baseline["input_rows"] == quality["input_rows"]
    assert [
        (f["file"], f["input_rows"], f["dispositions"]) for f in baseline["files"]
    ] == [(f["file"], f["input_rows"], f["dispositions"]) for f in quality["files"]]
    expected = baseline["completed_published_rides"]
    start = datetime.fromisoformat(baseline["period_start"])
    end = datetime.fromisoformat(baseline["period_end"])
    with duckdb.connect() as db:
        db.execute("SET memory_limit='256MB'; SET threads=2; SET TimeZone='UTC'")
        silver = db.execute(
            "SELECT count(*) FROM read_parquet(?) "
            "WHERE started_at_utc>=? AND started_at_utc<?",
            [str(result / "silver.parquet"), start, end],
        ).fetchone()
        gold = db.execute(
            "SELECT sum(completed_published_rides) FROM read_parquet(?) "
            "WHERE hour_utc>=? AND hour_utc<?",
            [str(result / "ride_hours.parquet"), start, end],
        ).fetchone()
        assert silver is not None and gold is not None
    with connect(readonly=True) as database:
        current = database.execute(
            "SELECT publication_id FROM active_publication WHERE singleton"
        ).fetchone()
        assert current is not None and current["publication_id"] == version
        pg = database.execute(
            "SELECT sum(completed_published_rides) n FROM ride_hours "
            "WHERE publication_id=%s AND hour_utc>=%s AND hour_utc<%s",
            (version, start, end),
        ).fetchone()
        assert pg is not None
    params = {
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
    }
    with httpx.Client(timeout=20) as client:
        publication = client.get(api + "/v1/publication")
        publication.raise_for_status()
        assert publication.json()["publication"]["publication_id"] == version
        pinned = {**params, "publication_id": version}
        kpi = client.get(api + "/v1/kpi", params=pinned)
        kpi.raise_for_status()
        series = client.get(api + "/v1/timeseries", params=pinned)
        series.raise_for_status()
        page = client.get(web + "/?" + urlencode(params))
        page.raise_for_status()
    metric = MetricHTML()
    metric.feed(page.text)
    assert len(metric.values) == 1 and metric.values[0][1] == version
    visible = "".join(metric.text)
    visible_number = int("".join(c for c in visible if c.isdigit()))
    api_count = kpi.json()["completed_published_rides"]
    api_series_sum = sum(
        p["completed_published_rides"] for p in series.json()["points"]
    )
    assert (
        kpi.json()["metadata"]["publication_id"]
        == series.json()["metadata"]["publication_id"]
        == version
    )
    stages = {
        "independent_raw_csv": expected,
        "silver": silver[0],
        "gold": gold[0],
        "postgresql": int(pg["n"]),
        "api_kpi": api_count,
        "api_hourly_sum": api_series_sum,
        "dashboard_attribute": metric.values[0][0],
        "dashboard_visible_text": visible_number,
    }
    assert all(value == expected for value in stages.values()), stages
    return {
        "stage": "F1",
        "source": source,
        "publication_id": version,
        "pipeline_version": quality["pipeline_version"],
        "verified_at": datetime.now(UTC).isoformat(),
        "filters": {
            "scope": "NYC",
            "timezone": "America/New_York",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "interval": "[start,end)",
        },
        "stage_values": stages,
        "independent_baseline": baseline,
        "quality": {
            k: quality[k]
            for k in [
                "input_rows",
                "dispositions",
                "files",
                "metrics",
                "reason_counts",
                "raw_missing_fields",
                "raw_categories",
                "profile",
                "quality_status",
                "rules",
            ]
        },
        "artifact_checksums": quality["artifact_checksums"],
        "artifact_bytes": {p.name: p.stat().st_size for p in result.glob("*.parquet")},
        "processing": {
            "elapsed_seconds": quality["elapsed_seconds"],
            "duckdb_memory_limit": "512MB",
            "threads": 2,
            "peak_rss": "NOT VERIFIED; configured engine limit "
            "is not a process peak measurement",
            "note": "Processing and independent baseline ran concurrently; "
            "no speedup or SLA claim",
        },
        "api_metadata": kpi.json()["metadata"],
        "ui_visible_metric": visible,
        "source_policy": "https://citibikenyc.com/data-sharing-policy",
        "privacy": "Aggregated non-commercial verification only; "
        "no raw rides or personal data are redistributed",
        "reproduce": [
            "uv run --locked python -m citibike.pipeline "
            f"--period {source['period']} --url {source['source_url']}",
            f"uv run --locked python -m citibike.verify --result {result.as_posix()} "
            f"--output {evidence_output.as_posix()} "
            "--recompute-baseline",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:3000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recompute-baseline", action="store_true")
    args = parser.parse_args()
    evidence = verify(
        args.result,
        args.api_url,
        args.web_url,
        args.recompute_baseline,
        evidence_path=args.output,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(evidence["stage_values"], indent=2))


if __name__ == "__main__":
    main()
