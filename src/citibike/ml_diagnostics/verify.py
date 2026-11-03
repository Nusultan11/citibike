"""Independent Silver/bisect, Python history and interval/metric oracles.

Does not call baseline.calculate, baseline.summary or events.detect. Its input
is the frozen protocol and source files, not claimed aggregate answers.
"""

import bisect
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from citibike.features.contracts import utc
from citibike.forecasting.artifacts import checked
from citibike.ingestion.archive import checksum
from citibike.ml_diagnostics.protocol import VERSION, boundary, check_files, load_plan
from citibike.storage import identity, read_json

NY = ZoneInfo("America/New_York")


def close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-8)


def oracle_metrics(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    n = len(pairs)
    actual = math.fsum(a for a, _ in pairs)
    absolute = math.fsum(abs(p - a) for a, p in pairs)
    return {
        "n": n,
        "actual_sum": actual,
        "mae": absolute / n,
        "wape": absolute / actual if actual else None,
        "rmse": math.sqrt(math.fsum((p - a) ** 2 for a, p in pairs) / n),
        "bias": math.fsum(p - a for a, p in pairs) / n,
    }


def label_oracle(
    db: duckdb.DuckDBPyConnection, plan: dict[str, Any], coverage: dict[str, Any]
) -> int:
    """Rebuild observed/missing labels directly from Silver, not Gold/dense helpers."""
    data = checked(Path(plan["dataset"]))
    paths = [s["silver"] for s in plan["sources"]]
    end = boundary(data["config"]["final"][0])
    first = db.execute("SELECT min(hour_utc) FROM dense").fetchone()
    assert first
    cities: dict[str, set[datetime]] = {}
    counts: dict[tuple[str, str, datetime], int] = {}
    boundaries: dict[tuple[str, str], set[datetime]] = defaultdict(set)
    for kind, field, stamp in (
        ("departures", "start_station_id", "started_at_utc"),
        ("arrivals", "end_station_id", "ended_at_utc"),
    ):
        cities[kind] = {
            r[0]
            for r in db.execute(
                f"""SELECT date_trunc('hour',{stamp})
            FROM read_parquet(?) WHERE {stamp}>=? AND {stamp}<? GROUP BY 1""",
                [paths, first[0], end],
            ).fetchall()
        }
        events = db.execute(
            f"""SELECT {field},date_trunc('hour',{stamp}),count(*)
            FROM read_parquet(?) WHERE {field} IN(SELECT station_id FROM stations)
                AND {stamp}>=? AND {stamp}<? GROUP BY 1,2""",
            [paths, first[0], end],
        ).fetchall()
        for station, hour, n in events:
            counts[(station, kind, hour)] = n
            boundaries[(station, hour.astimezone(NY).strftime("%Y-%m"))].add(hour)
    indices = {key: sorted(values) for key, values in boundaries.items()}
    gold = read_json(Path(data["gold_path"]) / "manifest.json")
    availability = {}
    for month in gold["months"]:
        dependencies = [month["month"], *(s["source_period"] for s in month["silver"])]
        dates = []
        for key in dependencies:
            year, number = map(int, key.split("-"))
            following = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=NY)
            dates.append(following.astimezone(UTC) + timedelta(days=45))
        availability[month["month"]] = max(dates)
    checked_labels = 0
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    gap_hours: dict[str, set[datetime]] = defaultdict(set)
    cur = db.execute(
        "SELECT station_id,hour_utc,month_key,departures,arrivals,label_available_at "
        "FROM dense WHERE hour_utc<? ORDER BY station_id,hour_utc",
        [end],
    )
    while batch := cur.fetchmany(8192):
        for station, hour, month, dep, arr, assumed in batch:
            local_month = hour.astimezone(NY).strftime("%Y-%m")
            if local_month != month or assumed != availability[month]:
                raise ValueError("Independent month/availability lineage mismatch")
            sequence = indices.get((station, month), [])
            i = bisect.bisect_right(sequence, hour) - 1
            j = bisect.bisect_left(sequence, hour)
            bracket = (
                i >= 0
                and j < len(sequence)
                and hour - sequence[i] <= timedelta(days=7)
                and sequence[j] - hour <= timedelta(days=7)
            )
            for kind, given in (("departures", dep), ("arrivals", arr)):
                expected = (
                    counts.get((station, kind, hour), 0)
                    if bracket and hour in cities[kind]
                    else None
                )
                if expected != given:
                    raise ValueError(
                        f"Independent Silver label mismatch: {station}/{hour}/{kind}"
                    )
                if hour not in cities[kind]:
                    gap_hours[kind].add(hour)
                for dimension, key in (
                    ("summary", "all"),
                    ("by_month", month),
                    ("by_station", station),
                ):
                    entry = groups.setdefault(
                        (dimension, kind, key),
                        {
                            "station_hours": 0,
                            "observed": 0,
                            "missing": 0,
                            "observed_zero": 0,
                            "published_events": 0,
                            "excluded_hours": 0,
                            "excluded_observed": 0,
                            "excluded_events": 0,
                        },
                    )
                    entry["station_hours"] += 1
                    entry["observed"] += expected is not None
                    entry["missing"] += expected is None
                    entry["observed_zero"] += expected == 0
                    entry["published_events"] += expected or 0
                    if station in plan["excluded_ids"]:
                        entry["excluded_hours"] += 1
                        entry["excluded_observed"] += expected is not None
                        entry["excluded_events"] += expected or 0
                checked_labels += 1
    for dimension, field in (
        ("summary", "segment"),
        ("by_month", "month_key"),
        ("by_station", "station_id"),
    ):
        expected_groups = {
            key: value for key, value in groups.items() if key[0] == dimension
        }
        if len(expected_groups) != len(coverage[dimension]):
            raise ValueError("Independent source coverage groups mismatch")
        for given in coverage[dimension]:
            value = groups[(dimension, given["prediction_type"], given[field])]
            for key, number in value.items():
                if (given[key] or 0) != number:
                    raise ValueError("Independent source coverage totals mismatch")
            if dimension == "summary":
                for share, numerator, denominator in (
                    ("excluded_observed_share", "excluded_observed", "observed"),
                    ("excluded_event_share", "excluded_events", "published_events"),
                ):
                    wanted = (
                        value[numerator] / value[denominator]
                        if value[denominator]
                        else None
                    )
                    if not close(given[share], wanted):
                        raise ValueError("Independent exclusion share mismatch")
    for given in coverage["city_source_gaps"]:
        if given["city_gap_hours"] != len(gap_hours[given["prediction_type"]]):
            raise ValueError("Independent city source gap mismatch")
    return checked_labels


