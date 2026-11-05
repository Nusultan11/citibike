"""Read-only readiness and preservation checks before and after remote fits."""

from pathlib import Path
from typing import Any

import duckdb

from citibike.decision_research.features import CATALOG
from citibike.forecasting.artifacts import checked
from citibike.forecasting.feature_research import read_frame
from citibike.ingestion.archive import checksum
from citibike.storage import read_json, write_json


def preserve(output: Path) -> dict[str, Any]:
    paths = {
        p
        for folder in ("data/f4", "data/f5", "data/decision_research")
        for p in Path(folder).rglob("*")
        if p.is_file()
    }
    paths.update(Path("data").rglob("active*.json"))
    paths.update(Path("docs/evidence").glob("*.json"))
    paths.update(Path(".").glob("*.md"))
    paths.discard(Path("ROADMAP.md"))
    paths.discard(Path("docs/evidence/f4-kaggle-research.json"))
    for folder in ("data/f7", "data/staging"):
        base = Path(folder)
        paths.update(p for p in base.glob("*.json") if p.is_file())
    hashes = {p.as_posix(): checksum(p) for p in sorted(paths)}
    write_json(output, hashes)
    return {"files": len(hashes), "snapshot": str(output), "sha256": checksum(output)}


def preservation(snapshot: Path) -> dict[str, Any]:
    hashes = read_json(snapshot)
    changed = [
        p
        for p, sha in hashes.items()
        if not Path(p).is_file() or checksum(Path(p)) != sha
    ]
    if changed:
        raise ValueError(f"Protected files changed: {changed[:5]}")
    return {"files": len(hashes), "changed": 0, "snapshot_sha256": checksum(snapshot)}


