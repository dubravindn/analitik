"""
Rolling backtest для базовых моделей прогноза.
Конфигурации: mean/median × window 7/14/21/28 дней.
Метрики: MAE, MdAE, WAPE, Bias — по всей сети, по типу точки.
Горизонт прогноза: 7 дней по умолчанию.

Ограничение: stock_snapshot доступен только с Aug 2026.
Поэтому backtest использует sales_by_product_day напрямую:
  - день с продажами = товар был в наличии (факт, а не предположение)
  - день без продаж = неизвестно (не считается в знаменателе demand)

Это не то же самое, что ForecastFeatures с POSITIVE_STOCK_OBSERVED,
но это единственный способ оценить историю глубже 10 дней.
По мере накопления snapshot-истории (≥28 дней) можно будет переключиться
на полный data layer через build_day_records + build_forecast_features.

Запуск:
    python3 scripts/run_backtest.py
    python3 scripts/run_backtest.py --horizon 7 --min-rolls 4
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

sys.path.insert(0, ".")
from hermes.config import DATABASE_URL
import psycopg


BASE_STORE_KEYWORDS = ("база", "base")


def store_type(store_name: str) -> Literal["BASE", "RETAIL"]:
    return "BASE" if any(k in store_name.lower() for k in BASE_STORE_KEYWORDS) else "RETAIL"


# ── Загрузка продаж ───────────────────────────────────────────────────

def _load_sales_range(conn, pids: list[str], d_from: date, d_to: date) -> defaultdict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT assortment_id, store_id, day, sell_qty
            FROM sales_by_product_day
            WHERE assortment_id = ANY(%s) AND day BETWEEN %s AND %s
        """, [pids, d_from, d_to])
        res: defaultdict = defaultdict(float)
        for r in cur.fetchall():
            res[(r[0], r[1], r[2])] = float(r[3] or 0)
    return res


# ── Метрики ───────────────────────────────────────────────────────────

def _metrics(errors: list[float], actuals: list[float]) -> dict:
    if not errors:
        return {}
    ae = [abs(e) for e in errors]
    wape_denom = sum(actuals)
    return {
        "n":    len(errors),
        "MAE":  round(sum(ae) / len(ae), 1),
        "MdAE": round(statistics.median(ae), 1),
        "WAPE": round(sum(ae) / wape_denom, 3) if wape_denom else None,
        "Bias": round(sum(errors) / len(errors), 1),
    }


# ── Rolling backtest ─────────────────────────────────────────────────

def _forecast(sales_in_window: list[float], horizon: int,
              method: str) -> float | None:
    """Прогноз: mean или median по дням с ненулевыми продажами × horizon."""
    positive = [s for s in sales_in_window if s > 0]
    if len(positive) < 2:
        return None
    if method == "median":
        daily = statistics.median(positive)
    else:
        daily = sum(positive) / len(positive)
    return round(daily * horizon, 1)


def rolling_backtest(
    conn,
    all_pids: list[str],
    store_nm: dict[str, str],
    *,
    train_window: int,
    horizon: int,
    cutoff_start: date,
    cutoff_end: date,
    step: int = 7,
) -> list[dict]:
    """Rolling backtest по sales_by_product_day.

    Для каждого cutoff в [cutoff_start, cutoff_end]:
      train: [cutoff - train_window, cutoff - 1]
      forecast: [cutoff, cutoff + horizon - 1]
    """
    results = []
    cutoff = cutoff_start

    while cutoff <= cutoff_end:
        train_from = cutoff - timedelta(train_window)
        train_to   = cutoff - timedelta(1)
        fcst_from  = cutoff
        fcst_to    = cutoff + timedelta(horizon - 1)

        # Загружаем продажи за оба периода разом
        load_from = train_from
        load_to   = fcst_to
        sales_all = _load_sales_range(conn, all_pids, load_from, load_to)

        # pid×sid в train-периоде (только те, у кого были продажи)
        ps_set: set[tuple[str, str]] = {
            (pid, sid)
            for (pid, sid, d) in sales_all
            if train_from <= d <= train_to
        }

        for pid, sid in ps_set:
            # продажи за тренировочный период (все дни, в т.ч. нулевые)
            train_days = []
            for i in range(train_window):
                d = train_from + timedelta(i)
                train_days.append(sales_all.get((pid, sid, d), 0.0))

            # фактические продажи за прогнозный период
            actual = sum(
                sales_all.get((pid, sid, fcst_from + timedelta(j)), 0.0)
                for j in range(horizon)
            )

            positive_train = sum(1 for s in train_days if s > 0)
            if positive_train < 2:
                continue

            results.append({
                "cutoff":       cutoff,
                "train_window": train_window,
                "product_id":   pid,
                "store_id":     sid,
                "store_type":   store_type(store_nm.get(sid, "")),
                "actual":       actual,
                "train_days":   train_days,
                "pos_days":     positive_train,
            })

        cutoff += timedelta(step)

    return results


