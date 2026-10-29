"""Public metric names and null semantics must survive schema evolution."""

from apps.api.main import create_app
from citibike.serving.analytics_models import AnalyticsMetrics, AvailabilityMetrics


def test_public_metric_name_and_independent_v1_v2_schemas() -> None:
    schema = create_app().openapi()
    assert "/v1/kpi" in schema["paths"]
    assert "/v2/analytics/kpi" in schema["paths"]
    properties = schema["components"]["schemas"]["AnalyticsMetrics"]["properties"]
    assert "completed_published_rides" in properties
    assert "completed_rides" not in properties
    availability = schema["components"]["schemas"]["AvailabilityMetrics"]["properties"]
    assert {
        "observed_coverage",
        "empty_station_time_share",
        "full_station_time_share",
    } <= availability.keys()


def test_unknown_and_confirmed_zero_have_distinct_serializations() -> None:
    assert AnalyticsMetrics().model_dump()["completed_published_rides"] is None
    assert (
        AnalyticsMetrics(completed_published_rides=0).model_dump()[
            "completed_published_rides"
        ]
        == 0
    )
    assert AvailabilityMetrics().model_dump()["rentable_bikes_time_weighted"] is None
    assert (
        AvailabilityMetrics(rentable_bikes_time_weighted=0).model_dump()[
            "rentable_bikes_time_weighted"
        ]
        == 0
    )
