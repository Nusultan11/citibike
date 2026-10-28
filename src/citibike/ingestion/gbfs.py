"Bounded official GBFS collection; immutable JSON and explicit missing/stale states."

import argparse
import hashlib
import json
import math
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import duckdb
import httpx

from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json

DISCOVERY = "https://gbfs.citibikenyc.com/gbfs/2.3/gbfs.json"
GBFS_PIPELINE = "f2-gbfs-2"
HOSTS = {"gbfs.citibikenyc.com", "gbfs.lyft.com"}


class InvalidStations(ValueError):
    def __init__(self, quarantine: list[dict[str, Any]]) -> None:
        super().__init__("All GBFS station rows invalid")
        self.quarantine = quarantine


def timestamp(value: Any, version: str) -> datetime:
    if version == "2.3":
        if type(value) is not int or value < 0:
            raise ValueError("Invalid GBFS epoch timestamp")
        return datetime.fromtimestamp(value, UTC)
    if version == "3.0" and isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("GBFS timestamp must include timezone")
        return result.astimezone(UTC)
    raise ValueError("Unsupported GBFS version or timestamp")


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("GBFS feed URL is outside official hosts")
    return url


def envelope(value: dict[str, Any], received: datetime) -> dict[str, Any]:
    version = value.get("version")
    if version not in {"2.3", "3.0"}:
        raise ValueError("Unsupported GBFS envelope version")
    updated = timestamp(value.get("last_updated"), version)
    ttl = value.get("ttl")
    if type(ttl) is not int or ttl < 0:
        raise ValueError("Invalid GBFS ttl")
    age = (received - updated).total_seconds()
    if age < -5:
        raise ValueError("GBFS source timestamp is in the future")
    return {
        "version": version,
        "last_updated": updated.isoformat(),
        "ttl": ttl,
        "source_age_seconds": age,
        "stale": age > (ttl if ttl else 300),
        "freshness_policy": (
            "Positive TTL: conservative expiry; ttl=0: refresh "
            "always, 300-second maximum source age per GBFS latency "
            "guidance."
        ),
        "received_at_utc": received.isoformat(),
    }


def feed_urls(discovery: dict[str, Any]) -> dict[str, str]:
    version = discovery.get("version")
    if version == "2.3":
        feeds = discovery["data"]["en"]["feeds"]
    elif version == "3.0":
        feeds = discovery["data"]["feeds"]
    else:
        raise ValueError("Unknown GBFS discovery version")
    result = {}
    for name in ("station_information", "station_status"):
        candidates = [f["url"] for f in feeds if f["name"] == name]
        if len(candidates) != 1:
            raise ValueError(f"Missing or duplicate discovery feed: {name}")
        result[name] = safe_url(candidates[0])
    return result


def fetch(client: httpx.Client, url: str) -> tuple[bytes, datetime]:
    for attempt in range(3):
        try:
            with client.stream(
                "GET", safe_url(url), headers={"Cache-Control": "no-cache"}
            ) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 25_000_000:
                        raise ValueError("GBFS response exceeds size limit")
                return bytes(chunks), datetime.now(UTC)
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
    raise RuntimeError("Unreachable GBFS request state")


