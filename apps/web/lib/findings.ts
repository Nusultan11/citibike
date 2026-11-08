// Recorded research findings, each with one primary page. Every number is copied
// from a committed evidence file or ROADMAP §6 entry; nothing here is computed live.
// Status follows AGENTS.md: VERIFIED (independent check), LIMITED (verified with a
// stated restriction), NOT_VERIFIED (evidence insufficient).

export type FindingStatus = "verified" | "limited" | "not_verified";
export type FindingArea = "data" | "analytics" | "forecast" | "scenario";

export type Finding = {
  id: string;
  area: FindingArea;
  status: FindingStatus;
  /** Short headline value as recorded; never recomputed in the UI. */
  value: string;
  title: string;
  takeaway: string;
  limitation: string;
  source: string;
  /** Additional evidence when the statement spans more than one study. */
  relatedSources?: readonly string[];
  recorded: string;
};

export const FINDING_AREAS: Record<FindingArea, string> = {
  data: "Данные и качество",
  analytics: "Аналитика спроса",
  forecast: "Прогнозы F4",
  scenario: "Сценарии F5",
};

// Pin recorded claims to the reviewed evidence, independent of future main updates.
export const EVIDENCE_REVISION = "107be3ca7221801f8f14bdf2dc1e56465a94b013";
export function evidenceHref(source: string) {
  return `https://github.com/Nusultan11/citibike/blob/${EVIDENCE_REVISION}/${source}`;
}

export const RESEARCH_POPULATIONS = {
  features: ["cohort64", "cohort128"],
  decision: ["cohort64", "cohort128", "cohort256", "cohort256x4"],
} as const;