def _run(conn, args) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT product_id FROM product_dim WHERE is_srezka = TRUE")
        all_pids = [r[0] for r in cur.fetchall()]

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (store_id) store_id, store_name FROM stock_snapshot")
        store_nm = {r[0]: r[1] for r in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute("SELECT MIN(day), MAX(day) FROM sales_by_product_day")
        hist_min, hist_max = cur.fetchone()

    print(f"История продаж: {hist_min} → {hist_max}")

    MAX_WINDOW = max(args.windows)
    cutoff_start = hist_min + timedelta(MAX_WINDOW)
    # не включаем незавершённый сегодняшний день (horizon отступ)
    cutoff_end = hist_max - timedelta(args.horizon)

    if cutoff_start > cutoff_end:
        print("Недостаточно истории.")
        return

    # Ограничиваем cutoff_end чтобы не брать слишком далёкое прошлое
    # (>= min_rolls итераций минимум)
    min_cutoff_end = cutoff_start + timedelta(args.min_rolls * 7)
    if cutoff_end < min_cutoff_end:
        print(f"Недостаточно итераций (need {args.min_rolls}, start={cutoff_start}, end={cutoff_end})")
        return

    print(f"Cutoff диапазон: {cutoff_start} → {cutoff_end}")
    print(f"Горизонт: {args.horizon}d  |  Окна: {args.windows}  |  step=7d")
    print(f"Метод: продажи из sales_by_product_day (snapshot-independent)\n")

    # Collect
    all_results: dict[str, list[dict]] = {}

    for window in args.windows:
        rows = rolling_backtest(
            conn, all_pids, store_nm,
            train_window=window,
            horizon=args.horizon,
            cutoff_start=cutoff_start,
            cutoff_end=cutoff_end,
        )
        n_iters = len({r["cutoff"] for r in rows})
        print(f"window={window:2d}: строк={len(rows):5d}  итераций={n_iters}")
        for method in ("median", "mean"):
            preds = []
            for r in rows:
                pred = _forecast(r["train_days"], args.horizon, method)
                if pred is None:
                    continue
                preds.append({**r, "pred": pred, "error": pred - r["actual"]})
            all_results[f"{method}-{window}"] = preds

    # ── Сводная таблица ───────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"ROLLING BACKTEST  horizon={args.horizon}d")
    print("=" * 72)

    hdr = "%-20s %6s %6s %6s %6s %+7s" % ("config", "n", "MAE", "MdAE", "WAPE", "Bias")

    for label, stype_filter in [("ВСЯ СЕТЬ", None), ("BASE", "BASE"), ("RETAIL", "RETAIL")]:
        print(f"\n── {label} ──")
        print(hdr)
        print("-" * 60)
        for key in sorted(all_results, key=lambda k: (k.split("-")[1], k.split("-")[0])):
            rows_f = all_results[key]
            if stype_filter:
                rows_f = [r for r in rows_f if r["store_type"] == stype_filter]
            if not rows_f:
                continue
            errs = [r["error"] for r in rows_f]
            acts = [r["actual"] for r in rows_f]
            m = _metrics(errs, acts)
            wape = ("%.3f" % m["WAPE"]) if m.get("WAPE") is not None else "  N/A"
            print("%-20s %6d %6.1f %6.1f %6s %+7.1f" % (
                key, m["n"], m["MAE"], m["MdAE"], wape, m["Bias"]))

    # ── Нулевой прогноз при ненулевом факте ─────────────────────────
    ref_key = "median-14" if "median-14" in all_results else sorted(all_results)[0]
    zero_cases = [r for r in all_results[ref_key] if r["pred"] == 0 and r["actual"] > 0]
    all_cases  = all_results[ref_key]
    print(f"\n── НУЛЕВОЙ ПРОГНОЗ / НЕНУЛЕВОЙ ФАКТ ({ref_key}) ──")
    print(f"Таких строк: {len(zero_cases)} / {len(all_cases)} = "
          f"{len(zero_cases)/len(all_cases)*100:.1f}%")
    for r in sorted(zero_cases, key=lambda x: -x["actual"])[:8]:
        sn = store_nm.get(r["store_id"], r["store_id"])[:22]
        with conn.cursor() as cur:
            cur.execute("SELECT product_name FROM product_dim WHERE product_id=%s",
                        [r["product_id"]])
            row_p = cur.fetchone()
        pn = (row_p[0] if row_p else r["product_id"])[:30]
        print(f"  cutoff={r['cutoff']}  act={r['actual']:5.0f}  pos_days={r['pos_days']:2d}"
              f"  {pn:30s}  {sn}")

    # ── Топ-10 абсолютных ошибок ─────────────────────────────────────
    print(f"\n── ТОП-10 ОШИБОК (abs) в {ref_key} ──")
    top_err = sorted(all_results[ref_key], key=lambda x: -abs(x["error"]))[:10]
    for r in top_err:
        sn = store_nm.get(r["store_id"], r["store_id"])[:20]
        with conn.cursor() as cur:
            cur.execute("SELECT product_name FROM product_dim WHERE product_id=%s",
                        [r["product_id"]])
            row_p = cur.fetchone()
        pn = (row_p[0] if row_p else r["product_id"])[:35]
        print(f"  {r['cutoff']}  pred={r['pred']:6.0f}  act={r['actual']:6.0f}"
              f"  err={r['error']:+6.0f}  {pn:35s}  {sn}")

    print("\nГотово.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon",      type=int, default=7)
    parser.add_argument("--windows",      type=int, nargs="+", default=[7, 14, 21, 28])
    parser.add_argument("--min-rolls",    type=int, default=4,
                        help="Минимум rolling-итераций для результата")
    args = parser.parse_args()

    conn = psycopg.connect(DATABASE_URL(), options="-c default_transaction_read_only=on")
    try:
        _run(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
