"""Main CI: publish a controlled forecast and inspect real HTTP; no real claims."""

import tempfile
from pathlib import Path

import httpx

from citibike.features.dataset import build
from citibike.forecasting.experiment import backtest, final_test, select
from citibike.forecasting.inference import predict, train
from citibike.serving.forecast_publish import publish
from tests.fixtures_forecasting import gold_fixture


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        dataset = build(root, gold_fixture(root), station_count=4, smoke=True)
        selection = select(root, backtest(root, dataset))
        final_test(root, selection)
        forecast = predict(root, train(root, selection))
        version = publish(forecast)
        assert publish(forecast) == version
        with httpx.Client(timeout=30) as client:
            response = client.get(
                "http://127.0.0.1:8000/v3/forecasts",
                params={"publication_id": version, "limit": 200},
            )
            response.raise_for_status()
            body = response.json()
            assert body["total_rows"] == 192 and len(body["rows"]) == 192
            assert (
                body["publication"]["accuracy_evidence_mode"]
                == "approximate retrospective"
            )
            assert all(p["predicted_value"] >= 0 for p in body["rows"])
        print(
            "Controlled F4 models/strict batch/PG/HTTP smoke passed; "
            "not official-data metrics"
        )


if __name__ == "__main__":
    main()
