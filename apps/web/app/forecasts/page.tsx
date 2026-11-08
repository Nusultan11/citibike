import Link from "next/link";
import { evidenceHref, FORECAST_STUDY } from "../../lib/findings";
import {
  type ForecastData,
  forecastState,
  getForecasts,
  message,
  readFilters,
} from "../../lib/operations";
import Chart from "../chart";
import {
  EvidenceNote,
  Icon,
  Kind,
  nyTime,
  PageHead,
  SiteFooter,
  SiteHeader,
  StatePanel,
  short,
  show,
  TableScroll,
  Term,
} from "../ui";

export const dynamic = "force-dynamic";

const STATE = {
  valid: {
    tone: "success",
    label: "действителен",
    title: "Прогноз действителен",
    icon: "check",
  },
  expired: {
    tone: "warning",
    label: "истёк",
    title: "Прогноз недоступен: окно истекло",
    icon: "clock",
  },
  empty: {
    tone: "neutral",
    label: "не опубликован",
    title: "Прогноз не опубликован",
    icon: "database",
  },
  unavailable: {
    tone: "warning",
    label: "недоступен",
    title: "Прогноз недоступен",
    icon: "off",
  },
} as const;

export default async function ForecastsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const input = await searchParams;
  const { params, error } = readFilters(input);
  const station = params.get("station_id");
  const query = new URLSearchParams({ limit: "48" });
  if (station) query.set("station_id", station);
  let data: ForecastData | null = null;
  let failure: { state: "unavailable" | "error"; text: string } | null = error
    ? { state: "error", text: error }
    : null;
  if (!failure) {
    try {
      data = await getForecasts(query);
    } catch (cause) {
      failure = message(cause);
    }
  }
  const pub = data?.publication ?? null;
  const now = new Date();
  const state = data
    ? forecastState(data.status, pub?.valid_until as string | undefined, now)
    : "unavailable";
  const meta = STATE[state];
  const rows = state === "valid" ? (data?.rows ?? []) : [];
  const horizons = Array.from({ length: 24 }, (_, i) => i + 1);
  const value = (type: string, h: number) =>
    rows.find((r) => r.prediction_type === type && r.horizon_h === h);
  return (
    <>
      <SiteHeader current="/forecasts" params={params} />
      <main id="content" className="product-page">
        <PageHead
          eyebrow="F4 · историческое исследование и публикация"
          title="Какая модель выдерживает проверку?"
          lead="Отправления и прибытия прогнозируются отдельно на 1–24 часа. Средняя ошибка деревьев ниже, но рабочим выбором остаётся Seasonal Naive: сегментные проверки не пройдены."
        >
          <Kind kind="forecast" />
        </PageHead>
        <section className="card" aria-labelledby="study-scope">
          <h2 id="study-scope">Что именно оценивали</h2>
          <p className="body-text">
            Когорта — 64 исходных ID станций NYC. Цель — число опубликованных поездок за
            станцию-час, а не скрытый спрос или наличие велосипедов. Backtest использует
            приближённую историческую доступность архивов (+45 дней); фактический момент первой
            публикации не установлен.
          </p>
          <EvidenceNote source="docs/evidence/f4-forecasting.md">
            Зафиксировано 29.10.2026 · история 2024-10–2026-09 · 64 станции · approximate
            retrospective.
          </EvidenceNote>
        </section>

        <div className="section-title">
          <h2>Сравнение моделей</h2>
          <p>
            MAE — среднее абсолютное отклонение в поездках на станцию-час; меньше лучше. Validation
            — среднее четырёх окон, сентябрь — прежний отложенный тест.
          </p>
        </div>
        <div className="grid">
          {FORECAST_STUDY.targets.map((target) => (
            <section className="card span-6" key={target.target} data-study-target={target.target}>
              <h2>{target.target === "departures" ? "Отправления" : "Прибытия"}</h2>
              <TableScroll>
                <table>
                  <thead>
                    <tr>
                      <th>Модель</th>
                      <th className="num">MAE validation</th>
                      <th className="num">MAE сентябрь</th>
                      <th className="num">Сегментов хуже &gt;20%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {target.models.map((m) => (
                      <tr key={m.model}>
                        <th scope="row">{MODEL[m.model]}</th>
                        <td className="num">{show(m.validation, 4)}</td>
                        <td className="num">{show(m.final, 4)}</td>
                        <td className="num">
                          {m.model === "seasonal_naive" ? "база" : m.degraded}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
              <p className="note">
                Сопоставимые bounded-конфигурации; validation: четыре окна. Сентябрь:{" "}
                {show(target.models[0].n)} оценок для каждой модели этой задачи.
              </p>
              <Chart
                label={`MAE по горизонту: ${target.target === "departures" ? "отправления" : "прибытия"}, сентябрь 2026`}
                spec={{
                  kind: "line",
                  unit: "поездок MAE",
                  categories: horizons.map((h) => `+${h} ч`),
                  series: target.models
                    .filter((m) => m.model === "seasonal_naive" || m.model === "lightgbm_bounded")
                    .map((m) => ({ name: MODEL[m.model], values: [...m.horizons] })),
                }}
              />
              <EvidenceNote source="docs/evidence/f4-forecasting.json">
                29.10.2026 · 64 ID · validation 2025-06/09/12 и 2026-03; final 2026-09 · ось X:
                горизонт, Y: MAE.
              </EvidenceNote>
            </section>
          ))}
        </div>
        <section className="callout tone-warning" style={{ marginTop: 16 }}>
          <Icon name="alert" size={17} />
          <div>
            <h2>Почему champion — Seasonal Naive</h2>
            <p>
              Для замены нужны выигрыш в большинстве окон, отсутствие деградации &gt;20% в
              критических сегментах, допустимые ресурсы и доказанная доступность признаков. LightGBM
              и CatBoost выигрывают в 3/4 окон, но проваливают сегментный gate. Низкий общий MAE не
              разрешает promotion.
            </p>
            <EvidenceNote source="docs/evidence/f4-forecasting.json">
              29.10.2026 · frozen selection по validation · строгая историческая доступность (PIT) и
              operational promotion NOT VERIFIED; champion не менялся.
            </EvidenceNote>
          </div>
        </section>
        <details className="disclosure card" style={{ marginTop: 16 }}>
          <summary>Rolling backtest: четыре окна и ошибки по моделям</summary>
          <p className="body-text">
            Каждое окно обучалось только на предшествующих данных; embargo — 24 часа. Этот протокол
            ограничен предположением о публикации архивов и не доказывает strict PIT.
          </p>
          {FORECAST_STUDY.targets.map((target) => (
            <div key={target.target}>
              <h3>{TARGET[target.target]}</h3>
              <TableScroll>
                <table>
                  <thead>
                    <tr>
                      <th>Окно origin (UTC)</th>
                      {target.models.map((m) => (
                        <th key={m.model} className="num">
                          {MODEL[m.model]}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {FORECAST_STUDY.folds.map((f, i) => (
                      <tr key={f.start}>
                        <td>
                          {f.start.slice(0, 10)} → {f.end.slice(0, 10)}
                        </td>
                        {target.models.map((m) => (
                          <td key={m.model} className="num">
                            {show(m.folds[i], 4)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            </div>
          ))}
          <EvidenceNote source="docs/evidence/f4-forecasting.json">
            29.10.2026 · 64 ID · четыре rolling-origin окна 2025-06–2026-03; MAE в поездках.
          </EvidenceNote>
        </details>
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Где средняя ошибка скрывает проблему</h2>
          <p className="body-text">
            На нулевых и обычных часах score leader лучше базы, на пиках ≥20 поездок — хуже и
            систематически недооценивает поток. Это отдельный pooled validation-анализ, не
            сентябрьский тест.
          </p>
          <TableScroll>
            <table>
              <thead>
                <tr>
                  <th>Задача · сегмент</th>
                  <th className="num">N</th>
                  <th className="num">MAE SN</th>
                  <th className="num">MAE leader</th>
                  <th className="num">Bias leader</th>
                </tr>
              </thead>
              <tbody>
                {FORECAST_STUDY.targets.flatMap((t) =>
                  t.slices
                    .filter((r) => r.name !== "seasonal_naive")
                    .map((r) => (
                      <tr key={`${t.target}-${r.slice}`}>
                        <th scope="row">
                          {TARGET[t.target]} · {SLICE[r.slice]}
                        </th>
                        <td className="num">{show(r.n)}</td>
                        <td className="num">
                          {show(
                            t.slices.find((s) => s.name === "seasonal_naive" && s.slice === r.slice)
                              ?.mae,
                            4,
                          )}
                        </td>
                        <td className="num">{show(r.mae, 4)}</td>
                        <td className="num">{show(r.bias, 4)}</td>
                      </tr>
                    )),
                )}
              </tbody>
            </table>
          </TableScroll>
          <EvidenceNote source="docs/evidence/f4-forecasting.json">
            Corrective validation 30.10.2026 · 64 ID · leader: CatBoost default для отправлений,
            bounded для прибытий; bias = prediction − actual.
          </EvidenceNote>
        </section>
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Что дали признаки и варианты loss</h2>
          <p className="body-text">
            Удаление календаря заметно ухудшает CatBoost; удаление station ID и профиля спроса даёт
            меньший средний эффект. Это ablation на тех же четырёх validation-окнах, без нового
            независимого теста.
          </p>
          <TableScroll>
            <table>
              <thead>
                <tr>
                  <th>CatBoost bounded · MAE</th>
                  <th className="num">Полный</th>
                  <th className="num">Без календаря</th>
                  <th className="num">Без station ID</th>
                  <th className="num">Без профиля</th>
                </tr>
              </thead>
              <tbody>
                {FORECAST_STUDY.targets.map((t) => {
                  const full =
                    t.models.find((m) => m.model === "catboost_bounded")?.validation ?? 0;
                  return (
                    <tr key={t.target}>
                      <th scope="row">{TARGET[t.target]}</th>
                      <td className="num">{show(full, 4)}</td>
                      {t.ablation.map((a) => (
                        <td key={a.group} className="num">
                          {show(full + a.delta, 4)}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableScroll>
          <EvidenceNote source="docs/evidence/f4-forecasting.json">
            30.10.2026 · 64 ID · одинаковые строки в парных ablation; среднее MAE четырёх окон.
          </EvidenceNote>
          <p className="body-text">
            Последующий exploratory A/B: 176 trials, 44 сравнения групп — 40 inconclusive, 3
            ухудшения, 1 улучшение. В decision research (872 trials) ни один вариант не прошёл
            критические сегменты. Эти результаты не меняют frozen selection.
          </p>
          <EvidenceNote source="docs/evidence/f4-feature-research.json">
            02.11.2026 · validation-only, 64 ID · эффект групп не установлен.{" "}
            <a href={evidenceHref("docs/evidence/f4-decision-research.json")}>
              Decision research ↗
            </a>
          </EvidenceNote>
          <p className="body-text">
            Poisson и hurdle проверялись как разные модели счётчиков. Сравнение семейств
            одновременно меняет модель и обучение; оно не изолирует эффект loss. Контролируемая
            loss-ablation при фиксированных остальных условиях — <strong>NOT VERIFIED</strong>.
          </p>
          <EvidenceNote source="docs/evidence/f4-decision-research.json" status="NOT VERIFIED">
            02.11.2026 · H5 hurdle · exploratory, без слепой предрегистрации и нового независимого
            периода.
          </EvidenceNote>
        </section>
        <section className="card" style={{ marginTop: 16 }} data-finding="f4-diagnostics">
          <h2>593 диагностических события — не 593 инцидента</h2>
          <p className="body-text">
            Из 1 126 сырых сигналов spike / long zero / CUSUM получены 593 дедуплицированных
            события. Контрольный протокол (инъекции и отрицательные случаи) прошёл 12/12 проверок.
            Независимой разметки реальных сбоев нет: Precision@K и реальная доля ложных тревог
            неизвестны; гипотеза контекстного признака — inconclusive.
          </p>
          <EvidenceNote source="docs/evidence/f4-ml-diagnostics.json" status="NOT VERIFIED">
            03.11.2026 · контекстные station-hour ретроспективной когорты; physical incidents не
            подтверждены, final test не пересчитывался.
          </EvidenceNote>
        </section>

        <div className="section-title">
          <h2>Текущая публикация</h2>
          <p>Её доступность проверяется отдельно от результатов исследования выше.</p>
        </div>
        <form
          className="filterbar static"
          action="/forecasts"
          method="get"
          aria-label="Станция прогноза"
        >
          <div className="filter-row">
            <label className="field grow">
              Станция (cohort F4)
              <input
                name="station_id"
                maxLength={80}
                defaultValue={station ?? ""}
                placeholder="Пусто — первая станция cohort"
              />
            </label>
            <div className="filter-actions">
              <Link className="btn btn-ghost" href="/forecasts">
                Сбросить
              </Link>
              <button className="btn btn-primary" type="submit">
                Показать
              </button>
            </div>
          </div>
        </form>
        {failure ? (
          <StatePanel state={failure.state} title="Публикация недоступна">
            <p data-forecast-page={failure.state}>{failure.text}</p>
          </StatePanel>
        ) : (
          <>
            <section
              className={`callout tone-${meta.tone}`}
              data-forecast-page={state}
              role="status"
            >
              <Icon name={meta.icon} size={17} />
              <div>
                <h2>{meta.title}</h2>
                <p>
                  {state === "expired"
                    ? `Окно закрылось ${nyTime(pub?.valid_until)}. Значения скрыты.`
                    : state === "valid"
                      ? "Оценки опубликованных поездок; не факты и не скрытый спрос."
                      : "Нет действительных значений для выбранной станции."}
                </p>
              </div>
            </section>
            {pub && (
              <details className="disclosure card" style={{ marginTop: 16 }}>
                <summary>Источник и срок публикации · {short(pub.publication_id)}</summary>
                <dl className="facts">
                  <dt>Publication</dt>
                  <dd className="hash">{String(pub.publication_id)}</dd>
                  <dt>Forecast origin</dt>
                  <dd>{nyTime(pub.forecast_origin_utc)}</dd>
                  <dt>Valid until</dt>
                  <dd data-forecast-valid-until={String(pub.valid_until)}>
                    {nyTime(pub.valid_until)}
                  </dd>
                  <dt>Bundle</dt>
                  <dd className="hash">{String(pub.model_bundle_id)}</dd>
                  <dt>Data as-of</dt>
                  <dd>
                    {nyTime(pub.data_as_of)} · {String(pub.data_as_of_kind)}
                  </dd>
                  <dt>Ограничения</dt>
                  <dd>{String(pub.limitations)}</dd>
                </dl>
              </details>
            )}
            {state === "valid" ? (
              <section
                className="card"
                style={{ marginTop: 16 }}
                data-forecast-values={rows.length}
              >
                <div className="card-head">
                  <div>
                    <h2>
                      <Term name="forecast">
                        {station ? `Станция ${station}` : `Станция ${rows[0]?.station_id ?? "N/A"}`}
                      </Term>
                    </h2>
                    <p className="card-sub">Оценка поездок по горизонту 1–24 ч; не факт.</p>
                  </div>
                  <Kind kind="forecast" />
                </div>
                <Chart
                  label="Прогноз отправлений и прибытий по горизонту"
                  spec={{
                    kind: "line",
                    unit: "поездок",
                    categories: horizons.map((h) => `+${h} ч`),
                    series: ["departures", "arrivals"].map((type) => ({
                      name: type === "departures" ? "Отправления" : "Прибытия",
                      values: horizons.map((h) => value(type, h)?.predicted_value ?? null),
                    })),
                  }}
                />
                <details className="disclosure" open>
                  <summary>Таблица по горизонтам</summary>
                  <TableScroll>
                    <table>
                      <thead>
                        <tr>
                          <th>Горизонт</th>
                          <th>Целевой час (NY)</th>
                          <th className="num">Отправления</th>
                          <th className="num">Прибытия</th>
                        </tr>
                      </thead>
                      <tbody>
                        {horizons.map((h) => (
                          <tr key={h}>
                            <td>+{h} ч</td>
                            <td>{nyTime(value("departures", h)?.target_hour_utc)}</td>
                            {(["departures", "arrivals"] as const).map((type) => {
                              const point = value(type, h);
                              return (
                                <td key={type} className="num">
                                  {point?.status === "ok" ? (
                                    show(point.predicted_value, 2)
                                  ) : (
                                    <span className="badge warn">
                                      unavailable: {point?.reason ?? "нет строки"}
                                    </span>
                                  )}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableScroll>
                </details>
              </section>
            ) : null}
          </>
        )}
      </main>
      <SiteFooter />
    </>
  );
}
const TARGET = { departures: "Отправления", arrivals: "Прибытия" };
const MODEL = {
  seasonal_naive: "Seasonal Naive",
  poisson_bounded: "Poisson",
  lightgbm_bounded: "LightGBM",
  catboost_bounded: "CatBoost",
};
const SLICE = {
  "actual:zero": "0 поездок",
  "actual:nonzero": ">0 поездок",
  "actual:peak_ge20": "≥20 поездок",
};