def history_oracle(
    db: duckdb.DuckDBPyConnection, plan: dict[str, Any], rows: list[dict[str, Any]]
) -> int:
    """Python cell accumulation, independent of the production SQL GROUP BY."""
    by_fold: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_fold[row["fold"]].append(row)
    validations = 0
    for window in plan["windows"]:
        cutoff = utc(window["start"])
        accum: dict[tuple[str, str, int, int, int], list[Any]] = {}
        cur = db.execute(
            "SELECT station_id,hour_utc,departures,arrivals,label_available_at "
            "FROM dense WHERE hour_utc<? ORDER BY hour_utc,station_id",
            [cutoff],
        )
        while batch := cur.fetchmany(8192):
            for station, hour, dep, arr, available in batch:
                local = hour.astimezone(NY)
                for kind, value in (("departures", dep), ("arrivals", arr)):
                    if value is None:
                        continue
                    key = (
                        station,
                        kind,
                        local.weekday(),
                        local.hour,
                        (local.month % 12) // 3,
                    )
                    a = accum.setdefault(key, [0, 0.0, 0.0, None, 0, 0.0])
                    a[0] += 1
                    a[1] += value
                    a[2] += value * value
                    a[3] = hour
                    if available <= cutoff:
                        a[4] += 1
                        a[5] += value
        for row in by_fold[window["fold"]]:
            local = row["target_hour_utc"].astimezone(NY)
            cell = accum.get(
                (
                    row["station_id"],
                    row["prediction_type"],
                    local.weekday(),
                    local.hour,
                    (local.month % 12) // 3,
                )
            )
            n = cell[0] if cell else None
            mean = cell[1] / n if cell and n else None
            variance = (
                max(0.0, cell[2] / n - float(mean) ** 2)
                if cell and n and mean is not None
                else None
            )
            proxy_n = cell[4] if cell else None
            proxy_mean = cell[5] / cell[4] if cell and cell[4] else None
            for name, value in (
                ("context_n", n),
                ("context_mean", mean),
                ("context_var", variance),
                ("proxy_n", proxy_n),
                ("proxy_mean", proxy_mean),
            ):
                if not close(row[name], value):
                    raise ValueError(
                        f"Independent context mismatch: {row['row_key']}/{name}"
                    )
            if row["context_max_event"] != (cell[3] if cell else None):
                raise ValueError("Independent context max event mismatch")
            if (
                row["history_cutoff"] != cutoff
                or row["weekday"] != local.weekday()
                or row["local_hour"] != local.hour
            ):
                raise ValueError("Independent DST/calendar/cutoff mismatch")
            quality = (
                "ambiguous_id"
                if row["station_id"] in plan["excluded_ids"]
                else "station_not_known"
                if row["forecast_origin_utc"] < row["known_at"]
                else "missing_observation"
                if row["actual"] is None
                else "insufficient_history"
                if (n or 0) < plan["rules"]["min_cell_history"]
                else "observed"
            )
            if quality != row["input_quality"]:
                raise ValueError("Independent input quality mismatch")
            sn_error = (
                row["predicted"] - row["actual"]
                if row["predicted"] is not None and row["actual"] is not None
                else None
            )
            residual = (
                row["actual"] - mean
                if row["actual"] is not None and mean is not None
                else None
            )
            if not close(row["sn_error"], sn_error) or not close(
                row["context_residual"], residual
            ):
                raise ValueError("Independent residual mismatch")
            validations += 1
    return validations


