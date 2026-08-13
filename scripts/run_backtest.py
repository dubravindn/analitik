"""
Rolling backtest v2 — диагностический режим.

СТАТУС: INSUFFICIENT_BACKTEST_HISTORY
  4–5 итераций (нужно ≥10 для предварительных выводов).
  Причина: разрыв в sales_by_product_day (окт 2025 – июн 2026).
  Все метрики — предварительные, не для принятия решений о модели.

Улучшения по сравнению с v1:
  - Coverage: candidate / predicted / excluded + причины исключения
  - 5 моделей: median_pos, mean_pos, mean_cal, median_cal, croston
  - WAPE как ratio (0.39 = 39%, явно помечается)
  - Bias: abs (mean error) и rel (sum/sum_actual)
  - Breakdown по каждому складу
  - STATUS PREFIX в заголовке
  - Детали исключений: INSUFFICIENT_POS_DAYS / DATA_GAP

Запуск:
    python3 scripts/run_backtest.py
    python3 scripts/run_backtest.py --horizon 7 --windows 7 14 21 28
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

sys.path.insert(0, ".")
from hermes.config import DATABASE_URL
import psycopg

BASE_STORE_KEYWORDS = ("база", "base")
MIN_ITERS_FOR_CONCLUSIONS = 10

ExclusionReason = Literal[
    "INSUFFICIENT_POS_DAYS",  # appeared in training but pos_days < threshold
    "DATA_GAP",               # product×store exists in history but no training sales
]


# ── Модели ────────────────────────────────────────────────────────────

def _models(train_days: list[float], horizon: int) -> dict[str, float | None]:
    """Все 5 baseline-моделей для одного ряда.

    train_days — список продаж по дням (включая нулевые).
    Возвращает dict model → forecast (None если недостаточно данных).
    """
    n = len(train_days)
    positive = [s for s in train_days if s > 0]
    n_pos = len(positive)
    out: dict[str, float | None] = {}

    # A1: median of positive-sales days × horizon
    out["median_pos"] = statistics.median(positive) * horizon if n_pos >= 2 else None

    # A2: mean of positive-sales days × horizon
    out["mean_pos"] = (sum(positive) / n_pos) * horizon if n_pos >= 1 else None

    # A3: mean of all calendar days × horizon
    out["mean_cal"] = (sum(train_days) / n) * horizon if n > 0 else None

    # A4: median of all calendar days × horizon
    out["median_cal"] = statistics.median(train_days) * horizon if n >= 2 else None

    # A5: Croston-lite
    # z̄ = mean non-zero demand; p̄ = mean inter-arrival (days between positive sales)
    if n_pos >= 2:
        pos_idx = [i for i, s in enumerate(train_days) if s > 0]
        inter = [pos_idx[i + 1] - pos_idx[i] for i in range(len(pos_idx) - 1)]
        p_bar = sum(inter) / len(inter) if inter else 1.0
        z_bar = sum(positive) / n_pos
        out["croston"] = (z_bar / p_bar) * horizon if p_bar > 0 else None
    else:
        out["croston"] = None

    return out


# ── Метрики ───────────────────────────────────────────────────────────

def _metrics(errors: list[float], actuals: list[float]) -> dict:
    if not errors:
        return {}
    ae = [abs(e) for e in errors]
    s_actual = sum(actuals)
    s_err = sum(errors)
    s_ae = sum(ae)
    return {
        "n": len(errors),
        "MAE": round(s_ae / len(ae), 1),
        "MdAE": round(statistics.median(ae), 1),
        # WAPE: ratio, NOT percent. 0.39 means 39%.
        "WAPE_ratio": round(s_ae / s_actual, 3) if s_actual else None,
        "abs_bias": round(s_err / len(errors), 1),   # mean(forecast - actual)
        "rel_bias": round(s_err / s_actual, 3) if s_actual else None,  # sum/sum_actual
    }


# ── Загрузка ──────────────────────────────────────────────────────────

def _load_sales(conn, pids: list[str], d_from: date, d_to: date) -> defaultdict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT assortment_id, store_id, day, sell_qty
            FROM sales_by_product_day
            WHERE assortment_id = ANY(%s) AND day BETWEEN %s AND %s
        """, [pids, d_from, d_to])
        out: defaultdict = defaultdict(float)
        for r in cur.fetchall():
            out[(r[0], r[1], r[2])] = float(r[3] or 0)
    return out


