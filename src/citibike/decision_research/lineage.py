"""Station ID lineage for dddd.d / dddd.d0 pairs: raw ZIP, Silver, Gold and GBFS."""

import csv
import io
import json
import math
import time
import zipfile
from pathlib import Path
from typing import Any

import duckdb

from citibike.decision_research import research_root
from citibike.decision_research.features import VERSION
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json

RAW_SAMPLES = 10
SUPPORTED = "source_format_variant_supported"
UNRESOLVED = "unresolved"


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def classify(pair: dict[str, Any]) -> str:
    """Identity is never taken from string similarity alone."""
    names = pair["short"]["names"] | pair["twin"]["names"]
    raw = pair["raw_confirmed"] in (True, None)
    if (
        len(names) == 1
        and pair["coordinate_distance_m"] is not None
        and pair["coordinate_distance_m"] < 1
        and pair["transform_mismatches"] == 0
        and raw
        and not pair["gbfs"]["short_name_present"]
    ):
        return SUPPORTED
    return UNRESOLVED


def silver_files(gold: Path) -> list[str]:
    report = read_json(gold / "manifest.json")
    snapshot = read_json(Path(report["source_snapshot"]) / "manifest.json")
    return [str(Path(p["path"]) / "silver.parquet") for p in snapshot["partitions"]]


def gold_pairs(db: duckdb.DuckDBPyConnection, gold: Path) -> list[tuple[str, str]]:
    report = read_json(gold / "manifest.json")
    db.read_parquet(
        [str(Path(m["path"]) / "fact_station_daily.parquet") for m in report["months"]]
    ).create_view("gold_station")
    return [
        (r[0], r[1])
        for r in db.execute("""WITH ids AS (
                SELECT DISTINCT station_id FROM gold_station)
            SELECT x.station_id,t.station_id FROM ids x JOIN ids t
            ON t.station_id=x.station_id||'0'
            WHERE regexp_full_match(x.station_id,'\\d{4}\\.\\d')
            ORDER BY 1""").fetchall()
    ]


def silver_profile(db: duckdb.DuckDBPyConnection, ids: list[str]) -> dict[str, Any]:
    db.execute("CREATE OR REPLACE TABLE wanted(id VARCHAR)")
    db.executemany("INSERT INTO wanted VALUES (?)", [(i,) for i in ids])
    rows = db.execute("""SELECT 'start' side_kind,start_station_id id,
            start_station_name n,
            count(*) c,avg(start_lat) lat,avg(start_lng) lon,
            count(*) FILTER(WHERE trim(start_station_id_raw)<>start_station_id) bad,
            count(DISTINCT source_file) files
        FROM silver WHERE start_station_id IN (SELECT id FROM wanted) GROUP BY 1,2,3
        UNION ALL SELECT 'end',end_station_id,end_station_name,count(*),avg(end_lat),
            avg(end_lng),
            count(*) FILTER(WHERE trim(end_station_id_raw)<>end_station_id),
            count(DISTINCT source_file)
        FROM silver WHERE end_station_id IN (SELECT id FROM wanted) GROUP BY 1,2,3""")
    profile: dict[str, Any] = {}
    for role, station, name, count, lat, lon, bad, files in rows.fetchall():
        entry = profile.setdefault(
            station,
            {
                "names": set(),
                "starts": 0,
                "ends": 0,
                "coords": [],
                "bad": 0,
                "files": 0,
            },
        )
        entry["names"].add(name)
        entry["starts" if role == "start" else "ends"] += count
        if lat is not None:
            entry["coords"].append((count, lat, lon))
        entry["bad"] += bad
        entry["files"] = max(entry["files"], files)
    return profile


def gold_counts(
    db: duckdb.DuckDBPyConnection, ids: list[str]
) -> dict[str, tuple[int, int]]:
    return {
        r[0]: (int(r[1]), int(r[2]))
        for r in db.execute(
            """SELECT station_id,sum(departures),sum(arrivals) FROM gold_station
            WHERE station_id IN (SELECT id FROM wanted) GROUP BY 1"""
        ).fetchall()
    }


def raw_check(
    db: duckdb.DuckDBPyConnection, root: Path, short: str
) -> dict[str, Any] | None:
    """Read one Silver row back from its source ZIP with stdlib csv only."""
    found = db.execute(
        """SELECT source_object_id,source_file,source_row_number,ride_id_raw
        FROM silver WHERE end_station_id=? ORDER BY ended_at_utc LIMIT 1""",
        [short],
    ).fetchone()
    if found is None:
        return None
    source, name, number, ride = found
    archive = root / "bronze/NYC" / source / "source.zip"
    if not archive.exists():
        return {"status": "BLOCKED", "reason": f"raw archive absent: {archive}"}
    with zipfile.ZipFile(archive) as bundle:
        member = next(n for n in bundle.namelist() if n.endswith(name))
        with bundle.open(member) as handle:
            reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
            header = next(reader)
            for index, row in enumerate(reader, start=1):
                if index == number:
                    record = dict(zip(header, row, strict=True))
                    return {
                        "status": "checked",
                        "source_object_id": source,
                        "source_file": name,
                        "source_row_number": number,
                        "ride_id": record["ride_id"],
                        "raw_end_station_id": record["end_station_id"],
                        "raw_end_station_name": record["end_station_name"],
                        "raw_end_lat_lng": [record["end_lat"], record["end_lng"]],
                        "matches": record["ride_id"] == ride
                        and record["end_station_id"] == short,
                    }
    return {"status": "BLOCKED", "reason": "row number not found in raw CSV"}


