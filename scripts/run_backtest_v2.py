#!/usr/bin/env python3
"""
Rolling backtest v2: исследовательский инструмент.

Сегменты: RETAIL_REGULAR, BASE_REGULAR, BASE_WITH_PREORDER
Модели:   mean_cal_{7,14,21,28}, median_cal_{7,14,21,28}, croston, weekly_naive
Периоды:  NORMAL / VALENTINE / MARCH_8 / NEW_YEAR / OTHER_HOLIDAY

Только читает аналитическую БД. calc_forecast.py не меняет.

Запуск:
  python3 scripts/run_backtest_v2.py [--horizon N] [--out report.txt]
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import hermes.config as config
import hermes.db as db


# ── Конфигурация ────────────────────────────────────────────────────────────

HORIZON = 7  # дней вперёд
MIN_POS_DAYS = 2  # мин. дней с продажами для прогноза Croston
MIN_ITERS_FOR_STATUS = 10  # минимум итераций для вывода статуса OK

# Holiday windows (inclusive)
HOLIDAY_WINDOWS: list[tuple[tuple[int, int], tuple[int, int], str]] = [
    ((2, 11), (2, 17), "VALENTINE"),
    ((3, 1),  (3, 10), "MARCH_8"),
    ((12, 25),(12, 31), "NEW_YEAR"),
    ((1, 1),  (1, 10), "NEW_YEAR"),
]

# Правила классификации спроса (порядок важен — первое совпадение побеждает)
# Формат: (predicate_fn, demand_type, excluded_reason)
DEMAND_CLASSIFICATION_RULES = [
    (lambda pn, rev, cost: "ПРЕДОПЛАТА" in pn,
     "PREORDER_DEMAND", "PRODUCT_NAME_PREORDER"),
    (lambda pn, rev, cost: rev == 0 and cost == 0 and "ПРЕДОПЛАТА" not in pn,
     "INTERNAL_MOVEMENT", "INTERNAL_WRITEOFF_AGENT"),
    (lambda pn, rev, cost: rev == 0 and cost > 0,
     "UNKNOWN", "ZERO_REVENUE_WITH_COST"),
    (lambda pn, rev, cost: rev > 0,
     "REGULAR_CUSTOMER_DEMAND", None),
]

# Конфигурация складов: operational_start, evaluation_start
# evaluation_start — момент после которого есть стабильная история
STORE_CONFIG = {
    # store_id заполняется из STORE_CHANNELS; здесь сопоставление по имени
    # (заполняется в _build_store_config из live-данных БД)
}

EVALUATION_START_OVERRIDES: dict[str, date] = {
    # channel -> override_date; для Базы пропускаем предновогоднюю неделю
    "опт": date(2026, 1, 5),  # после НГ-каникул
}


# ── Типы данных ──────────────────────────────────────────────────────────────

@dataclass
class StoreInfo:
    store_id: str
    store_name: str
    channel: str
    segment: str          # RETAIL | BASE | RESTAURANT
    operational_start: date | None
    evaluation_start: date | None


@dataclass
class SalesRecord:
    day: date
    store_id: str
    assortment_id: str
    product_name: str
    sell_qty: float
    revenue_kop: int
    cost_kop: int
    demand_type: str       # REGULAR_CUSTOMER_DEMAND | PREORDER_DEMAND | INTERNAL_MOVEMENT | UNKNOWN
    excluded_reason: str | None


@dataclass
class BacktestRecord:
    cutoff: date
    window: int
    product_id: str
    product_name: str
    store_id: str
    store_name: str
    segment: str
    demand_mode: str       # REGULAR | PREORDER_INCLUDED
    period_type: str       # NORMAL | VALENTINE | MARCH_8 | NEW_YEAR
    actual: float
    preds: dict[str, float | None] = field(default_factory=dict)
    excluded: bool = False
    exclusion_reason: str | None = None


# ── Вспомогательные функции ──────────────────────────────────────────────────

def classify_demand(product_name: str, revenue_kop: int, cost_kop: int) -> tuple[str, str | None]:
    for pred, dtype, reason in DEMAND_CLASSIFICATION_RULES:
        if pred(product_name, revenue_kop, cost_kop):
            return dtype, reason
    return "UNKNOWN", "NO_RULE_MATCHED"


def classify_period(d: date) -> str:
    for (m0, d0), (m1, d1), label in HOLIDAY_WINDOWS:
        if date(d.year, m0, d0) <= d <= date(d.year, m1, d1):
            return label
    return "NORMAL"


def _daterange(d_from: date, d_to: date):
    d = d_from
    while d <= d_to:
        yield d
        d += timedelta(days=1)


# ── Модели ───────────────────────────────────────────────────────────────────

def _make_series(daily: dict[date, float], cutoff: date, window: int,
                 eval_start: date | None) -> list[float]:
    """Возвращает список дневных значений за [cutoff-window, cutoff-1], ≥ eval_start."""
    start = cutoff - timedelta(days=window)
    if eval_start:
        start = max(start, eval_start)
    return [daily.get(d, 0.0) for d in _daterange(start, cutoff - timedelta(days=1))]


def _models(series: list[float], prev_week: list[float], horizon: int) -> dict[str, float | None]:
    n = len(series)
    positive = [s for s in series if s > 0]
    n_pos = len(positive)
    out: dict[str, float | None] = {}

    for w in (7, 14, 21, 28):
        sub = series[-w:] if len(series) >= w else series
        if len(sub) > 0:
            out[f"mean_cal_{w}"]   = (sum(sub) / len(sub)) * horizon
            out[f"median_cal_{w}"] = statistics.median(sub) * horizon if len(sub) >= 2 else None
        else:
            out[f"mean_cal_{w}"]   = None
            out[f"median_cal_{w}"] = None

    # Croston
    if n_pos >= MIN_POS_DAYS:
        pos_idx = [i for i, s in enumerate(series) if s > 0]
        inter   = [pos_idx[i + 1] - pos_idx[i] for i in range(len(pos_idx) - 1)]
        p_bar   = sum(inter) / len(inter) if inter else 1.0
        z_bar   = sum(positive) / n_pos
        out["croston"] = (z_bar / p_bar) * horizon if p_bar > 0 else None
    else:
        out["croston"] = None

    # Weekly naive: прогноз следующей недели = продажи предыдущей недели
    if prev_week:
        out["weekly_naive"] = sum(prev_week)
    else:
        out["weekly_naive"] = None

    return out


MODEL_NAMES = [f"mean_cal_{w}" for w in (7, 14, 21, 28)] + \
              [f"median_cal_{w}" for w in (7, 14, 21, 28)] + \
              ["croston", "weekly_naive"]


# ── Загрузка данных ──────────────────────────────────────────────────────────

def load_sales(conn) -> list[SalesRecord]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, store_id, assortment_id, product_name,
                   sell_qty, revenue_kop, cost_kop
            FROM sales_by_product_day
            WHERE day BETWEEN '2025-10-01' AND '2026-08-13'
              AND sell_qty > 0
            ORDER BY day
        """)
        rows = cur.fetchall()
    result = []
    for day, sid, aid, pname, qty, rev, cost in rows:
        dtype, reason = classify_demand(pname, rev, cost)
        result.append(SalesRecord(
            day=day, store_id=sid, assortment_id=aid, product_name=pname,
            sell_qty=qty, revenue_kop=rev, cost_kop=cost,
            demand_type=dtype, excluded_reason=reason,
        ))
    return result