def _load_active_pairs(conn, pids: list[str], lookback_from: date,
                       lookback_to: date) -> set[tuple[str, str]]:
    """Все (pid, sid) с хотя бы одной продажей в lookback-окне."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT assortment_id, store_id
            FROM sales_by_product_day
            WHERE assortment_id = ANY(%s) AND day BETWEEN %s AND %s
        """, [pids, lookback_from, lookback_to])
        return {(r[0], r[1]) for r in cur.fetchall()}


# ── Rolling backtest ─────────────────────────────────────────────────

@dataclass
class CutoffRecord:
    cutoff: date
    train_window: int
    product_id: str
    store_id: str
    store_name: str
    store_type: Literal["BASE", "RETAIL"]
    actual: float
    pos_days: int
    calendar_days: int
    # forecasts
    preds: dict[str, float | None] = field(default_factory=dict)
    # coverage
    excluded: bool = False
    exclusion_reason: ExclusionReason | None = None


def rolling_backtest(
    conn,
    all_pids: list[str],
    store_nm: dict[str, str],
    *,
    train_window: int,
    horizon: int,
    cutoff_start: date,
    cutoff_end: date,
    min_pos_days: int = 2,
    step: int = 7,
) -> list[CutoffRecord]:
    """Rolling backtest: для каждого cutoff вычисляет coverage + все модели."""
    records: list[CutoffRecord] = []
    cutoff = cutoff_start

    while cutoff <= cutoff_end:
        train_from = cutoff - timedelta(train_window)
        train_to   = cutoff - timedelta(1)
        fcst_from  = cutoff
        fcst_to    = cutoff + timedelta(horizon - 1)

        # Загрузить всё разом
        sales_all = _load_sales(conn, all_pids,
                                min(train_from, fcst_from),
                                max(train_to, fcst_to))

        # Кандидаты: pid×sid с actual > 0 в forecast-периоде
        # (ретроспективное определение: «что реально продалось»)
        actual_pairs: set[tuple[str, str]] = {
            (pid, sid)
            for (pid, sid, d) in sales_all
            if fcst_from <= d <= fcst_to
        }

        # pid×sid с продажами в training-окне
        train_pairs: set[tuple[str, str]] = {
            (pid, sid)
            for (pid, sid, d) in sales_all
            if train_from <= d <= train_to
        }

        for pid, sid in actual_pairs:
            sname = store_nm.get(sid, sid)
            stype: Literal["BASE", "RETAIL"] = (
                "BASE" if any(k in sname.lower() for k in BASE_STORE_KEYWORDS)
                else "RETAIL"
            )
            actual = sum(
                sales_all.get((pid, sid, fcst_from + timedelta(j)), 0.0)
                for j in range(horizon)
            )

            # Тренировочный ряд
            train_days = [
                sales_all.get((pid, sid, train_from + timedelta(i)), 0.0)
                for i in range(train_window)
            ]
            pos_days = sum(1 for s in train_days if s > 0)

            # Причина исключения
            excluded = False
            reason: ExclusionReason | None = None

            if (pid, sid) not in train_pairs:
                excluded = True
                reason = "DATA_GAP"
            elif pos_days < min_pos_days:
                excluded = True
                reason = "INSUFFICIENT_POS_DAYS"

            preds = {} if excluded else _models(train_days, horizon)

            records.append(CutoffRecord(
                cutoff=cutoff,
                train_window=train_window,
                product_id=pid,
                store_id=sid,
                store_name=sname,
                store_type=stype,
                actual=actual,
                pos_days=pos_days,
                calendar_days=train_window,
                preds=preds,
                excluded=excluded,
                exclusion_reason=reason,
            ))

        cutoff += timedelta(step)

    return records


# ── Отчёт ────────────────────────────────────────────────────────────

MODEL_ORDER = ["median_pos", "mean_pos", "mean_cal", "median_cal", "croston"]

def _print_coverage(records: list[CutoffRecord], label: str) -> None:
    n_cand = len(records)
    n_pred = sum(1 for r in records if not r.excluded)
    n_excl = n_cand - n_pred
    ex_gap = sum(1 for r in records if r.exclusion_reason == "DATA_GAP")
    ex_pos = sum(1 for r in records if r.exclusion_reason == "INSUFFICIENT_POS_DAYS")
    pct = 100.0 * n_pred / n_cand if n_cand else 0
    print(f"\n  Coverage [{label}]:")
    print(f"    candidate_rows  = {n_cand}  (actual > 0 в forecast-периоде)")
    print(f"    predicted_rows  = {n_pred}  ({pct:.0f}%)")
    print(f"    excluded_rows   = {n_excl}")
    if ex_gap:
        print(f"      DATA_GAP              = {ex_gap}")
    if ex_pos:
        print(f"      INSUFFICIENT_POS_DAYS = {ex_pos}")