def gbfs_names(root: Path) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {}
    files = sorted(root.glob("gbfs/bronze/*/station_information.json"))
    files += sorted(root.glob("f5/live-inputs/gbfs/bronze/*/station_information.json"))
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for station in (data.get("data") or {}).get("stations", []):
            entry = {
                "name": station.get("name"),
                "lat": station.get("lat"),
                "lon": station.get("lon"),
                "collection": path.parent.name,
            }
            seen.setdefault(str(station.get("short_name")), []).append(entry)
    return seen


def mean_point(coords: list[tuple[int, float, float]]) -> tuple[float, float] | None:
    total = sum(c for c, _, _ in coords)
    if not total:
        return None
    return (
        sum(c * lat for c, lat, _ in coords) / total,
        sum(c * lon for c, _, lon in coords) / total,
    )


def cohort_ids(root: Path, datasets: dict[str, Path]) -> dict[str, list[str]]:
    result = {}
    for name, path in datasets.items():
        with duckdb.connect() as db:
            result[name] = [
                r[0]
                for r in db.execute(
                    "SELECT station_id FROM read_parquet(?) ORDER BY 1",
                    [str(path / "stations.parquet")],
                ).fetchall()
            ]
    return result


def lineage(root: Path, dataset: Path) -> Path:
    from citibike.decision_research import populations_of

    before = time.monotonic()
    data = checked(dataset)
    gold = Path(data["gold_path"])
    cohorts = populations_of(root, dataset, smoke=data["config"]["smoke"])
    identifier = identity(
        [VERSION, "lineage", checksum(gold / "manifest.json"), RAW_SAMPLES]
    )

    def build(stage: Path) -> dict[str, Any]:
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'; SET threads=4; SET memory_limit='4GB'")
            db.read_parquet(silver_files(gold)).create_view("silver")
            pairs = gold_pairs(db, gold)
            ids = sorted({i for pair in pairs for i in pair})
            profile = silver_profile(db, ids)
            golds = gold_counts(db, ids)
            registry = gbfs_names(root)
            ordered = sorted(
                pairs, key=lambda p: -profile.get(p[0], {"ends": 0})["ends"]
            )
            raw = {
                short: raw_check(db, root, short) for short, _ in ordered[:RAW_SAMPLES]
            }
        members = cohort_ids(root, cohorts)
        report = []
        for short, twin in pairs:
            a, b = profile.get(short), profile.get(twin)
            if a is None or b is None:
                continue
            pa, pb = mean_point(a["coords"]), mean_point(b["coords"])
            entry: dict[str, Any] = {
                "short_id": short,
                "twin_id": twin,
                "short": {**a, "names": sorted(a["names"])},
                "twin": {**b, "names": sorted(b["names"])},
                "coordinate_distance_m": distance_m(pa, pb) if pa and pb else None,
                "transform_mismatches": a["bad"] + b["bad"],
                "gold_equals_silver": golds.get(short) == (a["starts"], a["ends"])
                and golds.get(twin) == (b["starts"], b["ends"]),
                "raw": raw.get(short),
                "raw_confirmed": (raw.get(short) or {}).get("matches"),
                "gbfs": {
                    "short_name_present": short in registry,
                    "twin_name_present": twin in registry,
                    "twin_names": sorted({e["name"] for e in registry.get(twin, [])}),
                },
                "cohorts": [
                    n for n, ids in members.items() if short in ids or twin in ids
                ],
            }
            for side in ("short", "twin"):
                entry[side].pop("coords")
            entry["status"] = classify(
                {
                    **entry,
                    "short": {"names": set(a["names"])},
                    "twin": {"names": set(b["names"])},
                }
            )
            report.append(entry)
        return {
            "kind": "decision_id_lineage",
            "gold_manifest_sha256": checksum(gold / "manifest.json"),
            "pairs": report,
            "summary": {
                "pairs": len(report),
                "supported": sum(r["status"] == SUPPORTED for r in report),
                "unresolved": sum(r["status"] == UNRESOLVED for r in report),
                "transform_mismatch_rows": sum(
                    r["transform_mismatches"] for r in report
                ),
                "gold_equals_silver": all(r["gold_equals_silver"] for r in report),
                "raw_rows_checked": sum(
                    1 for r in raw.values() if r and r.get("status") == "checked"
                ),
                "raw_rows_matching": sum(
                    1 for r in raw.values() if r and r.get("matches")
                ),
                "short_departures": sum(r["short"]["starts"] for r in report),
                "short_arrivals": sum(r["short"]["ends"] for r in report),
                "cohort_ids": {
                    n: sorted(
                        {r["short_id"] for r in report if r["short_id"] in ids}
                        | {r["twin_id"] for r in report if r["twin_id"] in ids}
                    )
                    for n, ids in members.items()
                },
            },
            "rule": "supported = one station name for both forms in Silver, mean "
            "coordinates < 1 m apart, the Silver raw column equals the ID on every "
            "row, the raw ZIP row holds the short form (checked for the largest "
            f"{RAW_SAMPLES} pairs with stdlib csv), and GBFS registers only the "
            "dddd.d0 form. This is "
            "evidence of a source formatting variant, not an operator confirmation; "
            "accepted Silver is unchanged",
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(research_root(root), "lineage", identifier, build, activate=False)
