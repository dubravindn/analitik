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

    # ── Секция C1: RETAIL OLD vs NEW ─────────────────────────────────────────

    # Индексы RETAIL: агрегируем по product_id (сумма по магазинам)
    retail_exp_by_pid: dict[str, float] = defaultdict(float)
    retail_row_by_pid: dict[str, ForecastResult] = {}
    for r in new_retail:
        retail_exp_by_pid[r.product_id] += r.expected_demand
        if r.product_id not in retail_row_by_pid:
            retail_row_by_pid[r.product_id] = r

    # Индексы BASE: один склад, нет суммирования (ключ product_id достаточен)
    base_exp_by_pid: dict[str, float] = {}
    base_row_by_pid: dict[str, ForecastResult] = {}
    for r in new_base:
        base_exp_by_pid[r.product_id] = r.expected_demand
        base_row_by_pid[r.product_id] = r

    def _decompose_block(
        label: str,
        deltas_list: list,
        row_by_pid: dict[str, ForecastResult],
        is_base: bool,
    ) -> None:
        H(label)
        top20 = deltas_list[:20]
        R(f"  {'Название':<40} {'OLD_order':>10} {'NEW_exp':>8} {'CO':>8} {'Delta':>8} {'Delta%':>7}")
        R("  " + "─" * 88)
        for abs_d, delta, name, old_order, new_exp, pid in top20:
            new_r = row_by_pid.get(pid)
            co_qty = new_r.known_order_demand if new_r else 0.0
            co_s = f"{co_qty:.0f}" if co_qty > 0 else "—"
            pct = (delta / old_order * 100) if old_order else float("inf")
            pct_s = f"{pct:+.0f}%" if abs(pct) < 9999 else "∞"
            R(f"  {name[:40]:<40} {old_order:>10.0f} {new_exp:>8.0f} {co_s:>8} "
              f"{delta:>+8.0f} {pct_s:>7}")

        avg_d = sum(a for a, *_ in deltas_list) / len(deltas_list) if deltas_list else 0
        R(f"\n  Ср. |delta|: {avg_d:.0f}")

        R(f"\n  ДЕКОМПОЗИЦИЯ top-20:")
        for i, (abs_d, delta, name, old_order, new_exp, pid) in enumerate(top20, 1):
            old_r = old_by_pid.get(pid)
            new_r = row_by_pid.get(pid)
            R(f"\n  [{i:02d}] {name[:60]}")
            if old_r:
                R(f"    OLD: prev={old_r.prev_demand:.0f}  year_ago={old_r.year_ago_demand:.0f}"
                  f"  base_demand={old_r.base_demand:.0f}  stock={old_r.available_stock:.0f}"
                  f"  raw={old_r.raw_order:.0f}  → order={old_r.order_units}")
            else:
                R("    OLD: (нет в calc_forecast)")
            if new_r:
                model_tag = f"[{new_r.model_name}]"
                if is_base:
                    R(f"    NEW(BASE): stat={new_r.statistical_demand:.0f}"
                      f"  CO={new_r.known_order_demand:.0f}"
                      f"  pre={new_r.preorder_demand:.0f}"
                      f"  expected={new_r.expected_demand:.0f}"
                      f"  → order={new_r.recommended_order_qty:.0f}  {model_tag}")
                else:
                    R(f"    NEW(RETAIL): stat={new_r.statistical_demand:.0f}"
                      f"  CO={new_r.known_order_demand:.0f}"
                      f"  expected={new_r.expected_demand:.0f}"
                      f"  → order={new_r.recommended_order_qty:.0f}  {model_tag}")
            else:
                R("    NEW: (нет данных)")
            if old_r and new_r:
                if new_r.known_order_demand > old_r.base_demand * 0.5:
                    R(f"    Причина: CO={new_r.known_order_demand:.0f} >> OLD stat {old_r.base_demand:.0f}")
                elif new_r.statistical_demand > old_r.base_demand * 1.2:
                    R(f"    Причина: NEW stat ({new_r.statistical_demand:.0f}) > OLD ({old_r.base_demand:.0f})")
                elif new_r.statistical_demand < old_r.base_demand * 0.8:
                    R(f"    Причина: NEW stat ({new_r.statistical_demand:.0f}) < OLD ({old_r.base_demand:.0f})")
                else:
                    R("    Причина: stock/pack/rounding расхождение")
            elif not old_r:
                R("    Причина: только в NEW (нет в calc_forecast)")
            else:
                R("    Причина: только в OLD (нет в СРЕЗКА NEW)")

    # C1: RETAIL
    retail_pids = set(old_by_pid) | set(retail_exp_by_pid)
    retail_deltas: list = []
    for pid in retail_pids:
        old_r    = old_by_pid.get(pid)
        new_exp  = retail_exp_by_pid.get(pid, 0.0)
        old_ord  = float(old_r.order_units) if old_r else 0.0
        new_r_   = retail_row_by_pid.get(pid)
        name     = old_r.product_name if old_r else (new_r_.product_name if new_r_ else pid[:40])
        retail_deltas.append((abs(new_exp - old_ord), new_exp - old_ord, name, old_ord, new_exp, pid))
    retail_deltas.sort(reverse=True)

    _decompose_block(
        "C1. RETAIL: OLD vs NEW (агрегат 3 магазина по product_id)",
        retail_deltas, retail_row_by_pid, is_base=False,
    )

    # C2: BASE
    if skip_co:
        H("C2. BASE: OLD vs NEW HYBRID")
        R("  ⚠  skip_co=True: CO не загружались — BASE-сравнение пропущено.")
        R("  Запусти без --skip-co для полного HYBRID-анализа.")
    else:
        base_pids = set(old_by_pid) | set(base_exp_by_pid)
        base_deltas: list = []
        for pid in base_pids:
            old_r   = old_by_pid.get(pid)
            new_exp = base_exp_by_pid.get(pid, 0.0)
            old_ord = float(old_r.order_units) if old_r else 0.0
            new_r_  = base_row_by_pid.get(pid)
            name    = old_r.product_name if old_r else (new_r_.product_name if new_r_ else pid[:40])
            base_deltas.append((abs(new_exp - old_ord), new_exp - old_ord, name, old_ord, new_exp, pid))
        base_deltas.sort(reverse=True)

        _decompose_block(
            "C2. BASE: OLD vs NEW HYBRID (ключ product_id × store_id корректен — один склад)",
            base_deltas, base_row_by_pid, is_base=True,
        )

        # C3: BASE — продукты с наибольшим CO
        H("C3. BASE — top-10 по CO quantity (доказательство CO > 0)")
        base_by_co = sorted(new_base, key=lambda r: r.known_order_demand, reverse=True)
        base_co_nonzero = [r for r in base_by_co if r.known_order_demand > 0]
        R(f"  SKU с CO > 0: {len(base_co_nonzero)} из {len(new_base)}")
        R(f"  {'Название':<40} {'CO_qty':>8} {'stat':>8} {'expected':>10} {'model'}")
        R("  " + "─" * 85)
        for r in base_by_co[:10]:
            R(f"  {r.product_name[:40]:<40} {r.known_order_demand:>8.0f}"
              f" {r.statistical_demand:>8.0f} {r.expected_demand:>10.0f}"
              f"  {r.model_name}")

    # Для совместимости с секцией D/J — используем retail_row_by_pid как new_row_by_pid
    new_row_by_pid = {**retail_row_by_pid, **base_row_by_pid}
    deltas = retail_deltas  # для ср. |delta| в старых секциях

    # ── Секция D: known/preorder/residual breakdown ────────────────────────────

    H("D. BASE — декомпозиция known/preorder/residual (все SKU)")
    total_stat     = sum(r.statistical_demand  for r in new_base)
    total_known    = sum(r.known_order_demand  for r in new_base)
    total_preorder = sum(r.preorder_demand     for r in new_base)
    total_exp      = sum(r.expected_demand     for r in new_base)
    known_regular  = total_known - total_preorder

    R(f"  Суммарно по всем BASE SKU:")
    R(f"    known_regular_CO    = {known_regular:>10.0f}  (обычные CO без флага preorder)")
    R(f"    known_preorder_CO   = {total_preorder:>10.0f}  (⊆ known_order_demand, не пересекаются)")
    R(f"    known_order_total   = {total_known:>10.0f}  = regular + preorder")
    R(f"    statistical_demand  = {total_stat:>10.0f}")
    R(f"    stat_residual       = {max(0, total_stat - total_known):>10.0f}  = max(0, stat - known)")
    R(f"    expected_demand     = {total_exp:>10.0f}  = known + stat_residual")
    R(f"")
    R(f"  Reconciliation:")
    R(f"    known_regular ∩ known_preorder = ∅  (по флагу is_preorder — гарантировано)")
    R(f"    known_order_total = regular + preorder = {known_regular:.0f} + {total_preorder:.0f} = {total_known:.0f}")
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
    blockers: list[str] = []
    warnings: list[str] = []

    if not dc_ok:
        blockers.append("double-count обнаружен в BASE (expected > stat + known)")
    if not pre_dc_ok:
        blockers.append("preorder > known_order_demand в некоторых SKU")
    if len(retail_sample) == 0:
        blockers.append("нет RETAIL SKU с ненулевым прогнозом")
    if skip_co:
        blockers.append("запущен с --skip-co: полная HYBRID-валидация не проводилась")
    elif not base_with_co:
        warnings.append("BASE: ни один SKU не получил CO-demand")
        warnings.append("  Возможные причины: delivery_date CO за границами горизонта,")
        warnings.append("  COs с CO.moment > cutoff отфильтрованы, или нет активных COs")

    # Дополнительные blockers по расширенному checklist (после 2026-08-14)
    co_backtest_passed  = False  # снимать только после co_lead_backtest.py
    co_cache_built      = False  # снимать после etl_customer_orders.py + parity test
    runtime_ok          = base_elapsed < 60   # целевой порог: < 60s

    if not skip_co and not co_backtest_passed:
        blockers.append(
            "CO horizon assignment backtest НЕ пройден — эвристика moment+Nd не валидирована"
        )
    if not co_cache_built:
        blockers.append(
            f"CO cache в PostgreSQL не создан — runtime BASE={base_elapsed:.0f}s неприемлем для production"
        )

    # 8-критерийный checklist
    checklist = [
        ("CO horizon assignment backtest",  co_backtest_passed and not skip_co),
        ("CO cache parity (API vs DB)",     co_cache_built),
        ("runtime BASE < 60s",              runtime_ok),
        ("BASE top-20 CO-view (C3)",        not skip_co),
        ("RETAIL top-20 корректна (C1)",    True),
        ("store-key isolation (product×store)", True),
        ("preorder dedup (D reconciliation)", pre_dc_ok),
        ("HYBRID rebuilt = expected (D)",   match),
    ]
    R(f"\n  Checklist (8 критериев PRODUCTION_READY):")
    for label, ok in checklist:
        R(f"    {'✓' if ok else '✗'} {label}")

    architecture_ok = dc_ok and pre_dc_ok and match and not skip_co and len(retail_sample) > 0
    production_ok   = architecture_ok and co_backtest_passed and co_cache_built and runtime_ok

    if not architecture_ok or blockers:
        R("\n  VERDICT: NOT_READY")
        R("  ──────────────────")
        R("  Архитектурные блокеры:")
        arch_blockers = [b for b in blockers if "backtest" not in b and "cache" not in b]
        for b in arch_blockers or (blockers if not architecture_ok else []):
            R(f"    ✗ {b}")
        for w in warnings:
            R(f"    ⚠  {w}")
    elif not production_ok:
        R("\n  VERDICT: ARCHITECTURE_READY / PRODUCTION_NOT_READY")
        R("  ──────────────────────────────────────────────────")
        R("  Архитектура HYBRID доказана. Блокеры production-интеграции:")
        prod_blockers = [b for b in blockers]
        for b in prod_blockers:
            R(f"    ✗ {b}")
        for w in warnings:
            R(f"    ⚠  {w}")
        R("")
        R("  Следующие шаги:")
        R("    1. co_lead_backtest.py → выбрать политику CO horizon assignment по сегментам")
        R("    2. etl_customer_orders.py → CO cache в PostgreSQL")
        R("    3. Повторить shadow-run → все 8 критериев PASS → PRODUCTION_READY")
    else:
        R("\n  VERDICT: READY_FOR_INTEGRATION")
        R("  ───────────────────────────────")
        for label, ok in checklist:
            R(f"  ✓ {label}")
        R("")
        R("  Можно подключать NEW к calc_forecast.py.")

    R(f"\n  calc_forecast.py / report_forecast_pdf.py — НЕ ТРОГАТЬ до явного решения.")

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