def station_index(feed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stations = feed["data"]["stations"]
    if not isinstance(stations, list) or not stations:
        raise ValueError("Empty/malformed station feed")
    result = {}
    for item in stations:
        sid = item.get("station_id")
        if not isinstance(sid, str) or not sid.strip() or sid.strip() in result:
            raise ValueError("Missing/duplicate station ID; snapshot grain blocked")
        result[sid.strip()] = item
    return result


def nonnegative(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("GBFS count must be a nonnegative integer")
    return int(value)


def flag(value: Any, version: str) -> bool | None:
    if value is None:
        return None
    if version == "2.3" and type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is bool:
        return bool(value)
    raise ValueError("Invalid GBFS boolean flag")


def coordinate(value: Any, limit: int) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or abs(value) > limit
    ):
        raise ValueError("Invalid GBFS coordinate")
    return float(value)


def normalize(
    information: dict[str, Any],
    status: dict[str, Any],
    info_meta: dict[str, Any],
    status_meta: dict[str, Any],
    collection_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    infos, statuses = station_index(information), station_index(status)
    if info_meta["version"] != status_meta["version"]:
        raise ValueError("Mixed GBFS feed versions")
    version = status_meta["version"]
    received = datetime.fromisoformat(status_meta["received_at_utc"])
    rows, quarantine, observations = [], [], []
    aliases = [i.get("short_name") for i in infos.values() if i.get("short_name")]
    alias_unique = len(aliases) == len(set(aliases))
    for sid in sorted(infos.keys() | statuses.keys()):
        info, state = infos.get(sid, {}), statuses.get(sid, {})
        reasons = []
        try:
            capacity = nonnegative(info.get("capacity"))
            lat, lon = coordinate(info.get("lat"), 90), coordinate(info.get("lon"), 180)
            bikes = nonnegative(
                state.get(
                    "num_bikes_available"
                    if version == "2.3"
                    else "num_vehicles_available"
                )
            )
            docks = nonnegative(state.get("num_docks_available"))
            renting, returning = (
                flag(state.get("is_renting"), version),
                flag(state.get("is_returning"), version),
            )
            installed = flag(state.get("is_installed"), version)
            reported = (
                timestamp(state["last_reported"], version)
                if state.get("last_reported") is not None
                else None
            )
            age = (received - reported).total_seconds() if reported else None
            if age is not None and age < -5:
                raise ValueError("Station last_reported is in the future")
            stale = status_meta["stale"] or (
                age is not None and age > (status_meta["ttl"] or 300)
            )
            unknown_freshness = reported is None
            if not info:
                reasons.append("missing_station_information")
            if not state:
                reasons.append("missing_station_status")
            if (
                bikes is None
                or docks is None
                or renting is None
                or returning is None
                or installed is None
            ):
                reasons.append("unknown_status_fields")
            if lat is None or lon is None or capacity is None:
                reasons.append("unknown_station_fields")
            if info_meta["stale"]:
                reasons.append("stale_information")
            if stale and state:
                reasons.append("stale_status")
            if unknown_freshness and state:
                reasons.append("unknown_last_reported")
            if (
                capacity is not None
                and bikes is not None
                and docks is not None
                and bikes + docks > capacity
            ):
                reasons.append("counts_exceed_capacity")
            if not alias_unique:
                reasons.append("nonunique_trip_station_alias")
            row = {
                "collection_id": collection_id,
                "station_id": sid,
                "trip_station_id": info.get("short_name") if alias_unique else None,
                "lat": lat,
                "lon": lon,
                "capacity": capacity,
                "bikes_available": bikes,
                "docks_available": docks,
                "is_renting": renting,
                "is_returning": returning,
                "is_installed": installed,
                "rentable_bikes": bikes
                if info
                and not stale
                and not unknown_freshness
                and renting is True
                and installed is True
                else None,
                "returnable_docks": docks
                if info
                and not stale
                and not unknown_freshness
                and returning is True
                and installed is True
                else None,
                "last_reported": reported.isoformat() if reported else None,
                "received_at_utc": received.isoformat(),
                "ttl": status_meta["ttl"],
                "feed_version": version,
                "pipeline_version": GBFS_PIPELINE,
                "quality_status": "missing"
                if not state
                else "unknown"
                if unknown_freshness
                else "stale"
                if stale
                else "warning"
                if reasons
                else "valid",
                "quality_reason_codes": reasons,
            }
            rows.append(row)
            if info and not info_meta["stale"]:
                observations.append(
                    {
                        "station_id": sid,
                        "trip_station_id": row["trip_station_id"],
                        "name": info.get("name"),
                        "lat": lat,
                        "lon": lon,
                        "capacity": capacity,
                        "observed_at": info_meta["received_at_utc"],
                        "valid_until": (
                            datetime.fromisoformat(info_meta["last_updated"])
                            + timedelta(seconds=info_meta["ttl"])
                        ).isoformat(),
                        "source_checksum": info_meta["checksum"],
                        "collection_id": collection_id,
                    }
                )
        except (ValueError, TypeError, OverflowError) as exc:
            quarantine.append(
                {
                    "station_id": sid,
                    "reason": str(exc),
                    "information": info,
                    "status": state,
                }
            )
    if len(rows) + len(quarantine) != len(infos.keys() | statuses.keys()):
        raise ValueError("GBFS row reconciliation failed")
    if not rows:
        raise InvalidStations(quarantine)
    return rows, quarantine, observations


def collect(
    root: Path, collection_id: str | None = None, client: httpx.Client | None = None
) -> Path:
    collection_id = (
        collection_id
        or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]
    )
    if not collection_id.replace("-", "").isalnum() or len(collection_id) > 100:
        raise ValueError("Unsafe collection ID")
    base = root / "gbfs"
    destination = base / "bronze" / collection_id
    if destination.exists():
        manifest = read_json(destination / "manifest.json")
        for name, feed_meta in manifest["feeds"].items():
            if checksum(destination / f"{name}.json") != feed_meta["checksum"]:
                raise ValueError("Immutable GBFS checksum mismatch")
        return materialize(root, destination)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        stage = Path(directory) / "collection"
        stage.mkdir()
        metadata: dict[str, Any] = {
            "collection_id": collection_id,
            "pipeline_version": GBFS_PIPELINE,
            "feeds": {},
        }
        try:
            pending = {"gbfs": DISCOVERY}
            while pending:
                name, url = pending.popitem()
                content, received = fetch(client, url)
                (stage / f"{name}.json").write_bytes(content)
                value = json.loads(content)
                meta = {
                    **envelope(value, received),
                    "url": url,
                    "checksum": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
                metadata["feeds"][name] = meta
                if name == "gbfs":
                    pending.update(feed_urls(value))
            write_json(stage / "manifest.json", metadata)
            stage.rename(destination)
        except Exception as exc:
            write_json(
                stage / "failure.json",
                {**metadata, "error_type": type(exc).__name__, "error": str(exc)},
            )
            failed = base / "failed" / collection_id
            failed.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(failed)
            raise
        finally:
            if own:
                client.close()
    return materialize(root, destination)


def materialize(root: Path, bronze: Path) -> Path:
    manifest = read_json(bronze / "manifest.json")
    cid: str = manifest["collection_id"]
    destination = root / "gbfs" / "silver" / GBFS_PIPELINE / cid
    if destination.exists():
        quality = read_json(destination / "quality.json")
        if checksum(destination / "station_status.parquet") != quality["checksum"]:
            raise ValueError("GBFS Silver checksum mismatch")
        # Recover a crash after Silver rename but before history/pointer activation.
        rebuild_station_history(root)
        write_json(
            root / "gbfs/active.json",
            {
                "collection_id": cid,
                "path": str(destination),
                "checksum": quality["checksum"],
            },
        )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        information = read_json(bronze / "station_information.json")
        status = read_json(bronze / "station_status.json")
        info_meta = manifest["feeds"]["station_information"]
        status_meta = manifest["feeds"]["station_status"]
        info_meta = {
            **info_meta,
            **envelope(
                information, datetime.fromisoformat(info_meta["received_at_utc"])
            ),
        }
        status_meta = {
            **status_meta,
            **envelope(status, datetime.fromisoformat(status_meta["received_at_utc"])),
        }
        rows, quarantine, observations = normalize(
            information,
            status,
            info_meta,
            status_meta,
            cid,
        )
        with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
            stage = Path(directory) / "result"
            stage.mkdir()
            write_json(stage / "normalized.json", rows)
            write_json(stage / "quarantine.json", quarantine)
            write_json(stage / "station_observations.json", observations)
            with duckdb.connect() as db:
                db.execute(
                    "SET TimeZone='UTC'; SET memory_limit='256MB'; SET threads=2"
                )
                db.execute(
                    """COPY (SELECT * FROM read_json($input, columns={
                    collection_id:'VARCHAR',station_id:'VARCHAR',trip_station_id:'VARCHAR',
                    lat:'DOUBLE',lon:'DOUBLE',capacity:'BIGINT',bikes_available:'BIGINT',
                    docks_available:'BIGINT',is_renting:'BOOLEAN',is_returning:'BOOLEAN',
                    is_installed:'BOOLEAN',rentable_bikes:'BIGINT',returnable_docks:'BIGINT',
                    last_reported:'TIMESTAMPTZ',received_at_utc:'TIMESTAMPTZ',ttl:'BIGINT',
                    feed_version:'VARCHAR',pipeline_version:'VARCHAR',quality_status:'VARCHAR',
                    quality_reason_codes:'VARCHAR[]'}))
                    TO $output (FORMAT PARQUET,COMPRESSION ZSTD)""",
                    {
                        "input": str(stage / "normalized.json"),
                        "output": str(stage / "station_status.parquet"),
                    },
                )
                grain = db.execute(
                    (
                        "SELECT count(*)-count(DISTINCT "
                        "(collection_id,station_id)) FROM read_parquet(?)"
                    ),
                    [str(stage / "station_status.parquet")],
                ).fetchone()
                if grain != (0,):
                    raise ValueError("GBFS Silver grain violated")
            quality = {
                "collection_id": cid,
                "pipeline_version": GBFS_PIPELINE,
                "input_station_ids": len(rows) + len(quarantine),
                "accepted": len(rows),
                "invalid": len(quarantine),
                "quality_counts": {
                    s: sum(r["quality_status"] == s for r in rows)
                    for s in ["valid", "warning", "stale", "missing", "unknown"]
                },
                "reason_counts": {
                    reason: sum(reason in r["quality_reason_codes"] for r in rows)
                    for reason in sorted(
                        {x for r in rows for x in r["quality_reason_codes"]}
                    )
                },
                "manifest": manifest,
                "checksum": checksum(stage / "station_status.parquet"),
            }
            write_json(stage / "quality.json", quality)
            stage.rename(destination)
        rebuild_station_history(root)
        write_json(
            root / "gbfs" / "active.json",
            {
                "collection_id": cid,
                "path": str(destination),
                "checksum": quality["checksum"],
            },
        )
        return destination
    except Exception as exc:
        if isinstance(exc, InvalidStations):
            write_json(
                root / "gbfs" / "failed" / f"{cid}-quarantine.json", exc.quarantine
            )
        write_json(
            root / "gbfs" / "failed" / f"{cid}-normalization.json",
            {"collection_id": cid, "error": str(exc), "error_type": type(exc).__name__},
        )
        raise


def rebuild_station_history(root: Path) -> Path:
    records: dict[str, list[dict[str, Any]]] = {}
    collections = []
    for path in sorted(
        (root / "gbfs" / "silver" / GBFS_PIPELINE).glob("*/station_observations.json")
    ):
        collections.append(path.parent.name)
        for observation in json.loads(path.read_text(encoding="utf-8")):
            records.setdefault(observation["station_id"], []).append(observation)
    intervals = []
    for observations in records.values():
        previous = None
        for observation in sorted(observations, key=lambda o: o["observed_at"]):
            start = datetime.fromisoformat(observation["observed_at"])
            until = datetime.fromisoformat(observation["valid_until"])
            if until <= start:
                continue
            signature = identity(
                {
                    k: observation[k]
                    for k in ["name", "trip_station_id", "lat", "lon", "capacity"]
                }
            )
            if (
                previous
                and observation["observed_at"] == previous["valid_from"]
                and previous["attribute_hash"] != signature
            ):
                raise ValueError(
                    "Conflicting station observations at the same timestamp"
                )
            if (
                previous
                and start <= datetime.fromisoformat(previous["valid_to"])
                and previous["attribute_hash"] == signature
            ):
                previous["valid_to"] = max(previous["valid_to"], until.isoformat())
                previous["last_seen"] = observation["observed_at"]
            else:
                if previous and start < datetime.fromisoformat(previous["valid_to"]):
                    previous["valid_to"] = observation["observed_at"]
                previous = {
                    **observation,
                    "valid_from": observation["observed_at"],
                    "valid_to": until.isoformat(),
                    "last_seen": observation["observed_at"],
                    "attribute_hash": signature,
                }
                intervals.append(previous)
    # A confirmed reuse of the operator's trip alias closes its prior observation;
    # overlapping aliases must never multiply historical trip rows on a join.
    aliases: dict[str, dict[str, Any]] = {}
    for interval in sorted(intervals, key=lambda i: i["valid_from"]):
        alias = interval["trip_station_id"]
        previous_alias = aliases.get(alias) if alias else None
        if previous_alias and interval["valid_from"] < previous_alias["valid_to"]:
            previous_alias["valid_to"] = interval["valid_from"]
        if alias:
            aliases[alias] = interval
    version = identity([GBFS_PIPELINE, collections])
    destination = root / "gbfs" / "station_history" / version
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        saved = read_json(destination / "manifest.json")
        if checksum(destination / "dim_station_history.parquet") != saved["checksum"]:
            raise ValueError("Immutable station history changed")
        write_json(
            root / "gbfs/station_history/active.json",
            {"version": version, "path": str(destination), "intervals": len(intervals)},
        )
        return destination
    with tempfile.TemporaryDirectory(dir=destination.parent) as working:
        stage = Path(working) / "history"
        stage.mkdir()
        _write_history(stage, intervals)
        write_json(
            stage / "manifest.json",
            {
                "version": version,
                "checksum": checksum(stage / "dim_station_history.parquet"),
            },
        )
        stage.rename(destination)
    write_json(
        root / "gbfs" / "station_history" / "active.json",
        {"version": version, "path": str(destination), "intervals": len(intervals)},
    )
    return destination


def _write_history(destination: Path, intervals: list[dict[str, Any]]) -> None:
    write_json(destination / "intervals.json", intervals)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        db.execute(
            """COPY (SELECT * FROM read_json($input, columns={
            station_id:'VARCHAR',trip_station_id:'VARCHAR',name:'VARCHAR',lat:'DOUBLE',
            lon:'DOUBLE',capacity:'BIGINT',observed_at:'TIMESTAMPTZ',valid_until:'TIMESTAMPTZ',
            source_checksum:'VARCHAR',collection_id:'VARCHAR',valid_from:'TIMESTAMPTZ',
            valid_to:'TIMESTAMPTZ',last_seen:'TIMESTAMPTZ',attribute_hash:'VARCHAR'}))
            TO $output (FORMAT PARQUET)""",
            {
                "input": str(destination / "intervals.json"),
                "output": str(destination / "dim_station_history.parquet"),
            },
        )
        overlap = db.execute(
            (
                "SELECT count(*) FROM (SELECT valid_from,lag(valid_to) "
                "OVER (PARTITION BY station_id ORDER BY valid_from) "
                "previous_end FROM read_parquet(?)) WHERE "
                "valid_from<previous_end"
            ),
            [str(destination / "dim_station_history.parquet")],
        ).fetchone()
        if overlap != (0,):
            raise ValueError("Station history intervals overlap")
        invalid = db.execute(
            """SELECT count(*) FROM (
            SELECT station_id,valid_from,count(*) n FROM read_parquet(?)
            GROUP BY 1,2 HAVING n<>1)""",
            [str(destination / "dim_station_history.parquet")],
        ).fetchone()
        if invalid != (0,):
            raise ValueError("Station history grain violated")
        if db.execute(
            ("SELECT count(*) FROM read_parquet(?) WHERE valid_to<=valid_from"),
            [str(destination / "dim_station_history.parquet")],
        ).fetchone() != (0,):
            raise ValueError("Station history interval is empty or reversed")


def poll(root: Path, count: int, max_duration_seconds: int = 7200) -> list[str]:
    if not 1 <= count <= 120:
        raise ValueError("Collection count must be bounded between 1 and 120")
    results = []
    deadline = time.monotonic() + max_duration_seconds
    for index in range(count):
        if time.monotonic() > deadline:
            raise ValueError("Bounded GBFS collection duration exhausted")
        path = collect(root)
        quality = read_json(path / "quality.json")
        results.append(str(path))
        print(
            json.dumps(
                {"collection_id": path.name, "counts": quality["quality_counts"]}
            ),
            flush=True,
        )
        if index + 1 < count:
            ttl = quality["manifest"]["feeds"]["station_status"]["ttl"]
            if time.monotonic() + max(1, ttl) > deadline:
                raise ValueError("TTL wait exceeds bounded GBFS collection duration")
            time.sleep(max(1, ttl))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    poll(args.data_dir, args.count)


if __name__ == "__main__":
    main()