export const FINDINGS: readonly Finding[] = [
  {
    id: "f2-reconciliation",
    area: "data",
    status: "verified",
    value: "92 076 567",
    title: "24 месяца истории сведены построчно",
    takeaway:
      "Из 92 085 994 входных строк (2024-10…2026-09) приняты 92 076 567; 8 886 невалидных, 483 точных дубликата и 58 конфликтующих ID изолированы. Независимый пересчёт — 0 расхождений.",
    limitation: "Охват публичного источника, не весь спрос и не скрытые отказы.",
    source: "docs/evidence/f2-analysis.md",
    recorded: "2026-10-28",
  },
  {
    id: "f2-gaps",
    area: "data",
    status: "verified",
    value: "54 ч",
    title: "Пробелы источника — это не ноль",
    takeaway:
      "54 городских UTC-часа без опубликованных поездок, больше всего в январе (18) и феврале 2026 (32). Такие часы остаются «нет наблюдений», а не «нет спроса».",
    limitation: "Причина пробелов в источнике не установлена.",
    source: "docs/evidence/f2-analysis.md",
    recorded: "2026-10-28",
  },
  {
    id: "f2-sparsity",
    area: "data",
    status: "verified",
    value: "49,7%",
    title: "Станция-час разрежен и с длинным хвостом",
    takeaway:
      "Почти половина допустимых station-hour отправлений — ноль, ещё ~17% — одна поездка; максимум 216 отправлений / 221 прибытие за час. Пики сохранены, не обрезаны.",
    limitation: "Ноль — опубликованная метка, не статус работы станции.",
    source: "docs/evidence/f2-analysis.md",
    recorded: "2026-10-29",
  },
  {
    id: "f2-weekly",
    area: "data",
    status: "limited",
    value: "Вт 17:00",
    title: "Недельный пик — будний вечер",
    takeaway:
      "Максимальная средняя интенсивность: отправления во вторник в 17:00 NY (6,22 на станцию-час), прибытия — в 18:00. Минимум сезона — февраль 2026.",
    limitation: "Описательная ассоциация; только два годовых цикла, сезон и тренд не разделены.",
    source: "docs/evidence/f2-analysis.md",
    recorded: "2026-10-29",
  },
  {
    id: "da-annual",
    area: "analytics",
    status: "verified",
    value: "−2,8%",
    title: "Год к году поездок немного меньше",
    takeaway:
      "2024-10…2025-09: 46 697 958 поездок → 2025-10…2026-09: 45 378 096. Доля электровелосипедов выросла с 69,6% до 72,3%; месячный Gini по наблюдаемым ID — 0,58–0,62.",
    limitation: "Факт опубликованных поездок; причины изменения не исследовались.",
    source: "docs/evidence/f4-decision-research.json",
    recorded: "2026-11-02",
  },
  {
    id: "da-id-split",
    area: "analytics",
    status: "limited",
    value: "105 / 105",
    title: "Топ net flow искажали двойные ID",
    takeaway:
      "Все 40 станций исходного топа |net flow| оказались парами dddd.d / dddd.d0: одно название, < 1 м между точками, причина — формат raw-архива. В историческом decision research пары исключены из рейтингов. Текущий интерфейс показывает исходные ID раздельно с пометками; Silver не объединяется.",
    limitation: "Тождество подтверждено lineage, оператором — нет; маппинг ждёт решения владельца.",
    source: "docs/evidence/f4-decision-research.json",
    recorded: "2026-11-02",
  },
  {
    id: "f4-final",
    area: "forecast",
    status: "limited",
    value: "1,58 vs 1,99",
    title: "Деревья точнее в среднем, но не надёжнее",
    takeaway:
      "На отложенном сентябре 2026 MAE LightGBM 1,58 / 1,60 против Seasonal Naive 1,99 / 1,96 (отправления / прибытия), но ни один кандидат не прошёл заранее заданный gate по деградации сегментов. Рабочая модель — Seasonal Naive.",
    limitation:
      "Approximate retrospective; operational promotion NOT VERIFIED; когорта 64 станции.",
    source: "docs/evidence/f4-forecasting.md",
    recorded: "2026-10-29",
  },
  {
    id: "f4-research",
    area: "forecast",
    status: "limited",
    value: "40 / 44",
    title: "Новые признаки не дали устойчивого выигрыша",
    takeaway:
      "176 trials A/B: из 44 парных сравнений групп признаков 40 inconclusive, 3 хуже, 1 лучше — эффекты групп не заявляются. В 872 trials decision research у каждого варианта > 20% деградации (ночные горизонты, сильный дождь, низкая активность).",
    limitation:
      "Exploratory validation-only; слепой пре-регистрации и нового независимого теста нет.",
    source: "docs/evidence/f4-feature-research.json",
    relatedSources: ["docs/evidence/f4-decision-research.json"],
    recorded: "2026-11-02",
  },
  {
    id: "f4-diagnostics",
    area: "forecast",
    status: "not_verified",
    value: "593",
    title: "Аномалии найдены, но не подтверждены",
    takeaway:
      "Контекстная диагностика station-hour дала 593 дедуплицированных события (spike / long zero / CUSUM); на контрольных сценариях (инъекции и отрицательные контроли) 12/12 PASS. Гипотеза о контекстном признаке — inconclusive.",
    limitation: "Независимой разметки нет: Precision@K и реальная доля ложных тревог NOT VERIFIED.",
    source: "docs/evidence/f4-ml-diagnostics.json",
    recorded: "2026-11-03",
  },
  {
    id: "f5-replay",
    area: "scenario",
    status: "limited",
    value: "9 / 9",
    title: "Перевозки в replay не окупились",
    takeaway:
      "В recorded-as-of replay no-action не хуже эвристики и MILP по loss + λ·cost во всех 9 ячейках масштаба × порядка событий; поэтому перевозки не рекомендуются. Независимый oracle — 27/27 objective, 0 расхождений.",
    limitation:
      "Только 3 из 64 станций имели годное GBFS-состояние; это архивная симуляция, не live-риск.",
    source: "docs/evidence/f6-product.json",
    recorded: "2026-11-03",
  },
];

