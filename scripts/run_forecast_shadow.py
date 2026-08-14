#!/usr/bin/env python3
"""
Shadow forecast runner — OLD vs NEW на реальных данных (read-only).

Запуск:
  python3 scripts/run_forecast_shadow.py
  python3 scripts/run_forecast_shadow.py --date 2026-08-18 --horizon 7 --skip-co --out /tmp/shadow.txt

Флаги:
  --date DATE     cutoff date (default: today)
  --horizon N     дней горизонта (default: 7)
  --limit N       максимум SKU в секциях A/B (default: 10)
  --skip-co       не загружать CustomerOrders из API (быстрый прогон)
  --out FILE      дополнительно писать в файл

production не трогает. Telegram/PDF не меняет.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import hermes.config as config
import hermes.db as db
from hermes.calc_forecast import build_forecast, ForecastRow
from hermes.forecast_base import build_base_hybrid_forecast
from hermes.forecast_data import load_store_names
from hermes.forecast_models import Channel, DataFlag, ForecastMode, ForecastResult
from hermes.forecast_orders import LARGE_ORDER_THRESHOLD_DEFAULT
from hermes.forecast_retail import build_retail_forecast

BASE_API = "https://api.moysklad.ru/api/remap/1.2"

RETAIL_STORES = [sid for sid, ch in config.STORE_CHANNELS.items() if ch == "розница"]
BASE_STORES   = [sid for sid, ch in config.STORE_CHANNELS.items() if ch == "опт"]


# ── Вывод ─────────────────────────────────────────────────────────────────────

def run(
    conn,
    cutoff:   date,
    horizon:  int,
    skip_co:  bool,
    limit:    int,
    out_lines: list[str],
) -> dict:
    token = config.MOYSKLAD_TOKEN()
    store_names_map = load_store_names(conn)

    horizon_from = cutoff + timedelta(days=1)
    horizon_to   = cutoff + timedelta(days=horizon)

    def R(*args):
        line = " ".join(str(a) for a in args)
        print(line); out_lines.append(line)

    def H(title: str):
        s = f"\n{'─'*72}\n{title}\n{'─'*72}"
        print(s); out_lines.append(s)

    R(f"SHADOW FORECAST  cutoff={cutoff}  horizon={horizon_from}..{horizon_to}")
    R(f"  skip_co={skip_co}  large_order_threshold={LARGE_ORDER_THRESHOLD_DEFAULT}")
    R(f"  git: see `git log -1 --oneline`")

    # ── OLD forecast (build_forecast — store-agnostic, SREZKA only) ───────────

    H("OLD forecast (calc_forecast.build_forecast)")
    t0 = time.time()
    try:
        old_rows: list[ForecastRow] = build_forecast(conn, horizon_from, horizon_to)
    except Exception as e:
        R(f"  ERROR: {e}")
        old_rows = []
    old_elapsed = time.time() - t0
    R(f"  OLD rows: {len(old_rows)}  elapsed: {old_elapsed:.1f}s")
    R(f"  Примечание: OLD агрегирует все склады вместе.")

    # Индекс OLD по product_id
    old_by_pid: dict[str, ForecastRow] = {}
    for row in old_rows:
        old_by_pid[row.product_id] = row

    # ── NEW forecast — RETAIL ─────────────────────────────────────────────────

    H("NEW forecast — RETAIL (forecast_retail.build_retail_forecast)")
    t1 = time.time()
    new_retail: list[ForecastResult] = []
    for sid in RETAIL_STORES:
        sname = store_names_map.get(sid, sid)
        try:
            rows = build_retail_forecast(conn, sid, cutoff, horizon_from, horizon_to)
            new_retail.extend(rows)
            R(f"  {sname}: {len(rows)} SKU")
        except Exception as e:
            R(f"  {sname}: ERROR {e}")
    retail_elapsed = time.time() - t1
    R(f"  Итого RETAIL: {len(new_retail)}  elapsed: {retail_elapsed:.1f}s")

    # ── NEW forecast — BASE ───────────────────────────────────────────────────

    H("NEW forecast — BASE (forecast_base.build_base_hybrid_forecast)")
    t2 = time.time()
    new_base: list[ForecastResult] = []
    for sid in BASE_STORES:
        sname = store_names_map.get(sid, sid)
        store_href = f"{BASE_API}/entity/store/{sid}"
        try:
            if skip_co:
                # Без CO: используем retail-движок с 28-дневным окном как прокси
                from hermes.forecast_retail import build_retail_forecast as _rf
                rows = _rf(conn, sid, cutoff, horizon_from, horizon_to, window_days=28)
                for r in rows:
                    r.channel = Channel.BASE
                    r.model_name = "mean_cal_28(skip_co)"
            else:
                rows = build_base_hybrid_forecast(
                    conn=conn,
                    token=token,
                    store_id=sid,
                    store_href=store_href,
                    cutoff_date=cutoff,
                    horizon_from=horizon_from,
                    horizon_to=horizon_to,
                )
            new_base.extend(rows)
            R(f"  {sname}: {len(rows)} SKU")
        except Exception as e:
            R(f"  {sname}: ERROR {e}")
    base_elapsed = time.time() - t2
    R(f"  Итого BASE: {len(new_base)}  elapsed: {base_elapsed:.1f}s")

    # ── Секция A: 10 RETAIL SKU (декомпозиция) ────────────────────────────────

    H("A. NEW RETAIL — декомпозиция (10 SKU с ненулевым прогнозом)")
    R(f"  {'Название':<40} {'Магазин':<22} {'Модель':<18}"
      f" {'Stat':>6} {'CO':>6} {'Pre':>5} {'Exp':>6} {'Flg'}")
    R("  " + "─" * 115)

    retail_nonzero = [r for r in new_retail if r.expected_demand > 0]
    retail_sample  = retail_nonzero[:limit]
    for r in retail_sample:
        flags = "+".join(f.value for f in r.data_quality_flags) or "—"
        R(f"  {r.product_name[:40]:<40} {r.store_name[:22]:<22} {r.model_name[:18]:<18}"
          f" {r.statistical_demand:>6.0f} {r.known_order_demand:>6.0f}"
          f" {r.preorder_demand:>5.0f} {r.expected_demand:>6.0f} {flags}")

    R(f"\n  Нулевой прогноз: {len(new_retail) - len(retail_nonzero)} SKU "
      f"(no_sales_history или below_oper_start)")

    # ── Секция B: 10 BASE SKU (декомпозиция) ──────────────────────────────────

    H("B. NEW BASE — декомпозиция HYBRID (10 SKU)")
    R(f"  {'Название':<40} {'Модель':<28}"
      f" {'Stat':>7} {'CO':>7} {'Pre':>5} {'Exp':>7} {'Flg'}")
    R("  " + "─" * 115)

    base_sample  = new_base[:limit]
    for r in base_sample:
        flags = "+".join(f.value for f in r.data_quality_flags) or "—"
        R(f"  {r.product_name[:40]:<40} {r.model_name[:28]:<28}"
          f" {r.statistical_demand:>7.0f} {r.known_order_demand:>7.0f}"
          f" {r.preorder_demand:>5.0f} {r.expected_demand:>7.0f} {flags}")

    # BASE с CO
    base_with_co = [r for r in new_base if r.known_order_demand > 0]
    base_with_pre = [r for r in new_base if r.preorder_demand > 0]
    R(f"\n  Всего BASE: {len(new_base)}  с CO: {len(base_with_co)}  с preorder: {len(base_with_pre)}")

    # ── Секция C: OLD vs NEW — top-20 расхождений ─────────────────────────────

    H("C. OLD vs NEW — топ расхождений (агрегат по product_id)")

    # Агрегируем NEW RETAIL по product_id
    new_by_pid: dict[str, float] = defaultdict(float)
    for r in new_retail + new_base:
        new_by_pid[r.product_id] += r.expected_demand

    # Совмещаем
    all_pids = set(old_by_pid) | set(new_by_pid)
    deltas = []
    for pid in all_pids:
        old_r = old_by_pid.get(pid)
        new_exp = new_by_pid.get(pid, 0.0)
        old_order = float(old_r.order_units) if old_r else 0.0
        old_name = old_r.product_name if old_r else pid[:40]
        delta = new_exp - old_order
        deltas.append((abs(delta), delta, old_name, old_order, new_exp, pid))

    deltas.sort(reverse=True)
    top20 = deltas[:20]

    R(f"  {'Название':<40} {'OLD_order':>10} {'NEW_exp':>8} {'Delta':>8} {'Delta%':>7}")
    R("  " + "─" * 80)
    for abs_d, delta, name, old_order, new_exp, pid in top20:
        pct = (delta / old_order * 100) if old_order else float("inf")
        pct_s = f"{pct:+.0f}%" if abs(pct) < 9999 else "∞"
        R(f"  {name[:40]:<40} {old_order:>10.0f} {new_exp:>8.0f} "
          f"{delta:>+8.0f} {pct_s:>7}")

    R(f"\n  Ср. |delta|: {sum(a for a, *_ in deltas) / len(deltas):.0f}" if deltas else "")

    # ── Секция D: known/preorder/residual breakdown ────────────────────────────

    H("D. BASE — декомпозиция known/preorder/residual (все SKU)")
    total_stat    = sum(r.statistical_demand  for r in new_base)
    total_known   = sum(r.known_order_demand  for r in new_base)
    total_preorder = sum(r.preorder_demand    for r in new_base)
    total_exp     = sum(r.expected_demand     for r in new_base)

    R(f"  Суммарно по всем BASE SKU:")
    R(f"    statistical_demand  = {total_stat:>10.0f}")
    R(f"    known_order_demand  = {total_known:>10.0f}")
    R(f"    preorder_demand     = {total_preorder:>10.0f}  (⊆ known_order_demand)")
    R(f"    expected_demand     = {total_exp:>10.0f}")
    R(f"")
    R(f"  Проверка формулы HYBRID:")
    R(f"    expected = known + max(0, stat - known)")
    rebuilt = sum(
        r.known_order_demand + max(0.0, r.statistical_demand - r.known_order_demand)
        for r in new_base
    )
    R(f"    rebuilt  = {rebuilt:>10.0f}")
    match = abs(rebuilt - total_exp) < 1.0
    R(f"    MATCH: {'✓ OK' if match else '✗ РАСХОЖДЕНИЕ'}")

    # ── Секция E: double-count проверка ───────────────────────────────────────

    H("E. Double-count check")
    dc_ok = True
    violations: list[str] = []
    for r in new_base:
        naive = r.statistical_demand + r.known_order_demand
        if r.expected_demand > naive + 0.01:
            dc_ok = False
            violations.append(
                f"{r.product_name[:40]}: expected={r.expected_demand:.0f} "
                f"> stat+known={naive:.0f}"
            )
    if dc_ok:
        R("  ✓ Double-count не обнаружен (expected <= stat + known для всех SKU)")
    else:
        R(f"  ✗ НАРУШЕНИЕ double-count: {len(violations)} SKU")
        for v in violations[:5]:
            R(f"    {v}")

    # preorder ⊆ known проверка
    pre_dc_ok = all(r.preorder_demand <= r.known_order_demand + 0.01 for r in new_base)
    R(f"  {'✓' if pre_dc_ok else '✗'} preorder ⊆ known_order_demand")

    # ── Секция F: UNKNOWN stock/incoming ──────────────────────────────────────

    H("F. UNKNOWN stock / incoming")
    all_results = new_retail + new_base
    unknown_stock    = [r for r in all_results if r.available_stock is None]
    unknown_incoming = [r for r in all_results if r.incoming_qty is None]
    no_stock_flag    = [r for r in all_results if DataFlag.NO_STOCK_DATA in r.data_quality_flags]

    R(f"  available_stock = None: {len(unknown_stock)} SKU")
    R(f"  incoming_qty    = None: {len(unknown_incoming)} SKU  (не-0, а UNKNOWN)")
    R(f"  DataFlag.NO_STOCK_DATA: {len(no_stock_flag)} SKU")
    R(f"  Примечание: в v1 stock_snapshot ещё не загружается — "
      f"replenishment рассчитан без вычета остатка.")

    # ── Секция G: pack rounding ───────────────────────────────────────────────

    H("G. Pack rounding (pack_size > 1)")
    with_packs = [r for r in all_results if r.pack_size > 1]
    R(f"  SKU с pack_size > 1: {len(with_packs)}")
    if with_packs:
        R(f"  {'Название':<40} {'pack_size':>10} {'raw':>8} {'rounded':>10}")
        for r in with_packs[:10]:
            R(f"  {r.product_name[:40]:<40} {r.pack_size:>10} "
              f"{r.raw_order_qty:>8.0f} {r.recommended_order_qty:>10.0f}")

    # ── Секция H: ошибки и флаги ──────────────────────────────────────────────

    H("H. Флаги качества данных")
    from collections import Counter
    flag_counts: Counter = Counter()
    for r in all_results:
        for f in r.data_quality_flags:
            flag_counts[f.value] += 1

    for flag, cnt in sorted(flag_counts.items(), key=lambda x: -x[1]):
        R(f"  {flag:<30}: {cnt}")

    # ── Секция I: runtime ──────────────────────────────────────────────────────

    H("I. Runtime")
    R(f"  OLD (build_forecast)  : {old_elapsed:.1f}s")
    R(f"  NEW RETAIL            : {retail_elapsed:.1f}s  ({len(new_retail)} SKU)")
    R(f"  NEW BASE              : {base_elapsed:.1f}s  ({len(new_base)} SKU, skip_co={skip_co})")

    # ── Секция J: вывод ───────────────────────────────────────────────────────

    H("J. Вывод")
    issues: list[str] = []
    if not dc_ok:
        issues.append("double-count обнаружен в BASE")
    if not pre_dc_ok:
        issues.append("preorder > known в некоторых SKU")
    if len(retail_sample) == 0:
        issues.append("нет RETAIL SKU с ненулевым прогнозом")

    if issues:
        R("  ✗ БЛОКЕРЫ перед подключением к production:")
        for issue in issues:
            R(f"    - {issue}")
    else:
        R("  ✓ Базовые проверки пройдены.")

    if skip_co:
        R("  ⚠  Запущен с --skip-co: BASE-прогноз без CustomerOrders.")
        R("     Запусти без --skip-co для реального HYBRID.")
    elif not base_with_co:
        R("  ⚠  BASE: ни один SKU не получил CO-demand.")
        R("     Возможные причины:")
        R("     - deliveredBy не заполнен в COs")
        R("     - горизонт не пересекается с delivery_date CO")
        R("     - нет активных COs на cutoff")

    R(f"\n  calc_forecast.py — НЕ МЕНЯТЬ до завершения shadow-валидации.")
    R(f"  Следующий шаг: убедиться что known_order_demand > 0 на реальном горизонте,")
    R(f"  или реализовать fallback через demand-историю CO как proxy.")

    # ── JSON дамп ─────────────────────────────────────────────────────────────

    return {
        "cutoff": str(cutoff),
        "horizon_from": str(horizon_from),
        "horizon_to": str(horizon_to),
        "skip_co": skip_co,
        "large_order_threshold": LARGE_ORDER_THRESHOLD_DEFAULT,
        "old_count": len(old_rows),
        "new_retail_count": len(new_retail),
        "new_base_count": len(new_base),
        "double_count_ok": dc_ok,
        "preorder_subset_ok": pre_dc_ok,
        "flag_counts": dict(flag_counts),
        "top20_deltas": [
            {
                "product_name": name,
                "old_order": old_order,
                "new_expected": new_exp,
                "delta": delta,
            }
            for abs_d, delta, name, old_order, new_exp, _ in top20
        ],
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Shadow forecast: OLD vs NEW")
    parser.add_argument("--date",    default=str(date.today()),
                        help="Cutoff date YYYY-MM-DD (default: today)")
    parser.add_argument("--horizon", type=int, default=7,
                        help="Forecast horizon days (default: 7)")
    parser.add_argument("--limit",   type=int, default=10,
                        help="SKU limit in sections A/B (default: 10)")
    parser.add_argument("--skip-co", action="store_true",
                        help="Skip CustomerOrder API calls (faster, no HYBRID for BASE)")
    parser.add_argument("--out",     default="/tmp/forecast_shadow.txt")
    parser.add_argument("--json",    default="/tmp/forecast_shadow.json")
    args = parser.parse_args()

    cutoff = date.fromisoformat(args.date)
    conn   = db.connect(config.DATABASE_URL())
    conn.autocommit = True

    out_lines: list[str] = []
    result = run(
        conn=conn,
        cutoff=cutoff,
        horizon=args.horizon,
        skip_co=args.skip_co,
        limit=args.limit,
        out_lines=out_lines,
    )

    Path(args.out).write_text("\n".join(out_lines), encoding="utf-8")
    if result:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    print(f"\nОтчёт: {args.out}")
    print(f"JSON:  {args.json}")
    conn.close()


if __name__ == "__main__":
    main()
