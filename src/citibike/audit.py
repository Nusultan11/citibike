"""Independent full-CSV baseline: stdlib csv/zoneinfo + disk-backed SQLite.

Does not import transformation, quality-rule or Gold calculation helpers.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

_COLUMNS = (
    "ride_id",
    "rideable_type",
    "started_at",
    "ended_at",
    "start_station_name",
    "start_station_id",
    "end_station_name",
    "end_station_id",
    "start_lat",
    "start_lng",
    "end_lat",
    "end_lng",
    "member_casual",
)


def independent_time(value: str) -> tuple[datetime | None, str | None]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?", value):
        return None, "invalid"
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None, "invalid"
    candidates = set()
    zone = ZoneInfo("America/New_York")
    for fold in (0, 1):
        utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(utc)
    if not candidates:
        return None, "nonexistent"
    if len(candidates) > 1:
        return None, "ambiguous"
    return candidates.pop(), None


def micros(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def audit_archive(source: Path, work: Path) -> dict[str, Any]:
    before = time.monotonic()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    archive_path = source / "source.zip"
    digest = hashlib.sha256()
    with archive_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != manifest["checksum"]:
        raise ValueError("Independent source checksum mismatch")
    year, month = map(int, manifest["period"].split("-"))
    zone = ZoneInfo("America/New_York")
    start = datetime(year, month, 1, tzinfo=zone).astimezone(UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone).astimezone(UTC)
    received = datetime.fromisoformat(manifest["retrieved_at"].replace("Z", "+00:00"))
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as directory:
        db = sqlite3.connect(str(Path(directory) / "baseline.sqlite"))
        try:
            db.executescript("""
                PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
                PRAGMA cache_size=-32768; PRAGMA temp_store=FILE;
                CREATE TABLE raw(file TEXT,n INTEGER,ride_id TEXT,payload TEXT,
                    eligible INTEGER,start_us INTEGER,end_us INTEGER,
                    start_station TEXT,end_station TEXT);
            """)
            files: list[dict[str, Any]] = []
            critical: Counter[str] = Counter()
            with zipfile.ZipFile(archive_path) as archive:
                names = sorted(
                    i.filename
                    for i in archive.infolist()
                    if not i.is_dir()
                    and i.filename.lower().endswith(".csv")
                    and "__MACOSX" not in PurePosixPath(i.filename).parts
                    and not PurePosixPath(i.filename).name.startswith("._")
                )
                assert names == sorted(c["path"] for c in manifest["csv_files"])
                for name in names:
                    csv_hash = hashlib.sha256()
                    with archive.open(name) as raw:
                        while chunk := raw.read(1024 * 1024):
                            csv_hash.update(chunk)
                    count = 0
                    batch = []
                    with archive.open(name) as raw:
                        reader = csv.DictReader(
                            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                        )
                        if set(reader.fieldnames or []) != set(_COLUMNS):
                            raise ValueError("Independent schema check failed")
                        for count, row in enumerate(reader, 1):
                            if None in row or any(v is None for v in row.values()):
                                raise ValueError("Malformed source CSV")
                            rid = row["ride_id"].strip() or None
                            s, s_error = independent_time(row["started_at"])
                            e, e_error = independent_time(row["ended_at"])
                            errors = []
                            if rid is None:
                                errors.append("missing_ride_id")
                            if s_error:
                                errors.append(f"{s_error}_start_time")
                            if e_error:
                                errors.append(f"{e_error}_end_time")
                            if s and e and e < s:
                                errors.append("negative_duration")
                            if e and e > received:
                                errors.append("future_event")
                            critical.update(errors)
                            fingerprint = hashlib.sha256(
                                json.dumps(
                                    [row[c] for c in _COLUMNS],
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ).encode()
                            ).hexdigest()
                            batch.append(
                                (
                                    name,
                                    count,
                                    rid,
                                    fingerprint,
                                    int(not errors),
                                    micros(s) if s else None,
                                    micros(e) if e else None,
                                    row["start_station_id"].strip() or None,
                                    row["end_station_id"].strip() or None,
                                )
                            )
                            if len(batch) == 20000:
                                db.executemany(
                                    "INSERT INTO raw VALUES (?,?,?,?,?,?,?,?,?)", batch
                                )
                                batch = []
                    db.executemany("INSERT INTO raw VALUES (?,?,?,?,?,?,?,?,?)", batch)
                    db.commit()
                    files.append(
                        {
                            "file": name,
                            "input_rows": count,
                            "sha256_uncompressed": csv_hash.hexdigest(),
                        }
                    )
            db.executescript("""
                CREATE INDEX raw_identity ON raw(ride_id);
                CREATE TABLE identities AS
                    SELECT ride_id,count(DISTINCT payload) variants
                    FROM raw WHERE ride_id IS NOT NULL GROUP BY ride_id;
                CREATE UNIQUE INDEX identity_key ON identities(ride_id);
                CREATE TABLE decisions AS SELECT r.*,
                    CASE WHEN i.variants>1 THEN 'conflicting_id'
                         WHEN eligible=0 THEN 'invalid'
                         WHEN row_number() OVER (
                             PARTITION BY r.ride_id ORDER BY file,n)>1
                              THEN 'duplicate_exact' ELSE 'accepted' END disposition
                    FROM raw r LEFT JOIN identities i ON r.ride_id=i.ride_id;
            """)
            dispositions = dict(
                db.execute("SELECT disposition,count(*) FROM decisions GROUP BY 1")
            )
            total = sum(f["input_rows"] for f in files)
            assert sum(dispositions.values()) == total
            for item in files:
                item["dispositions"] = dict(
                    db.execute(
                        "SELECT disposition,count(*) FROM decisions "
                        "WHERE file=? GROUP BY 1",
                        (item["file"],),
                    )
                )
            metric = db.execute(
                """SELECT count(*) FROM decisions WHERE disposition='accepted'
                AND start_us>=? AND start_us<?""",
                (micros(start), micros(end)),
            ).fetchone()[0]
            return {
                "algorithm": "stdlib csv + zoneinfo fold roundtrip + disk SQLite; "
                "independent of Gold helpers",
                "source_checksum": digest.hexdigest(),
                "period": manifest["period"],
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "input_rows": total,
                "dispositions": dispositions,
                "files": files,
                "canonical_rows_all": dispositions.get("accepted", 0),
                "completed_published_rides": metric,
                "critical_reason_counts": dict(critical),
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "sqlite_cache_mb": 32,
            }
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_archive(args.bronze, args.output.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "files"}, indent=2))


if __name__ == "__main__":
    main()