def build_store_info(conn) -> dict[str, StoreInfo]:
    channels = config.STORE_CHANNELS  # {store_id: channel}
    channel_to_segment = {"розница": "RETAIL", "опт": "BASE", "ресторан": "RESTAURANT"}

    # operational_start = первый день с revenue > 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT store_id, MIN(day) AS first_rev
            FROM sales_by_product_day
            WHERE revenue_kop > 0
            GROUP BY store_id
        """)
        first_rev: dict[str, date] = {r[0]: r[1] for r in cur.fetchall()}

        # store names
        cur.execute("SELECT DISTINCT store_id, store_name FROM sales_by_store_day")
        names: dict[str, str] = {r[0]: r[1] for r in cur.fetchall()}

    result = {}
    for sid, channel in channels.items():
        sname = names.get(sid, sid)
        segment = channel_to_segment.get(channel, "OTHER")
        op_start = first_rev.get(sid)
        # evaluation_start: override для опт (после НГ), иначе = op_start
        ev_start = EVALUATION_START_OVERRIDES.get(channel, op_start)
        if op_start and ev_start and ev_start < op_start:
            ev_start = op_start
        result[sid] = StoreInfo(
            store_id=sid, store_name=sname, channel=channel,
            segment=segment, operational_start=op_start, evaluation_start=ev_start,
        )
    return result


# ── Основной backtest ─────────────────────────────────────────────────────────

def run_backtest(sales: list[SalesRecord], stores: dict[str, StoreInfo],
                 horizon: int, demand_modes: list[str]) -> list[BacktestRecord]:
    """
    demand_modes: REGULAR (только REGULAR_CUSTOMER_DEMAND)
                  PREORDER_INCLUDED (REGULAR + PREORDER_DEMAND)
    """
    # Индексируем: (sid, aid) -> {day: qty}  по режиму
    daily: dict[str, dict[tuple, dict[date, float]]] = {}
    for mode in demand_modes:
        daily[mode] = defaultdict(lambda: defaultdict(float))

    for r in sales:
        for mode in demand_modes:
            include = (
                r.demand_type == "REGULAR_CUSTOMER_DEMAND" or
                (mode == "PREORDER_INCLUDED" and r.demand_type == "PREORDER_DEMAND")
            )
            if include:
                daily[mode][(r.store_id, r.assortment_id)][r.day] += r.sell_qty

    # Все уникальные sku×store
    all_keys: set[tuple[str, str]] = set()
    for r in sales:
        if r.demand_type in ("REGULAR_CUSTOMER_DEMAND", "PREORDER_DEMAND"):
            all_keys.add((r.store_id, r.assortment_id))
    # Имена продуктов
    prod_names: dict[tuple[str, str], str] = {}
    for r in sales:
        prod_names[(r.store_id, r.assortment_id)] = r.product_name

    # Глобальный диапазон
    all_days = sorted({r.day for r in sales})
    if not all_days:
        return []
    hist_min, hist_max = all_days[0], all_days[-1]
    max_window = 28

    records: list[BacktestRecord] = []

    for mode in demand_modes:
        for sid, sinfo in stores.items():
            if sinfo.segment == "RESTAURANT":
                continue
            # Определяем сегмент-режим
            seg_mode = f"{sinfo.segment}_{mode}"
            eval_start = sinfo.evaluation_start
            if eval_start is None:
                continue

            # Допустимые cutoffs: [eval_start + max_window, hist_max - horizon]
            cutoff_start = eval_start + timedelta(days=max_window)
            cutoff_end   = hist_max - timedelta(days=horizon)
            if cutoff_start > cutoff_end:
                continue

            # Шаг 7 дней
            cutoff = cutoff_start
            while cutoff <= cutoff_end:
                fcst_from = cutoff
                fcst_to   = cutoff + timedelta(days=horizon - 1)
                period_type = classify_period(fcst_from)

                # Все sku на этом складе с любой активностью до cutoff
                store_keys = [(s, a) for (s, a) in all_keys if s == sid and
                              any(d < cutoff for d in daily[mode][(s, a)])]

                # Кандидаты: sku с фактическими продажами в forecast-периоде
                actual_sums: dict[tuple, float] = {}
                for s, a in store_keys:
                    actual = sum(
                        daily[mode][(s, a)].get(d, 0.0)
                        for d in _daterange(fcst_from, fcst_to)
                    )
                    if actual > 0:
                        actual_sums[(s, a)] = actual

                for (s, a), actual in actual_sums.items():
                    pname = prod_names.get((s, a), "?")

                    # Предыдущая неделя для weekly_naive
                    prev_7 = [daily[mode][(s, a)].get(cutoff - timedelta(days=i), 0.0)
                               for i in range(1, 8)]

                    # Для каждого окна — series и прогноз
                    # (используем max окно для series, потом внутри _models берём sub)
                    full_series = _make_series(daily[mode][(s, a)], cutoff, max_window, eval_start)
                    preds = _models(full_series, prev_7, horizon)

                    rec = BacktestRecord(
                        cutoff=cutoff, window=max_window,
                        product_id=a, product_name=pname,
                        store_id=s, store_name=sinfo.store_name,
                        segment=sinfo.segment, demand_mode=mode,
                        period_type=period_type,
                        actual=actual, preds=preds,
                    )
                    records.append(rec)

                cutoff += timedelta(days=7)

    return records


# ── Метрики ───────────────────────────────────────────────────────────────────

def compute_metrics(errors: list[float], actuals: list[float],
                    preds_list: list[float]) -> dict:
    if not errors:
        return {}
    ae = [abs(e) for e in errors]
    n = len(errors)
    sum_act = sum(actuals)
    return {
        "n":            n,
        "MAE":          round(sum(ae) / n, 1),
        "MdAE":         round(statistics.median(ae), 1),
        "WAPE":         round(sum(ae) / sum_act, 3) if sum_act else None,
        "rel_bias":     round(sum(errors) / sum_act, 3) if sum_act else None,
        "abs_bias":     round(sum(errors) / n, 1),
        "P90_AE":       round(sorted(ae)[int(0.9 * n)], 1),
        "over_rate":    round(sum(1 for e in errors if e > 0) / n, 3),  # overforecast
        "under_rate":   round(sum(1 for e in errors if e < 0) / n, 3), # underforecast
    }


def aggregate(records: list[BacktestRecord]) -> dict[str, dict]:
    """Агрегация метрик по модели×сегменту×режиму."""
    # bucket: (model, segment, mode, period_type) -> {errors, actuals}
    buckets: dict[tuple, dict[str, list]] = defaultdict(lambda: {"errors": [], "actuals": []})

    for rec in records:
        if rec.excluded:
            continue
        for model in MODEL_NAMES:
            pred = rec.preds.get(model)
            if pred is None:
                continue
            err = pred - rec.actual
            key = (model, rec.segment, rec.demand_mode, rec.period_type)
            buckets[key]["errors"].append(err)
            buckets[key]["actuals"].append(rec.actual)
            # Добавляем в ALL-периоды
            all_key = (model, rec.segment, rec.demand_mode, "ALL")
            buckets[all_key]["errors"].append(err)
            buckets[all_key]["actuals"].append(rec.actual)

    result = {}
    for key, data in buckets.items():
        m = compute_metrics(data["errors"], data["actuals"],
                             [e + a for e, a in zip(data["errors"], data["actuals"])])
        result[key] = m
    return result


def worst_errors(records: list[BacktestRecord], model: str, n: int = 20,
                 mode: str = "REGULAR") -> list[tuple]:
    """Топ-N ошибок (|pred - actual|) для заданной модели и режима."""
    rows = []
    for rec in records:
        if rec.demand_mode != mode or rec.excluded:
            continue
        pred = rec.preds.get(model)
        if pred is None:
            continue
        rows.append((abs(pred - rec.actual), pred, rec.actual,
                     rec.cutoff, rec.store_name, rec.product_name,
                     rec.period_type, rec.demand_mode))
    rows.sort(reverse=True)
    return rows[:n]


def coverage_report(records: list[BacktestRecord]) -> dict[str, dict]:
    """Coverage по сегменту×режиму."""
    buckets: dict[tuple, dict] = defaultdict(lambda: {
        "candidate": 0, "predicted": 0,
        "excl": defaultdict(int),
    })
    for rec in records:
        key = (rec.segment, rec.demand_mode)
        buckets[key]["candidate"] += 1
        if rec.excluded:
            reason = rec.exclusion_reason or "UNKNOWN"
            buckets[key]["excl"][reason] += 1
        else:
            # predicted если хотя бы одна модель дала прогноз
            if any(v is not None for v in rec.preds.values()):
                buckets[key]["predicted"] += 1
    result = {}
    for key, data in buckets.items():
        c = data["candidate"]
        p = data["predicted"]
        result[key] = {
            "candidate": c,
            "predicted": p,
            "coverage_pct": round(p / c * 100, 1) if c else 0,
            "excluded_by_reason": dict(data["excl"]),
        }
    return result


# ── CustomerOrder анализ (BASE only) ─────────────────────────────────────────

def co_analysis(stores: dict[str, StoreInfo]) -> None:
    """
    Для BASE: при каждом историческом cutoff — какая доля
    фактического demand-qty была известна через CustomerOrder
    (созданный до cutoff)?
    """
    import hermes.config as cfg

    base_sids = [sid for sid, s in stores.items() if s.segment == "BASE"]
    if not base_sids:
        print("  CO analysis: нет BASE-складов")
        return

    token = cfg.MOYSKLAD_TOKEN()
    BASE_API = "https://api.moysklad.ru/api/remap/1.2"

    def api_get(path, params=None):
        url = BASE_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept-Encoding", "gzip")
        req.add_header("User-Agent", "hermes-backtest2/1.0")
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    return json.loads(data)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(3 * attempt); continue
                return {}
            except Exception:
                if attempt < 3:
                    time.sleep(2 * attempt); continue
                return {}
        return {}

    sid = base_sids[0]
    store_href = f"{BASE_API}/entity/store/{sid}"
    sinfo = stores[sid]

    print(f"\n  Загружаем demand+CO для {sinfo.store_name} (gap + Aug)...")
    # Загружаем все demand с expand=customerOrder, customerOrder.moment
    all_demands: list[dict] = []
    offset = 0
    while True:
        r = api_get("/entity/demand", {
            "filter": f"store={store_href};moment>=2025-10-01 00:00:00;moment<=2026-08-13 23:59:59",
            "limit": 100, "offset": offset,
            "expand": "customerOrder",
        })
        rows = r.get("rows", [])
        all_demands.extend(rows)
        size = r.get("meta", {}).get("size", 0)
        offset += len(rows)
        if not rows or offset >= size:
            break
        if offset % 500 == 0:
            print(f"    ... {offset}/{size}")
        time.sleep(0.3)

    print(f"  Загружено {len(all_demands)} demand-документов")

    # Строим mapping: demand_moment → {sum, co_moment}
    demand_map: list[dict] = []
    for d in all_demands:
        try:
            d_moment = date.fromisoformat(d.get("moment", "")[:10])
        except Exception:
            continue
        d_sum = int(d.get("sum", 0))
        co = d.get("customerOrder")
        co_moment = None
        if co and isinstance(co, dict) and co.get("moment"):
            try:
                co_moment = date.fromisoformat(co["moment"][:10])
            except Exception:
                pass
        demand_map.append({"day": d_moment, "sum": d_sum, "co_day": co_moment})

    # Для каждого cutoff (step=7d): доля известного спроса
    if not sinfo.evaluation_start:
        print("  CO analysis: нет evaluation_start для BASE")
        return

    cutoff_start = sinfo.evaluation_start + timedelta(days=28)
    cutoff_end   = date(2026, 8, 6)
    rows_out: list[tuple] = []
    cutoff = cutoff_start
    while cutoff <= cutoff_end:
        fcst_from = cutoff
        fcst_to   = cutoff + timedelta(days=6)  # horizon=7
        # Demand в forecast-периоде
        in_forecast = [dm for dm in demand_map if fcst_from <= dm["day"] <= fcst_to]
        if not in_forecast:
            cutoff += timedelta(days=7); continue
        total_sum = sum(dm["sum"] for dm in in_forecast)
        known_2d  = sum(dm["sum"] for dm in in_forecast
                        if dm["co_day"] and dm["co_day"] <= cutoff - timedelta(days=2))
        known_7d  = sum(dm["sum"] for dm in in_forecast
                        if dm["co_day"] and dm["co_day"] <= cutoff - timedelta(days=7))
        known_14d = sum(dm["sum"] for dm in in_forecast
                        if dm["co_day"] and dm["co_day"] <= cutoff - timedelta(days=14))
        known_at_cutoff = sum(dm["sum"] for dm in in_forecast
                               if dm["co_day"] and dm["co_day"] <= cutoff)
        period_type = classify_period(fcst_from)
        rows_out.append((
            str(cutoff), period_type,
            len(in_forecast), total_sum / 100,
            round(100 * known_at_cutoff / total_sum, 1) if total_sum else 0,
            round(100 * known_2d / total_sum, 1) if total_sum else 0,
            round(100 * known_7d / total_sum, 1) if total_sum else 0,
            round(100 * known_14d / total_sum, 1) if total_sum else 0,
        ))
        cutoff += timedelta(days=7)

    print(f"\n  CustomerOrder coverage — База Воровского")
    print(f"  {'Cutoff':12s} {'Period':10s} {'docs':>5s} {'Rev,р':>12s} {'CO@cut%':>9s} {'CO-2d%':>8s} {'CO-7d%':>8s} {'CO-14d%':>9s}")
    print("  " + "-"*80)
    for row in rows_out:
        print(f"  {row[0]:12s} {row[1]:10s} {row[2]:>5d} {row[3]:>12,.0f} {row[4]:>9.1f} {row[5]:>8.1f} {row[6]:>8.1f} {row[7]:>9.1f}")

    if rows_out:
        avg_at_cut = sum(r[4] for r in rows_out) / len(rows_out)
        avg_7d     = sum(r[6] for r in rows_out) / len(rows_out)
        avg_14d    = sum(r[7] for r in rows_out) / len(rows_out)
        print(f"\n  Среднее: CO@cutoff={avg_at_cut:.1f}%  CO-7d={avg_7d:.1f}%  CO-14d={avg_14d:.1f}%")


# ── Отчёт ────────────────────────────────────────────────────────────────────

def print_report(records: list[BacktestRecord], stores: dict[str, StoreInfo],
                 conn, out_lines: list[str]) -> None:
    def p(*args):
        line = " ".join(str(a) for a in args)
        print(line)
        out_lines.append(line)

    p("=" * 70)
    p("ROLLING BACKTEST v2 — полный отчёт")
    p("=" * 70)

    agg = aggregate(records)
    cov = coverage_report(records)

    # ── A. Классификация спроса ──────────────────────────────────────────
    p("\n── A. Классификация спроса (все строки БД) ──")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_name, revenue_kop, cost_kop, sell_qty
            FROM sales_by_product_day
            WHERE day BETWEEN '2025-10-01' AND '2026-08-13' AND sell_qty > 0
        """)
        raw = cur.fetchall()
    type_counts: dict[str, list[float]] = defaultdict(list)
    for pname, rev, cost, qty in raw:
        dtype, _ = classify_demand(pname, rev, cost)
        type_counts[dtype].append(qty)
    for dtype, qtys in sorted(type_counts.items()):
        p(f"  {dtype:30s}: {len(qtys):6d} строк  qty={sum(qtys):9.0f}")

    # ── B. Operational / evaluation start ────────────────────────────────
    p("\n── B. Operational & evaluation start ──")
    p(f"  {'Склад':30s} {'Сегмент':8s} {'op_start':12s} {'ev_start':12s}")
    for sid, s in stores.items():
        p(f"  {s.store_name:30s} {s.segment:8s} {str(s.operational_start):12s} {str(s.evaluation_start):12s}")

    # ── C. Coverage ────────────────────────────────────────────────────────
    p("\n── C. Coverage по сегменту×режиму ──")
    for (seg, mode), cv in sorted(cov.items()):
        p(f"  {seg}×{mode}: candidate={cv['candidate']}  predicted={cv['predicted']}  {cv['coverage_pct']}%")
        for reason, cnt in cv["excluded_by_reason"].items():
            p(f"    excl {reason}: {cnt}")

    # ── D/E. Таблица метрик — ALL + NORMAL ──────────────────────────────
    def _model_table(seg: str, mode: str, period: str) -> None:
        p(f"\n  {seg} × {mode} × {period}")
        p(f"  {'model':18s} {'n':>5s} {'MAE':>7s} {'MdAE':>7s} {'WAPE':>7s} {'relBias':>8s} {'P90':>7s} {'over%':>6s} {'under%':>7s}")
        p("  " + "-"*85)
        # Сортируем по MAE
        model_rows = []
        for model in MODEL_NAMES:
            key = (model, seg, mode, period)
            m = agg.get(key)
            if m and m.get("n", 0) > 0:
                model_rows.append((model, m))
        model_rows.sort(key=lambda x: x[1].get("MAE", 9999))
        for model, m in model_rows[:12]:
            wape_str = f"{m['WAPE']:.3f}" if m.get("WAPE") is not None else "  —  "
            rb_str   = f"{m['rel_bias']:+.3f}" if m.get("rel_bias") is not None else "  —  "
            p(f"  {model:18s} {m['n']:>5d} {m['MAE']:>7.1f} {m['MdAE']:>7.1f} {wape_str:>7s} {rb_str:>8s} {m['P90_AE']:>7.1f} {m['over_rate']*100:>5.1f}% {m['under_rate']*100:>6.1f}%")

    for seg in ("RETAIL", "BASE"):
        for mode in ("REGULAR", "PREORDER_INCLUDED"):
            for period in ("ALL", "NORMAL", "MARCH_8", "NEW_YEAR"):
                key_check = (MODEL_NAMES[0], seg, mode, period)
                if agg.get(key_check):
                    _model_table(seg, mode, period)

    # ── F. Топ-3 модели по каждому складу ────────────────────────────────
    p("\n── F. Топ-3 модели по каждому складу (REGULAR, ALL периоды, MAE) ──")
    # Перегруппируем по store_id
    store_buckets: dict[tuple, dict[str, list]] = defaultdict(
        lambda: {m: {"errors": [], "actuals": []} for m in MODEL_NAMES}
    )
    for rec in records:
        if rec.excluded or rec.demand_mode != "REGULAR":
            continue
        for model in MODEL_NAMES:
            pred = rec.preds.get(model)
            if pred is not None:
                store_buckets[(rec.store_id, rec.store_name)][model]["errors"].append(pred - rec.actual)
                store_buckets[(rec.store_id, rec.store_name)][model]["actuals"].append(rec.actual)

    for (sid, sname), model_data in sorted(store_buckets.items(), key=lambda x: x[0][1]):
        p(f"\n  {sname}")
        rows_store = []
        for model, data in model_data.items():
            if not data["errors"]:
                continue
            m = compute_metrics(data["errors"], data["actuals"], [])
            rows_store.append((model, m))
        rows_store.sort(key=lambda x: x[1].get("MAE", 9999))
        p(f"  {'model':18s} {'n':>5s} {'MAE':>7s} {'WAPE':>7s} {'relBias':>8s}")
        for model, m in rows_store[:3]:
            wape_str = f"{m['WAPE']:.3f}" if m.get("WAPE") is not None else "  —  "
            rb_str   = f"{m['rel_bias']:+.3f}" if m.get("rel_bias") is not None else "  —  "
            p(f"  {model:18s} {m['n']:>5d} {m['MAE']:>7.1f} {wape_str:>7s} {rb_str:>8s}")

    # ── G. Worst-20 ошибок ───────────────────────────────────────────────
    p("\n── G. Worst-20 ошибок (mean_cal_14, REGULAR) ──")
    worst = worst_errors(records, "mean_cal_14", 20)
    p(f"  {'|err|':>7s} {'pred':>7s} {'actual':>8s} {'cutoff':12s} {'store':22s} {'период':10s} {'product'}")
    p("  " + "-"*100)
    for row in worst:
        ae, pred, actual, cutoff, sname, pname, period, mode = row
        p(f"  {ae:>7.0f} {pred:>7.0f} {actual:>8.0f} {str(cutoff):12s} {sname[:21]:22s} {period:10s} {pname[:35]}")

    # ── H. Стабильность: wins по итерациям ─────────────────────────────
    p("\n── H. Стабильность модели (RETAIL×REGULAR×ALL) — wins по MAE ──")
    # По каждому cutoff×store считаем лучшую модель
    cutoff_store_errors: dict[tuple, dict[str, list[float]]] = defaultdict(
        lambda: {m: [] for m in MODEL_NAMES}
    )
    for rec in records:
        if rec.excluded or rec.segment != "RETAIL" or rec.demand_mode != "REGULAR":
            continue
        for model in MODEL_NAMES:
            pred = rec.preds.get(model)
            if pred is not None:
                key = (rec.cutoff, rec.store_id)
                cutoff_store_errors[key][model].append(abs(pred - rec.actual))

    wins: dict[str, int] = defaultdict(int)
    for key, model_ae in cutoff_store_errors.items():
        mae_by_model = {m: sum(ae) / len(ae) for m, ae in model_ae.items() if ae}
        if mae_by_model:
            best = min(mae_by_model, key=mae_by_model.get)
            wins[best] += 1

    total_iters = sum(wins.values())
    p(f"  Всего cutoff×store: {total_iters}")
    for model, w in sorted(wins.items(), key=lambda x: -x[1])[:8]:
        p(f"  {model:18s}: {w:4d} побед ({100*w//total_iters if total_iters else 0}%)")

    # ── I. Рекомендация baseline RETAIL ─────────────────────────────────
    p("\n── I. Рекомендация baseline RETAIL ──")
    retail_normal = {
        m: agg.get((m, "RETAIL", "REGULAR", "NORMAL"))
        for m in MODEL_NAMES
    }
    best_retail = sorted(
        [(m, v) for m, v in retail_normal.items() if v and v.get("n", 0) >= 50],
        key=lambda x: x[1]["MAE"]
    )
    if best_retail:
        bm, bv = best_retail[0]
        p(f"  Лучшая RETAIL/NORMAL/REGULAR: {bm}")
        p(f"    MAE={bv['MAE']}  WAPE={bv['WAPE']}  relBias={bv['rel_bias']:+.3f}  n={bv['n']}")
        p()
        p(f"  Топ-3 кандидата:")
        for model, mv in best_retail[:3]:
            p(f"    {model:18s} MAE={mv['MAE']:6.1f} WAPE={mv['WAPE']:.3f} bias={mv['rel_bias']:+.3f}")

    # ── J. Рекомендация архитектуры BASE ─────────────────────────────────
    p("\n── J. Рекомендация архитектуры BASE ──")
    base_reg = {
        m: agg.get((m, "BASE", "REGULAR", "ALL"))
        for m in MODEL_NAMES
    }
    best_base = sorted(
        [(m, v) for m, v in base_reg.items() if v and v.get("n", 0) >= 20],
        key=lambda x: x[1]["MAE"]
    )
    if best_base:
        bm, bv = best_base[0]
        p(f"  Лучший статистический: {bm} MAE={bv['MAE']} WAPE={bv['WAPE']}")
    p(f"  Рекомендация: BASE требует двух треков:")
    p(f"    1. known_CO (customer orders до cutoff) → точный для крупных отгрузок")
    p(f"    2. statistical_residual (mean_cal на REGULAR без CO-покрытых дней)")
    p(f"    CO-анализ — см. секцию ниже.")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=HORIZON)
    parser.add_argument("--out", default=None, help="Сохранить отчёт в файл")
    parser.add_argument("--skip-co", action="store_true", help="Пропустить CO-анализ (экономит API-запросы)")
    args = parser.parse_args()

    horizon = args.horizon
    conn = db.connect(config.DATABASE_URL())
    conn.autocommit = True

    print("Загружаем данные...")
    sales = load_sales(conn)
    stores = build_store_info(conn)
    print(f"  Строк продаж: {len(sales)}  Складов: {len(stores)}")

    print("Запускаем rolling backtest...")
    records = run_backtest(sales, stores, horizon, ["REGULAR", "PREORDER_INCLUDED"])
    print(f"  BacktestRecord создано: {len(records)}")

    out_lines: list[str] = []
    print_report(records, stores, conn, out_lines)

    if not args.skip_co:
        print("\nCO-анализ (BASE)...")
        co_analysis(stores)
    else:
        print("[--skip-co] CO-анализ пропущен")

    if args.out:
        Path(args.out).write_text("\n".join(out_lines), encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.out}")

    conn.close()


if __name__ == "__main__":
    main()
