# Шпаргалка 01 — источники и предметная область

**Когда открывать:** перед загрузкой данных, изменением источника, построением бизнес-сценариев или публичной публикацией. Контракты находятся в [../DATA_CONTRACTS.md](../DATA_CONTRACTS.md).

## Официальные источники

| Ресурс | Ссылка | На что смотреть |
|---|---|---|
| Citi Bike System Data | https://citibikenyc.com/system-data | Современная/старая схема поездок, фильтры, обновления |
| Архив Trip History | https://s3.amazonaws.com/tripdata/index.html | Архивы, имена периодов, несколько CSV в ZIP |
| GBFS discovery | https://gbfs.citibikenyc.com/gbfs/2.3/gbfs.json | `data.en.feeds`, `last_updated`, `ttl`, версия; URL дочерних feed получать отсюда |
| GBFS standard | https://www.gbfs.org/documentation/ | Семантика station_status/info, версии 2.3/3.0, поля и freshness |
| NYC DOT / bike-share | https://www.nyc.gov/html/dot/html/bicyclists/bikestats.shtml | Независимые агрегированные отчёты / внешний контекст |
| Citi Bike Operating Reports | https://citibikenyc.com/system-data/operating-reports | Возможные референсы для сверки объёмов |
| Open-Meteo historical | https://open-meteo.com/en/docs/historical-weather-api | Исторические наблюдения/reanalysis для EDA |
| Open-Meteo historical forecast | https://open-meteo.com/en/docs/historical-forecast-api | Прогнозы прошлых дат — различать продукт и coverage |
| Open-Meteo previous runs | https://open-meteo.com/en/docs/previous-runs-api | Исторические прогнозы для конкретного lead-time |

**Чек-лист исходных данных:** скачать только через предоставленные/разрешённые источником интерфейсы; проверить лицензию; manifest checksum/size/time; посчитать все CSV в архиве; сохранить raw; выяснить NYC и `JC`; никогда не считать доступным будущий архив; GBFS собирать самим, если нужна история состояния.

## Лицензия и открытая публикация

**Обязательно читать:** https://citibikenyc.com/data-sharing-policy

- Не публиковать публично исходные наборы как отдельный перезаливаемый датасет; не маскировать републикацию через Git LFS.
- Не заявлять о сотрудничестве/одобрении оператором, не использовать логотип и товарный знак в собственном брендинге без разрешения.
- Не пытаться деанонимизировать пользователей и связывать их с личными данными.
- Условия запрещают неавторизованные методы извлечения данных с сайта; придерживаться официального интерфейса и проверить правила повторного доступа.
- Для GitHub выкладывать код, воспроизводимый pipeline, схематические данные/агрегированные результаты только после отдельной проверки разрешённости.

## Исследования: что именно у них брать

1. **Jin et al. (2022), simulation & maintenance on Citi Bike** — https://www.sciencedirect.com/science/article/pii/S1569190X21001209  
   Полезно для проектирования симулятора rebalancing и сравнения стратегий. Не переносить их показатели как наши.
2. **Liu et al. (2024), Data-Driven Optimization for Static Rebalancing** — https://pubsonline.informs.org/doi/10.1287/ijoc.2022.0182  
   Изучить декомпозицию routing, demand substitution, оценку результатов на Citi Bike. Есть ссылка на сопровождающий код в статье.
3. **Maleki et al. (2023), Supervised Learning-Based Simulation** — https://www.sciencedirect.com/science/article/pii/S0957417423014859  
   Понять, когда ML-суррогат может ускорить тяжёлые симуляции — только при доказанной необходимости.
4. **Bike sharing: value of demand forecasts (2025)** — https://www.sciencedirect.com/science/article/pii/S2950105925000026  
   Исследует, как точность спроса влияет на качество решений; читать при выборе бизнес-метрик.

## Что нельзя смешивать

- `published ride count` и `latent demand`.
- «нет данных» и «на станции 0 велосипедов».
- Историческую погоду и прогноз, доступный на старую дату.
- Математическую экономию симулятора и доказанную денежную выгоду компании.
- Число поездок и выручку, без данных о тарифах и реальном расчёте оплаты.