def _print_metrics_table(records: list[CutoffRecord]) -> None:
    predicted = [r for r in records if not r.excluded]
    if not predicted:
        print("    (нет предсказаний)")
        return
    hdr = f"  {'model':15s} {'n':6s} {'MAE':7s} {'MdAE':7s} {'WAPE':7s} {'absBias':9s} {'relBias':9s}"
    print(hdr)
    print("  " + "-" * 68)
    for m in MODEL_ORDER:
        m_preds = [(r, r.preds[m]) for r in predicted if r.preds.get(m) is not None]
        if not m_preds:
            print(f"  {m:15s}  —")
            continue
        errs = [p - r.actual for r, p in m_preds]
        acts = [r.actual for r, _ in m_preds]
        mt = _metrics(errs, acts)
        wape = f"{mt['WAPE_ratio']:.3f}" if mt.get("WAPE_ratio") is not None else "  N/A"
        rbias = f"{mt['rel_bias']:+.3f}" if mt.get("rel_bias") is not None else "  N/A"
        print(f"  {m:15s} {mt['n']:6d} {mt['MAE']:7.1f} {mt['MdAE']:7.1f} "
              f"{wape:>7s} {mt['abs_bias']:+9.1f} {rbias:>9s}")


def _run(conn, args) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT product_id FROM product_dim WHERE is_srezka = TRUE")
        all_pids = [r[0] for r in cur.fetchall()]

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT store_id, store_name FROM stock_snapshot")
        store_nm = {r[0]: r[1] for r in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute("SELECT MIN(day), MAX(day) FROM sales_by_product_day")
        hist_min, hist_max = cur.fetchone()

    print(f"История продаж: {hist_min} → {hist_max}")

    MAX_WINDOW = max(args.windows)
    cutoff_start = hist_min + timedelta(MAX_WINDOW)
    cutoff_end   = hist_max - timedelta(args.horizon)

    n_possible = max(0, (cutoff_end - cutoff_start).days // 7 + 1)

    print(f"Горизонт: {args.horizon}d  |  Окна: {args.windows}  |  step=7d")
    print(f"Cutoff диапазон: {cutoff_start} → {cutoff_end}  ({n_possible} возможных итераций)")

    print("\nNOTE: WAPE — ratio (не %). 0.39 = 39%. absBias = mean(pred−actual). "
          "relBias = sum(pred−actual)/sum(actual).\n")

    # Собрать результаты по окнам
    all_by_window: dict[int, list[CutoffRecord]] = {}
    _actual_iters: dict[int, int] = {}

    for window in args.windows:
        recs = rolling_backtest(
            conn, all_pids, store_nm,
            train_window=window,
            horizon=args.horizon,
            cutoff_start=cutoff_start,
            cutoff_end=cutoff_end,
        )
        n_iters = len({r.cutoff for r in recs})
        _actual_iters[window] = n_iters
        print(f"window={window:2d}: candidate={len(recs):5d}  "
              f"predicted={sum(1 for r in recs if not r.excluded):5d}  "
              f"iters={n_iters}")
        all_by_window[window] = recs

    max_actual_iters = max(_actual_iters.values()) if _actual_iters else 0
    status = ("INSUFFICIENT_BACKTEST_HISTORY"
              if max_actual_iters < MIN_ITERS_FOR_CONCLUSIONS else "OK")
    print(f"\n{'=' * 72}")
    print(f"STATUS: {status}")
    print(f"  Реальных итераций: {max_actual_iters}  (нужно ≥{MIN_ITERS_FOR_CONCLUSIONS})")
    if status == "INSUFFICIENT_BACKTEST_HISTORY":
        print(f"  Причина: разрыв данных sales_by_product_day ({hist_min} → {hist_max})")
        print(f"  Данные за окт 2025 – июн 2026 отсутствуют.")
        print(f"  Все метрики ниже — предварительные, не для принятия решений о модели.")
    print(f"{'=' * 72}\n")

    # ── SECTION 1: Вся сеть ─────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("ВСЯ СЕТЬ")
    print(f"{'─'*72}")
    for window in args.windows:
        recs = all_by_window[window]
        print(f"\n  window={window}d:")
        _print_coverage(recs, "вся сеть")
        _print_metrics_table(recs)

    # ── SECTION 2: BASE vs RETAIL ────────────────────────────────────
    for stype in ("BASE", "RETAIL"):
        print(f"\n{'─'*72}")
        print(f"{stype} (отдельная задача)")
        print(f"{'─'*72}")
        for window in args.windows:
            recs = [r for r in all_by_window[window] if r.store_type == stype]
            print(f"\n  window={window}d:")
            _print_coverage(recs, stype)
            _print_metrics_table(recs)

    # ── SECTION 3: Разбивка по складам (лучшее окно) ────────────────
    best_window = args.windows[1] if len(args.windows) > 1 else args.windows[0]
    print(f"\n{'─'*72}")
    print(f"PER-STORE BREAKDOWN  (window={best_window}d)")
    print(f"{'─'*72}")
    recs_bw = all_by_window[best_window]
    stores_seen = sorted({r.store_name for r in recs_bw})
    for sname in stores_seen:
        recs_s = [r for r in recs_bw if r.store_name == sname]
        print(f"\n  [{sname}]")
        _print_coverage(recs_s, sname[:30])
        _print_metrics_table(recs_s)

    # ── SECTION 4: median_pos vs mean_cal (ключевое сравнение) ──────
    print(f"\n{'─'*72}")
    print("POSITIVE-DAYS vs CALENDAR-DAYS (window=14d)")
    print("  median_pos: median(дней с продажами>0) × 7  — ЗАВЫШАЕТ для прерывистого")
    print("  mean_cal:   mean(всех дней включая нули) × 7 — корректнее для прерывистого")
    print(f"{'─'*72}")
    w14 = all_by_window[14] if 14 in all_by_window else all_by_window[args.windows[0]]
    for stype_f in (None, "BASE", "RETAIL"):
        label = stype_f or "ВСЯ СЕТЬ"
        recs_f = [r for r in w14 if stype_f is None or r.store_type == stype_f]
        predicted_f = [r for r in recs_f if not r.excluded]
        if not predicted_f:
            continue
        print(f"\n  {label}:")
        for m in ("median_pos", "mean_cal"):
            m_preds = [(r, r.preds[m]) for r in predicted_f if r.preds.get(m) is not None]
            if not m_preds:
                continue
            errs = [p - r.actual for r, p in m_preds]
            acts = [r.actual for r, _ in m_preds]
            mt = _metrics(errs, acts)
            wape = f"{mt['WAPE_ratio']:.3f}" if mt.get("WAPE_ratio") is not None else "N/A"
            rbias = f"{mt['rel_bias']:+.3f}" if mt.get("rel_bias") is not None else "N/A"
            print(f"    {m:15s}  n={mt['n']} MAE={mt['MAE']:.1f} "
                  f"WAPE={wape} absBias={mt['abs_bias']:+.1f} relBias={rbias}")

    # ── SECTION 5: Крупнейшие ошибки (window=14d, median_pos) ───────
    print(f"\n{'─'*72}")
    print("ТОП-12 АБСОЛЮТНЫХ ОШИБОК  (window=14d, median_pos)")
    print(f"{'─'*72}")
    w14_pred = [r for r in w14 if not r.excluded and r.preds.get("median_pos") is not None]
    top_err = sorted(w14_pred, key=lambda r: -abs(r.preds["median_pos"] - r.actual))[:12]
    for r in top_err:
        pred = r.preds["median_pos"]
        err = pred - r.actual
        with conn.cursor() as cur:
            cur.execute("SELECT product_name FROM product_dim WHERE product_id=%s", [r.product_id])
            row = cur.fetchone()
        pname = (row[0] if row else r.product_id)[:35]
        sname = r.store_name[:20]
        print(f"  {r.cutoff}  pred={pred:6.0f} act={r.actual:6.0f} err={err:+6.0f}"
              f"  {pname:35s}  {sname}")

    print("\nГотово.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon",  type=int, default=7)
    parser.add_argument("--windows",  type=int, nargs="+", default=[7, 14, 21, 28])
    args = parser.parse_args()

    conn = psycopg.connect(DATABASE_URL(),
                           options="-c default_transaction_read_only=on")
    try:
        _run(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
