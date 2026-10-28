# Шпаргалка 04 — ML forecasting и оценка экспериментов

**Когда открывать:** Feature Engineering, backtesting, настройка моделей, MLflow, диагностика ошибок. Формальные требования: [../ML_SPEC.md](../ML_SPEC.md).

## 1. Каркас одного честного эксперимента

1. Зафиксировать `target`, `origin`, горизонт, доступную историю и конкретный набор станций.
2. Сформировать features **на as-of origin**, включая реальные даты публикации данных. Хранить manifest/хэш данных и код.
3. Train → несколько rolling validation окон → final untouched test; train-only fit для трансформаций.
4. Обучить seasonal baseline; затем Poisson → LightGBM → CatBoost → N-HiTS по необходимости.
5. Сравнить MAE, WAPE (не при нулевом denominator), bias, RMSE и сегменты ошибок.
6. Проанализировать худшие станции, сезонность, nonstationarity и реальную доступность признаков.
7. Сохранить run, конфиг, checksum, время/RAM/GPU и прогнозы. Отрицательные counts недопустимы.
8. Принять/отклонить candidate с объяснением улучшения **относительно baseline**, не только абсолютного score.

## 2. Конкретные ловушки

| Ловушка | Как обнаружить |
|---|---|
| Random train/test split для временных рядов | Проверить время `origin` и `target` каждого fold |
| `lag_1h` из опубликованного позже архива | Проверить `available_at <= origin` для feature |
| Историческая фактическая погода вместо forecast | Хранить `forecast_issue_time` и `lead_time` |
| Станция не существовала в выбранном периоде | Использовать историческую dimension и availability |
| Zero vs missing trip hour | Проверить coverage источника и станции |
| Rolling feature захватывает target | Shift перед rolling, unit-test на границу окна |
| Результат слишком хорош на тесте | Аудит leakage, сравнение с trivial baseline, разрезы |
| «Лучшая модель» выбрана после многократного взгляда на test | Отложить test до завершения выбора |

## 3. Основные метрики

`MAE = mean(|y - yhat|)`; `RMSE = sqrt(mean((y-yhat)^2))`; `Bias = mean(yhat-y)`; `WAPE = sum(|y-yhat|)/sum(y)` при `sum(y)>0`. Смотреть агрегаты по горизонту и станциям, иначе модель может быть слабой на тихих станциях.

Для риска нужны отдельные calibration/precision/recall и бизнес-сценарии. **Accuracy поездок нельзя считать моментально**, пока не поступил опубликованный true target.

## 4. Короткий чек-лист tuning

- Эксперимент A: дефолт и базовые признаки.
- Эксперимент B: ограниченный бюджет Optuna по 3–6 влиятельным параметрам, воспроизводимые folds.
- Эксперимент C: stress test перспективной модели — разные сезоны/станции/неполные входы.
- Стоп: нет прогресса против baseline/простого кандидата, расходы растут — идём дальше.
- По возможности единая глобальная модель на многих станциях, затем сравнение с local baseline.

## Материалы

- scikit-learn TimeSeriesSplit — https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- scikit-learn common pitfalls (leakage) — https://scikit-learn.org/stable/common_pitfalls.html
- Nixtla MLForecast — https://nixtlaverse.nixtla.io/mlforecast/
- Nixtla NeuralForecast / N-HiTS — https://nixtlaverse.nixtla.io/neuralforecast/models.nhits.html
- LightGBM parameters — https://lightgbm.readthedocs.io/en/latest/Parameters.html
- CatBoost docs — https://catboost.ai/en/docs/
- Optuna — https://optuna.readthedocs.io/en/stable/
- MLflow Model Registry — https://mlflow.org/docs/latest/ml/model-registry/
- Open-Meteo historical forecasts — https://open-meteo.com/en/docs/historical-forecast-api
