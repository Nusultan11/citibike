// Recorded research findings for the Deep Analytics page. Every number is copied
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
  recorded: string;
};

export const FINDING_AREAS: Record<FindingArea, string> = {
  data: "Данные и качество",
  analytics: "Аналитика спроса",
  forecast: "Прогнозы F4",
  scenario: "Сценарии F5",
};

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
      "2024-10…2025-09: 46 697 958 поездок → 2025-10…2026-09: 45 378 096. Доля электровелосипедов выросла с 69,6% до 72,3%; концентрация по станциям стабильна (Gini 0,60–0,61).",
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
      "Все 40 станций топа |net flow| оказались парами dddd.d / dddd.d0: одно название, < 1 м между точками, причина — формат raw-архива. Пары помечаются и исключаются из рейтингов, Silver не объединяется.",
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
    recorded: "2026-11-02",
  },
  {
    id: "f4-diagnostics",
    area: "forecast",
    status: "not_verified",
    value: "593",
    title: "Аномалии найдены, но не подтверждены",
    takeaway:
      "Контекстная диагностика station-hour дала 593 дедуплицированных события (spike / long zero / CUSUM); на контрольных инъекциях 12/12 PASS. Гипотеза о контекстном признаке — inconclusive.",
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
