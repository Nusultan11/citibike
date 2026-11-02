from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from citibike.serving.risk_api import RiskFilters, currency
from citibike.serving.station_api import StationFilters, suspected_split

MANIFEST: dict[str, Any] = {
    "data_as_of": "2026-10-30T06:36:54.163891+00:00",
    "horizon_end": "2026-10-30T11:00:00+00:00",
    "ttl_seconds": 60,
}


def test_id_split_flags_only_existing_dddd_d_and_d0_pairs() -> None:
    universe = {"5980.1", "5980.10", "6611.07", "7000.1", "1234.56", "8000.20"}
    assert suspected_split("5980.1", universe) == "5980.10"
    assert suspected_split("5980.10", universe) == "5980.1"
    assert suspected_split("7000.1", universe) is None
    assert suspected_split("6611.07", universe) is None
    assert suspected_split("8000.20", universe) is None
    assert suspected_split("Shop Morgan", universe) is None


def test_scenario_is_current_only_inside_ttl_and_open_horizon() -> None:
    as_of = datetime.fromisoformat(MANIFEST["data_as_of"])
    assert currency(MANIFEST, as_of + timedelta(seconds=30)).state == "current"
    assert currency(MANIFEST, as_of + timedelta(seconds=61)).state == "archival"
    assert currency(MANIFEST, as_of - timedelta(seconds=1)).state == "archival"
    late = {**MANIFEST, "horizon_end": (as_of + timedelta(seconds=20)).isoformat()}
    assert currency(late, as_of + timedelta(seconds=30)).state == "archival"
    recorded = currency(MANIFEST, datetime(2026, 11, 2, tzinfo=UTC))
    assert recorded.state == "archival" and recorded.as_of_age_seconds > 86400


def test_product_filters_reject_unbounded_or_inconsistent_input() -> None:
    for bad in (
        {"start_date": "2026-01-02", "end_date": "2026-01-01"},
        {"start_date": "2026-01-02"},
        {"limit": 3001},
        {"q": "x" * 81},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            StationFilters.model_validate(bad)
    for bad_risk in ({"scale": 5}, {"event_order": "random"}, {"publication_id": "x"}):
        with pytest.raises(ValidationError):
            RiskFilters.model_validate(bad_risk)