/** One primary destination per recorded finding; other pages link instead of repeating it. */
export function findingPage(
  finding: Finding,
): "/analytics" | "/stations" | "/forecasts" | "/scenarios" {
  if (finding.id === "da-id-split") return "/stations";
  if (finding.area === "forecast") return "/forecasts";
  if (finding.area === "scenario") return "/scenarios";
  return "/analytics";
}

export function findingPopulation(finding: Finding) {
  if (finding.id === "f4-research")
    return `A/B: ${RESEARCH_POPULATIONS.features.join(" / ")}; decision research: ${RESEARCH_POPULATIONS.decision.join(" / ")}; exploratory validation-only`;
  if (finding.area === "forecast")
    return "Когорта 64 source station ID; ретроспективные station-hour, горизонты 1–24 ч";
  if (finding.area === "scenario")
    return "3/64 станции; recorded as-of 2026-10-30 06:36 UTC; 4 часа симуляции";
  return "NYC, публичные поездки 2024-10–2026-09; source station ID без объединения";
}

// Small, checked excerpt of committed f4-forecasting.json. No experiment runs in the UI.
export const FORECAST_STUDY = {
  recorded: "2026-10-29",
  folds: [
    {
      start: "2025-06-01T00:00:00+00:00",
      end: "2025-06-15T00:00:00+00:00",
    },
    {
      start: "2025-09-01T00:00:00+00:00",
      end: "2025-09-15T00:00:00+00:00",
    },
    {
      start: "2025-12-01T00:00:00+00:00",
      end: "2025-12-15T00:00:00+00:00",
    },
    {
      start: "2026-03-01T00:00:00+00:00",
      end: "2026-03-15T00:00:00+00:00",
    },
  ],
  targets: [
    {
      target: "departures",
      champion: "seasonal_naive",
      leader: "catboost_default",
      models: [
        {
          model: "seasonal_naive",
          validation: 1.75443157344928,
          folds: [1.774301354076635, 1.9805177616602254, 1.9329279969360398, 1.3299791811242192],
          final: 1.9902843231411789,
          n: 44773,
          degraded: 0,
          horizons: [
            2.1272727272727274, 1.9163987138263665, 1.6131820637493246, 1.2099837221920782,
            0.7365010799136069, 0.5496760259179265, 0.3842417701025364, 0.3710895361380798,
            0.5655913978494623, 1.094855305466238, 1.967880085653105, 2.5858747993579456,
            2.443850267379679, 2.0978609625668447, 2.3522180652057725, 2.388888888888889,
            2.563568376068376, 2.7148958889482113, 2.9450373532550693, 2.8928, 3.2288,
            3.3630539241857984, 2.9663641217298453, 2.5938001068947085,
          ],
        },
        {
          model: "poisson_bounded",
          validation: 1.7682853819170856,
          folds: [2.2212835133312643, 2.0612560212318183, 1.2960995949744711, 1.494502398130789],
          final: 2.0795514437699043,
          n: 44773,
          degraded: 40,
          horizons: [
            1.7936048066615855, 1.7349115083627225, 1.452214754835351, 1.1755898106480094,
            1.0476321810056821, 1.011675579889113, 0.9490558575567883, 0.8792111079828944,
            0.9501814453284763, 1.3091057410624771, 2.134203995637597, 3.036369547532296,
            2.2659183089954036, 2.0406350802578195, 2.209116313367081, 2.2517807575067454,
            2.376616247400059, 2.4751362494992017, 2.783576359619259, 2.933954560215177,
            3.9700891712651565, 4.020933413052322, 2.8922535579234125, 2.132930430823219,
          ],
        },
        {
          model: "lightgbm_bounded",
          validation: 1.5664882742414963,
          folds: [2.1225247003257834, 1.6772714971169678, 1.3661970360968816, 1.0999598634263528],
          final: 1.5778341849045467,
          n: 44773,
          degraded: 12,
          horizons: [
            1.5154022923681, 1.4872920823509121, 1.233743299587779, 0.8422716658226594,
            0.5995497242233092, 0.46323083445044727, 0.36848687285368714, 0.35387844912355626,
            0.5466037014175885, 0.9777871915113592, 1.6092416144502564, 2.1770932197947714,
            1.8517975366545696, 1.723011970328395, 1.885495549959779, 1.8973999927607228,
            2.033121147627552, 2.1373135795670133, 2.3728707286975577, 2.4172092308191204,
            2.6617563332728746, 2.6853407301669674, 2.174547161814038, 1.779633509921927,
          ],
        },
        {
          model: "catboost_bounded",
          validation: 1.524501841633192,
          folds: [2.138919930696578, 1.6360182733784183, 1.253119615951834, 1.0699495465059374],
          final: 1.6107795013923962,
          n: 44773,
          degraded: 14,
          horizons: [
            1.5230007296833048, 1.4803617029757212, 1.2268269149334838, 0.8480068815810703,
            0.6089863979671503, 0.48952429090856714, 0.4087846302675978, 0.39167711120032705,
            0.5712418145084075, 0.9912942683145431, 1.6290306512011186, 2.1339817657444677,
            1.857793354897995, 1.7592584402075906, 1.951056086195851, 1.9686887023557453,
            2.105422506374463, 2.179485061039563, 2.424458649001594, 2.4814814103407126,
            2.7191857467493605, 2.727531808065671, 2.2887757821462293, 1.8172532521250822,
          ],
        },
      ],
      slices: [
        {
          name: "catboost_default",
          slice: "actual:zero",
          n: 37217,
          mae: 0.541626024836044,
          bias: 0.541626024836044,
        },
        {
          name: "catboost_default",
          slice: "actual:nonzero",
          n: 40089,
          mae: 2.3764307837161667,
          bias: -1.4427639665696592,
        },
        {
          name: "catboost_default",
          slice: "actual:peak_ge20",
          n: 1052,
          mae: 14.511030351908634,
          bias: -14.39294064703524,
        },
        {
          name: "seasonal_naive",
          slice: "actual:zero",
          n: 37217,
          mae: 0.5886557218475428,
          bias: 0.5886557218475428,
        },
        {
          name: "seasonal_naive",
          slice: "actual:nonzero",
          n: 40089,
          mae: 2.831225523210856,
          bias: -0.7959540023447829,
        },
        {
          name: "seasonal_naive",
          slice: "actual:peak_ge20",
          n: 1052,
          mae: 11.923954372623575,
          bias: -11.085551330798479,
        },
      ],
      ablation: [
        {
          group: "calendar",
          delta: 0.2603923677587477,
        },
        {
          group: "station_identity",
          delta: 0.014883069431042395,
        },
        {
          group: "demand_profile",
          delta: 0.09310733076998223,
        },
      ],
    },
    {
      target: "arrivals",
      champion: "seasonal_naive",
      leader: "catboost_bounded",
      models: [
        {
          model: "seasonal_naive",
          validation: 1.747921847905371,
          folds: [1.781964851627773, 1.9449944412091693, 1.9346993489084643, 1.330028749876078],
          final: 1.9609586134500703,
          n: 44773,
          degraded: 0,
          horizons: [
            2.167914438502674, 1.932475884244373, 1.586709886547812, 1.1975040694519805,
            0.816414686825054, 0.5545356371490281, 0.41068537506745817, 0.3581445523193096,
            0.49946236559139784, 0.9833869239013934, 1.724304068522484, 2.407704654895666,
            2.323529411764706, 2.0919786096256683, 2.316942811330839, 2.386217948717949,
            2.552350427350427, 2.785371062466631, 2.9439701173959447, 2.8053333333333335,
            3.3141333333333334, 3.2808328884143085, 3.002669514148425, 2.5285943345804385,
          ],
        },
        {
          model: "poisson_bounded",
          validation: 1.7821360382238685,
          folds: [2.28012271801116, 2.078427476481835, 1.295044846825647, 1.4749491115768318],
          final: 2.0951977641797654,
          n: 44773,
          degraded: 47,
          horizons: [
            1.8602027803733707, 1.6291295803711443, 1.4009438069512299, 1.221443355532095,
            1.107770635729307, 1.0006648727518563, 0.9546670844071196, 0.9293615061608587,
            1.0262321852454381, 1.3636191969717852, 1.832551908368797, 2.543011044339402,
            2.407501499455219, 2.1218213207932224, 2.2029165459571614, 2.3461088785686006,
            2.4172922738973313, 2.5314720558616632, 2.6975730155029853, 2.8998973186036148,
            4.399087868074704, 4.0355589822464895, 3.074740011830738, 2.1970961478973794,
          ],
        },
        {
          model: "lightgbm_bounded",
          validation: 1.5683834200551596,
          folds: [2.1380675632557606, 1.6552730889152807, 1.3940701241972275, 1.0861229038523699],
          final: 1.5988198365695492,
          n: 44773,
          degraded: 16,
          horizons: [
            1.6054964998800854, 1.4055993943125116, 1.1980347505548772, 0.9198498154908998,
            0.6777509561402644, 0.4989672515065522, 0.4201867078835753, 0.37360440099540365,
            0.5244188908030266, 0.9339654791623478, 1.4830995311662456, 2.10811537296995,
            2.0032294432167617, 1.778914730193969, 1.9394758326525339, 2.0125424360825956,
            2.0455822181894097, 2.157793794063491, 2.3099430992725334, 2.330678918186797,
            2.7464905324972197, 2.5879859394060163, 2.3803673122141427, 1.8561001674560287,
          ],
        },
        {
          model: "catboost_bounded",
          validation: 1.4956794357173266,
          folds: [2.1436624246276836, 1.5907150586578351, 1.1916725606358505, 1.0566676989479376],
          final: 1.6511521580725226,
          n: 44773,
          degraded: 14,
          horizons: [
            1.6378587325503937, 1.43857455555982, 1.2101966825684376, 0.9328401825121132,
            0.684298562517439, 0.5264862437610278, 0.4587748147143848, 0.41611103736136695,
            0.5433096042858923, 0.929707772144494, 1.4942967958625972, 2.1295222789352213,
            2.027708724682124, 1.8144865929010316, 1.9994484386813862, 2.07564782228998,
            2.148466264708921, 2.2171098721459472, 2.4340750555329955, 2.436872950866445,
            2.7867618262613596, 2.7187980105338365, 2.5138812897971223, 1.9760299868140383,
          ],
        },
      ],
      slices: [
        {
          name: "catboost_bounded",
          slice: "actual:zero",
          n: 37594,
          mae: 0.5225391086785536,
          bias: 0.5225391086785536,
        },
        {
          name: "catboost_bounded",
          slice: "actual:nonzero",
          n: 39712,
          mae: 2.3623794690742623,
          bias: -1.3136792943210096,
        },
        {
          name: "catboost_bounded",
          slice: "actual:peak_ge20",
          n: 1107,
          mae: 14.398696866852413,
          bias: -14.306056205223049,
        },
        {
          name: "seasonal_naive",
          slice: "actual:zero",
          n: 37594,
          mae: 0.5679629728148109,
          bias: 0.5679629728148109,
        },
        {
          name: "seasonal_naive",
          slice: "actual:nonzero",
          n: 39712,
          mae: 2.8595134971796936,
          bias: -0.7842717566478646,
        },
        {
          name: "seasonal_naive",
          slice: "actual:peak_ge20",
          n: 1107,
          mae: 12.447154471544716,
          bias: -10.956639566395664,
        },
      ],
      ablation: [
        {
          group: "calendar",
          delta: 0.28304496609815366,
        },
        {
          group: "station_identity",
          delta: 0.021526721244986502,
        },
        {
          group: "demand_profile",
          delta: 0.12232011650885727,
        },
      ],
    },
  ],
} as const;
