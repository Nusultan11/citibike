from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def forecast_workflow(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    from citibike.features.dataset import build
    from citibike.forecasting.experiment import backtest, final_test, select
    from citibike.forecasting.inference import predict, train
    from tests.fixtures_forecasting import gold_fixture

    root = tmp_path_factory.mktemp("forecasting")
    gold = gold_fixture(root)
    dataset = build(root, gold, station_count=4, smoke=True)
    experiment = backtest(root, dataset)
    selection = select(root, experiment)
    final = final_test(root, selection)
    model = train(root, selection)
    forecast = predict(root, model)
    return dict(
        root=root,
        gold=gold,
        dataset=dataset,
        experiment=experiment,
        selection=selection,
        final=final,
        model=model,
        forecast=forecast,
    )