def readiness(output: Path) -> Path:
    prior = read_json(Path("docs/evidence/f4-decision-research.json"))
    pop = prior["plan"]["populations"]["cohort64"]
    dataset, inputs = Path(pop["dataset"]), Path(pop["inputs"])
    report, source = checked(dataset), checked(inputs)
    diagnostic = read_json(Path("docs/evidence/f4-ml-diagnostics.json"))
    pairs = diagnostic["plan"]["excluded_ids"]
    sql = {
        "monthly": """SELECT prediction_type,month_key,count(*) total,count(y) observed,
            count(*)-count(y) missing,count(*) FILTER(WHERE y=0) zero,
            avg(y) mean,var_pop(y) variance,max(y) maximum,
            count(*) FILTER(WHERE y>=20) spikes
            FROM labels GROUP BY 1,2 ORDER BY 1,2""",
        "calendar": """SELECT prediction_type,
            dayofweek(timezone('America/New_York',hour_utc)) weekday,
            hour(timezone('America/New_York',hour_utc)) local_hour,
            count(y) n,avg(y) mean
            FROM labels GROUP BY 1,2,3 ORDER BY 1,2,3""",
        "stations": """SELECT prediction_type,station_id,
            count(*) total,count(y) observed,
            count(*)-count(y) missing,count(*) FILTER(WHERE y=0) zero,
            avg(y) mean,var_pop(y) variance,max(y) maximum
            FROM labels GROUP BY 1,2 ORDER BY 1,2""",
        "autocorrelation": """WITH ordered AS (SELECT *,
            lag(y,1) OVER w y1,lag(hour_utc,1) OVER w t1,
            lag(y,24) OVER w y24,lag(hour_utc,24) OVER w t24,
            lag(y,168) OVER w y168,lag(hour_utc,168) OVER w t168
            FROM labels WINDOW w AS (
                PARTITION BY station_id,prediction_type ORDER BY hour_utc))
            SELECT prediction_type,1 lag_hours,count(*) pairs,corr(y,y1) correlation
            FROM ordered WHERE y IS NOT NULL AND y1 IS NOT NULL
                AND hour_utc-t1=INTERVAL '1 hour' GROUP BY 1
            UNION ALL SELECT prediction_type,24,count(*),corr(y,y24)
            FROM ordered WHERE y IS NOT NULL AND y24 IS NOT NULL
                AND hour_utc-t24=INTERVAL '24 hours' GROUP BY 1
            UNION ALL SELECT prediction_type,168,count(*),corr(y,y168)
            FROM ordered WHERE y IS NOT NULL AND y168 IS NOT NULL
                AND hour_utc-t168=INTERVAL '168 hours' GROUP BY 1 ORDER BY 1,2""",
        "zero_inflation": """SELECT prediction_type,count(y) observed,
            avg((y=0)::INT) zero_share,avg(y) mean,var_pop(y) variance,
            count(*) FILTER(WHERE y>=20) spikes FROM labels GROUP BY 1 ORDER BY 1""",
    }
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='1GB'")
        db.read_parquet(str(dataset / "dense.parquet")).create_view("dense")
        db.execute("""CREATE TEMP TABLE labels AS
            SELECT station_id,hour_utc,month_key,
                'departures' prediction_type,departures y
            FROM dense WHERE hour_utc<TIMESTAMPTZ '2026-09-01 00:00:00+00'
            UNION ALL SELECT station_id,hour_utc,month_key,'arrivals',arrivals
            FROM dense WHERE hour_utc<TIMESTAMPTZ '2026-09-01 00:00:00+00'""")
        results = {}
        for name, query in sql.items():
            cursor = db.execute(query)
            columns = [d[0] for d in cursor.description]
            results[name] = [
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            ]
    folds = []
    for split in source["splits"]:
        if not split["fold"].startswith("T"):
            continue
        for field in ("train_file", "evaluation_file"):
            frame = read_frame(inputs / split[field])
            folds.append(
                {
                    "fold": split["fold"],
                    "kind": field,
                    "rows": len(frame),
                    "ambiguous_pair_rows": int(frame["station_id"].isin(pairs).sum()),
                    "ambiguous_ids": sorted(set(frame["station_id"]) & set(pairs)),
                    "flagged_ids": int(frame["anomalous_id"].sum()),
                    "suspect_zero_rows": int(frame["suspect_closure"].sum()),
                    "missing_features": {
                        name: int(frame[name].isna().sum()) for name in CATALOG
                    },
                    "targets": {
                        kind: {
                            "rows": len(part),
                            "actual_mean": float(part["actual"].mean()),
                            "zero_fraction": float((part["actual"] == 0).mean()),
                            "unseen_station_rows": int(
                                (part["history_depth"] == "none").sum()
                            ),
                        }
                        for kind, part in frame.groupby("prediction_type")
                    },
                }
            )
    write_json(
        output,
        {
            "dataset_id": report["id"],
            "dense_sha256": checksum(dataset / "dense.parquet"),
            "source": (
                "Existing verified F2/Silver -> F3 -> F4 labels; no ID merge "
                "or target change"
            ),
            "window": "[2024-10,2026-09-01 UTC); final labels/predictions not scored",
            "sql": sql,
            "results": results,
            "folds": folds,
            "catalog": CATALOG,
            "known_ambiguous_pairs": pairs,
            "source_gaps": prior["readiness"]["cohorts"]["cohort64"][
                "hours_missing_for_every_station"
            ],
            "trip_type_and_city_eda": {
                "path": "docs/evidence/f2-history.json",
                "sha256": checksum(Path("docs/evidence/f2-history.json")),
                "status": "PRIOR evidence, not a fresh Silver category recomputation",
            },
            "root_causes": {
                "DATA ERROR": (
                    "No new confirmed source error; ambiguity and suspect zero "
                    "runs are diagnostic flags"
                ),
                "MODEL ERROR": (
                    "Numerical residual and seasonal level bias on eligible "
                    "published counts"
                ),
                "INFORMATION LIMITATION": (
                    "Publication UNKNOWN, missing station-hours, ambiguous "
                    "physical identities, delayed history, future status/weather "
                    "unavailable"
                ),
            },
            "hypotheses": [
                "Overdispersion/zero fraction motivates interval and hurdle comparison",
                (
                    "Seasonal train/evaluation shift motivates "
                    "loss/regularization ablations"
                ),
                (
                    "Thin/ambiguous history motivates P/S feature ablations with "
                    "unchanged population"
                ),
            ],
            "limits": [
                (
                    "Pooled autocorrelation mixes station levels and seasonality;"
                    " descriptive, not causal"
                ),
                "Observed zero does not prove an open station",
                "Strict historical publication timestamps remain UNKNOWN",
            ],
        },
    )
    return output
