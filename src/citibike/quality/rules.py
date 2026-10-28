"""SQL rules keep ambiguous/nonexistent NY times out of canonical metrics."""

TIME_PATTERN = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?"


def temporal_reasons(prefix: str, raw: str) -> list[str]:
    local, utc = f"{prefix}_local", f"{prefix}_candidate"
    return [
        f"CASE WHEN {local} IS NULL OR NOT coalesce(regexp_full_match("
        f"{raw}, '{TIME_PATTERN}'), false) THEN 'invalid_{prefix}_time' END",
        f"CASE WHEN timezone('America/New_York', {utc}) <> {local} "
        f"THEN 'nonexistent_{prefix}_time' END",
        f"CASE WHEN timezone('America/New_York', {utc} - INTERVAL '1 hour') = {local} "
        f"OR timezone('America/New_York', {utc} + INTERVAL '1 hour') = {local} "
        f"THEN 'ambiguous_{prefix}_time' END",
    ]


def critical_reasons() -> str:
    rules = ["CASE WHEN ride_id IS NULL THEN 'missing_ride_id' END"]
    rules += temporal_reasons("start", "started_at_raw")
    rules += temporal_reasons("end", "ended_at_raw")
    rules += [
        "CASE WHEN end_candidate < start_candidate THEN 'negative_duration' END",
        "CASE WHEN end_candidate > (SELECT retrieved_at FROM context) "
        "THEN 'future_event' END",
    ]
    return "list_filter([" + ",".join(rules) + "], r -> r IS NOT NULL)"