def event_oracle(
    rows: list[dict[str, Any]], rules: dict[str, Any], events: list[dict[str, Any]]
) -> int:
    """Separate interval reference: enumerate zero suffixes and CUSUM excursions,
    then compare the connected components of signal support intervals.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["station_id"], row["prediction_type"], row["fold"])].append(row)
    components: list[dict[str, Any]] = []
    signal_total = 0
    for key, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda r: r["target_hour_utc"])
        blocks: list[list[dict[str, Any]]] = []
        block: list[dict[str, Any]] = []
        for row in ordered:
            if row["input_quality"] != "observed":
                if block:
                    blocks.append(block)
                block = []
            else:
                if block and row["target_hour_utc"] - block[-1][
                    "target_hour_utc"
                ] != timedelta(hours=1):
                    blocks.append(block)
                    block = []
                block.append(row)
        if block:
            blocks.append(block)
        for block in blocks:
            intervals: list[tuple[int, int, str, float]] = []
            totals, starts = [0.0, 0.0], [0, 0]
            zero_start = 0
            for i, row in enumerate(block):
                y, m = row["actual"], row["context_mean"]
                z = (y - m) / math.sqrt(max(1.0, m, row["context_var"]))
                if y >= rules["spike_count"] and z >= rules["spike_z"]:
                    intervals.append((i, i, "spike", z))
                if y != 0:
                    zero_start = i + 1
                if i - zero_start + 1 >= rules["zero_hours"]:
                    expected = math.fsum(
                        r["context_mean"] for r in block[zero_start : i + 1]
                    )
                    if expected >= rules["zero_expected"]:
                        intervals.append(
                            (
                                zero_start,
                                i,
                                "long_zero",
                                expected / rules["zero_expected"],
                            )
                        )
                z = min(rules["z_clip"], max(-rules["z_clip"], z))
                for side, multiplier in enumerate((1, -1)):
                    totals[side] += multiplier * z - rules["cusum_k"]
                    if totals[side] <= 0:
                        totals[side], starts[side] = 0.0, i + 1
                    elif (
                        totals[side] >= rules["cusum_h"]
                        and i - starts[side] + 1 >= rules["cusum_min_hours"]
                    ):
                        intervals.append(
                            (
                                starts[side],
                                i,
                                "cusum_up" if side == 0 else "cusum_down",
                                totals[side] / rules["cusum_h"],
                            )
                        )
                        totals[side], starts[side] = 0.0, i + 1
            merged: list[dict[str, Any]] = []
            for left, right, kind, score in sorted(
                intervals, key=lambda v: (v[0], v[2])
            ):
                signal_total += 1
                support = {r["row_key"] for r in block[left : right + 1]}
                if (
                    merged
                    and left <= merged[-1]["right"] + 1 + rules["merge_gap_hours"]
                ):
                    item = merged[-1]
                    item["right"] = max(item["right"], right)
                    item["types"].add(kind)
                    item["score"] = max(item["score"], score)
                    item["evidence_keys"] |= support
                    item["raw_signals"] += 1
                else:
                    merged.append(
                        {
                            "left": left,
                            "right": right,
                            "types": {kind},
                            "score": score,
                            "evidence_keys": support,
                            "raw_signals": 1,
                        }
                    )
            for item in merged:
                components.append(
                    {
                        "key": key,
                        "event_at": block[item["left"]]["target_hour_utc"],
                        "event_end": block[item["right"]]["target_hour_utc"]
                        + timedelta(hours=1),
                        **item,
                    }
                )
    if len(components) != len(events):
        raise ValueError("Independent event count mismatch")
    lookup = {
        (
            e["station_id"],
            e["prediction_type"],
            e["fold"],
            utc(e["event_at"]),
            utc(e["event_end"]),
        ): e
        for e in events
    }
    if len(lookup) != len(events):
        raise ValueError("Duplicate anomaly event")
    row_lookup = {r["row_key"]: r for r in rows}
    for component in components:
        event = lookup.get(
            (*component["key"], component["event_at"], component["event_end"])
        )
        if (
            not event
            or set(event["types"]) != component["types"]
            or not close(event["score"], component["score"])
        ):
            raise ValueError("Independent event interval/score mismatch")
        if (
            set(event["evidence_keys"]) != component["evidence_keys"]
            or event["raw_signals"] != component["raw_signals"]
        ):
            raise ValueError("Independent event provenance mismatch")
        available = max(
            row_lookup[k]["input_available_at"] for k in event["evidence_keys"]
        )
        if (
            utc(event["available_at"]) != available
            or utc(event["detected_at"]) < available
            or utc(event["finalized_at"]) < utc(event["event_end"])
            or event["classification"] != "INFORMATION LIMITATION"
        ):
            raise ValueError("Independent event timestamps/classification mismatch")
        if not close(
            event["delay_hours"],
            (utc(event["detected_at"]) - utc(event["event_at"])).total_seconds() / 3600,
        ):
            raise ValueError("Independent event delay mismatch")
        expected_id = identity(
            [
                event["station_id"],
                event["prediction_type"],
                event["fold"],
                event["event_at"],
                event["event_end"],
                event["types"],
                event["provenance"],
            ]
        )
        if event["id"] != expected_id:
            raise ValueError("Independent event identity mismatch")
    return signal_total


def verify(result: Path) -> dict[str, Any]:
    report = checked(result)
    if report["kind"] != "ml_diagnostics_result":
        raise ValueError("A computed diagnostics result is required")
    protocol = Path(report["protocol"])
    plan = load_plan(protocol)
    if report["protocol_id"] != checked(protocol)["id"] or report["id"] != identity(
        [VERSION, "result", report["protocol_id"]]
    ):
        raise ValueError("Result/protocol identity mismatch")
    summary = read_json(result / "summary.json")
    events = read_json(result / "events.json")["events"]
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='1GB'")
        for name in ("dense", "examples", "stations"):
            db.read_parquet(str(Path(plan["dataset"]) / f"{name}.parquet")).create_view(
                name
            )
        gold = read_json(
            Path(checked(Path(plan["dataset"]))["gold_path"]) / "manifest.json"
        )
        db.read_parquet(
            [
                str(Path(m["path"]) / "fact_station_daily.parquet")
                for m in gold["months"]
            ]
        ).create_view("gold_ids")
        pairs = db.execute(r"""WITH ids AS (SELECT DISTINCT station_id FROM gold_ids)
            SELECT a.station_id,b.station_id FROM ids a JOIN ids b
            ON b.station_id=a.station_id||'0'
            WHERE regexp_full_match(a.station_id,'\d{4}\.\d')""").fetchall()
        if (
            sorted({station for pair in pairs for station in pair})
            != plan["excluded_ids"]
        ):
            raise ValueError("Independent station ID ambiguity exclusion mismatch")
        db.read_parquet(str(result / "rows.parquet")).create_view("diagnostic")
        cur = db.execute("SELECT * FROM diagnostic ORDER BY row_key")
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        keys = [r["row_key"] for r in rows]
        if len(keys) != len(set(keys)) or len(rows) != report["rows"]:
            raise ValueError("Diagnostics row grain mismatch")
        for window in plan["windows"]:
            # Full membership and label integrity, both directions; no sample.
            columns = (
                "station_id,forecast_origin_utc,target_hour_utc,horizon_h,"
                "prediction_type,actual,eligibility"
            )
            mismatch = db.execute(
                f"""WITH expected AS (SELECT {columns} FROM examples
                WHERE forecast_origin_utc>=? AND forecast_origin_utc<?),
                given AS (SELECT {columns} FROM diagnostic WHERE fold=?)
                SELECT count(*) FROM ((SELECT * FROM expected
                EXCEPT ALL SELECT * FROM given)
                UNION ALL (SELECT * FROM given EXCEPT ALL SELECT * FROM expected))""",
                [utc(window["start"]), utc(window["end"]), window["fold"]],
            ).fetchone()
            if mismatch != (0,):
                raise ValueError("Independent validation membership mismatch")
        frozen = [str(Path(t["path"]) / "predictions.parquet") for t in plan["trials"]]
        db.read_parquet(frozen).create_view("frozen")
        fields = (
            "station_id,forecast_origin_utc,target_hour_utc,horizon_h,"
            "prediction_type,actual,predicted"
        )
        mismatch = db.execute(f"""SELECT count(*) FROM (
            (SELECT {fields} FROM frozen EXCEPT ALL
                SELECT {fields} FROM diagnostic WHERE predicted IS NOT NULL)
            UNION ALL (SELECT {fields} FROM diagnostic WHERE predicted IS NOT NULL
                EXCEPT ALL SELECT {fields} FROM frozen))""").fetchone()
        if mismatch != (0,):
            raise ValueError("Frozen Seasonal Naive predictions mismatch")
        labels_checked = label_oracle(db, plan, summary["coverage"])
        histories_checked = history_oracle(db, plan, rows)
    segment_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    sn_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    forecast_rows = []
    qualities: dict[str, int] = defaultdict(int)
    paired: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    event_keys = {key for event in events for key in event["evidence_keys"]}
    for row in rows:
        y = row["actual"]
        row["actual_band"] = (
            "missing"
            if y is None
            else "zero"
            if y == 0
            else "1-4"
            if y < 5
            else "5-19"
            if y < 20
            else "20+"
        )
        row["contextual_event"] = row["row_key"] in event_keys
        if (
            row["input_available_at"] != utc(plan["input_available_at"])
            or row["analysis_as_of"] != utc(plan["analysis_as_of"])
            or row["input_available_at"] > row["analysis_as_of"]
        ):
            raise ValueError("Independent strict analysis as-of mismatch")
        qualities[f"{row['fold']}/{row['prediction_type']}/{row['input_quality']}"] += 1
        paired[(row["fold"], row["prediction_type"])].append(row)
        if row["predicted"] is not None and row["input_quality"] not in (
            "ambiguous_id",
            "station_not_known",
            "missing_observation",
        ):
            forecast_rows.append(row)
            for dimension in (
                "fold",
                "horizon_h",
                "local_hour",
                "season",
                "activity",
                "actual_band",
                "new_known",
                "input_quality",
                "contextual_event",
            ):
                sn_groups[
                    (row["prediction_type"], dimension, str(row[dimension]))
                ].append(row)
        if row["input_quality"] == "observed" and row["predicted"] is not None:
            for dimension in (
                "fold",
                "horizon_h",
                "station_id",
                "local_hour",
                "season",
                "activity",
                "actual_band",
                "new_known",
                "contextual_event",
            ):
                segment_groups[
                    (row["prediction_type"], dimension, str(row[dimension]))
                ].append(row)
    if dict(qualities) != summary["errors"]["quality"]:
        raise ValueError("Independent quality counts mismatch")
    if len(sn_groups) != len(summary["errors"]["sn_segments"]):
        raise ValueError("Independent SN segment coverage mismatch")
    for given in summary["errors"]["sn_segments"]:
        group = sn_groups[(given["target"], given["dimension"], given["key"])]
        computed = oracle_metrics([(r["actual"], r["predicted"]) for r in group])
        if any(
            not close(value, given["metrics"][key]) for key, value in computed.items()
        ):
            raise ValueError("Independent SN metric mismatch")
    top = sorted(forecast_rows, key=lambda r: (-abs(r["sn_error"]), r["row_key"]))[:10]
    if [r["row_key"] for r in top] != [
        r["row_key"] for r in summary["errors"]["largest_model_errors"]
    ]:
        raise ValueError("Independent largest model errors mismatch")
    if len(segment_groups) != len(summary["errors"]["segments"]):
        raise ValueError("Independent segment coverage mismatch")
    for given in summary["errors"]["segments"]:
        group = segment_groups[(given["target"], given["dimension"], given["key"])]
        for name, field in (
            ("seasonal_naive", "predicted"),
            ("context", "context_mean"),
        ):
            oracle = oracle_metrics([(r["actual"], r[field]) for r in group])
            if any(not close(value, given[name][key]) for key, value in oracle.items()):
                raise ValueError("Independent segmented metric mismatch")
    wins: dict[str, int] = defaultdict(int)
    for given in summary["errors"]["paired"]:
        group = paired[(given["fold"], given["target"])]
        eligible = [
            r
            for r in group
            if r["predicted"] is not None
            and r["input_quality"]
            not in ("ambiguous_id", "station_not_known", "missing_observation")
        ]
        supported = [r for r in eligible if r["input_quality"] == "observed"]
        share = len(supported) / len(eligible) if eligible else None
        delta = (
            math.fsum(
                abs(r["context_mean"] - r["actual"]) - abs(r["predicted"] - r["actual"])
                for r in supported
            )
            / len(supported)
            if supported
            else None
        )
        if (
            given["eligible"] != len(eligible)
            or given["supported"] != len(supported)
            or not close(given["coverage"], share)
            or not close(given["mae_delta_context_minus_sn"], delta)
        ):
            raise ValueError("Independent paired comparison mismatch")
        if share is not None and delta is not None and share >= 0.5 and delta < 0:
            wins[given["target"]] += 1
    expected_decision = (
        "ONE future seasonal-profile feature ablation accepted for research; "
        "no fit/promotion"
        if all(wins[k] >= 3 for k in ("departures", "arrivals"))
        else "inconclusive"
    )
    if summary["errors"]["decision"] != expected_decision:
        raise ValueError("Independent hypothesis decision mismatch")
    strict = sum(r["input_available_at"] <= r["forecast_origin_utc"] for r in rows)
    proxy = sum((r["proxy_n"] or 0) >= plan["rules"]["min_cell_history"] for r in rows)
    if (
        summary["as_of"]["strict_origin_supported_rows"] != strict
        or summary["as_of"]["proxy_origin_supported_rows"] != proxy
        or summary["as_of"]["rows"] != len(rows)
    ):
        raise ValueError("Independent historical-origin coverage mismatch")
    controls = summary["controls"]
    for name, delay in (
        ("spike", 1),
        ("long_zero", 6),
        ("shift_up", 4),
        ("shift_down", 6),
    ):
        case = controls["cases"][name]
        if not case["detected"] or case["first_detection_delay_hours"] != delay:
            raise ValueError("Controlled detection/delay mismatch")
    if (
        controls["real_precision_at_k"] is not None
        or controls["real_false_alert_rate"] is not None
        or any(case["status"] != "PASS" for case in controls["cases"].values())
    ):
        raise ValueError("Controlled validation / real accuracy claim mismatch")
    raw_signals = event_oracle(rows, plan["rules"], events)
    if (
        raw_signals != summary["anomalies"]["raw_signals"]
        or len(events) != summary["anomalies"]["events"]
    ):
        raise ValueError("Independent event totals mismatch")
    if (
        summary["anomalies"]["top_cases"]
        != sorted(events, key=lambda r: (-r["score"], r["id"]))[
            : plan["rules"]["top_k"]
        ]
    ):
        raise ValueError("Independent top cases mismatch")
    if (
        summary["anomalies"]["precision_at_k"] is not None
        or summary["anomalies"]["real_false_alert_rate"] is not None
    ):
        raise ValueError("Unsupported real accuracy claim")
    check_files(plan["protected_files"])
    return {
        "status": "VERIFIED validation-only; independent owner acceptance pending",
        "protocol_id": report["protocol_id"],
        "protocol": str(protocol),
        "protocol_manifest_sha256": checksum(protocol / "manifest.json"),
        "result_id": report["id"],
        "result": str(result),
        "plan": {k: v for k, v in plan.items() if k != "protected_files"},
        "oracle": {
            "silver_labels": labels_checked,
            "context_rows": histories_checked,
            "segments": len(segment_groups),
            "sn_segments": len(sn_groups),
            "events": len(events),
            "raw_signals": raw_signals,
            "mismatches": 0,
            "protected_hashes": len(plan["protected_files"]),
        },
        "results": summary,
        "limitations": [
            "strict historical PIT NOT VERIFIED; first publication UNKNOWN",
            "No new independent period; previously inspected validation only",
            "No online deployment, expert Precision@K or real false-alert rate",
            "Synthetic detection is not proof of real-demand accuracy",
            "Observed trips are not hidden demand or station operating status",
            "F8 remains PARTIAL; champion/active publications unchanged",
        ],
        "final_test_accessed": False,
    }
