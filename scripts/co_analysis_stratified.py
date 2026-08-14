#!/usr/bin/env python3
"""
CO-анализ BASE — стратифицированная полная версия (read-only).

Stratified sample: NORMAL (Jan/Apr/May/Jun/Jul/Aug) + MARCH_8 + NEW_YEAR + VALENTINE + крупные.
SKU-level: demand positions → is_srezka, qty, lead_days, period_type.
Три режима: STAT_ONLY / KNOWN_ONLY / HYBRID (residual без double-count).
Итог: procurement_lead, LARGE_ORDER threshold, coverage, recommendation.

Запуск (~20 мин с позициями):
  python3 scripts/co_analysis_stratified.py --out /tmp/co_stratified.txt
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
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import hermes.config as config
import hermes.db as db

BASE_API   = "https://api.moysklad.ru/api/remap/1.2"
HORIZON    = 7
EVAL_START = date(2026, 1, 5)

SREZKA_KW = [
    "роза", "тюльпан", "пион", "гвоздик", "хризантем", "лили", "ирис",
    "фрезия", "орхидея", "антуриум", "гербер", "альстромер", "эустома",
    "гладиол", "нарцисс", "гиацинт", "ранункул", "мимоза", "верон",
    "мускар", "гипсофил", "зелень", "кустов", "спрей", "срез", "хам",
]
NON_SREZKA_KW = [
    "шар", "упаков", "лента", "открытк", "корзин", "ваза", "ножниц",
    "губк", "целлоф", "крафт", "флориз", "инструм", "предоплат",
    "ящик", "пакет", "сетк",
]

def is_srezka(name: str) -> bool:
    n = name.lower()
    if any(k in n for k in NON_SREZKA_KW):
        return False
    return any(k in n for k in SREZKA_KW)


HOLIDAY_WINDOWS = [
    ((2, 11), (2, 17), "VALENTINE"),
    ((3, 1),  (3, 10), "MARCH_8"),
    ((12, 25),(12, 31), "NEW_YEAR"),
    ((1, 1),  (1, 10), "NEW_YEAR"),
]

def period_of(d: date) -> str:
    for (m0, d0), (m1, d1), lbl in HOLIDAY_WINDOWS:
        if date(d.year, m0, d0) <= d <= date(d.year, m1, d1):
            return lbl
    return "NORMAL"

def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# ── API ────────────────────────────────────────────────────────────────────────

def _get(token: str, path: str, params: dict | None = None) -> dict:
    url = BASE_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("User-Agent", "hermes-co-strat/1.0")
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


def paginate(token: str, path: str, base_params: dict,
             label: str = "", delay: float = 0.25) -> list[dict]:
    rows: list[dict] = []
    offset, total = 0, None
    while True:
        r = _get(token, path, {**base_params, "limit": 100, "offset": offset})
        batch = r.get("rows", [])
        rows.extend(batch)
        if total is None:
            total = r.get("meta", {}).get("size", 0)
        offset += len(batch)
        if label and offset % 500 < 100 and offset > 0:
            print(f"    {label}: {offset}/{total}")
        if not batch or offset >= (total or 0):
            break
        time.sleep(delay)
    return rows


# ── Типы ─────────────────────────────────────────────────────────────────────

class DemandRec:
    __slots__ = ("did", "day", "sum_kop", "co_id", "co_day", "lead", "period")
    def __init__(self, doc: dict):
        m = doc.get("moment", "")
        self.did     = doc["id"]
        self.day     = date.fromisoformat(m[:10]) if m else None
        self.sum_kop = int(doc.get("sum", 0))
        co = doc.get("customerOrder") or {}
        cm = co.get("moment", "")
        self.co_id   = co.get("id") if cm else None
        self.co_day  = date.fromisoformat(cm[:10]) if cm else None
        self.lead    = (self.day - self.co_day).days if (self.day and self.co_day) else None
        self.period  = period_of(self.day) if self.day else "UNKNOWN"


# ── Загрузка ──────────────────────────────────────────────────────────────────

def load_demands(token: str, store_href: str) -> list[DemandRec]:
    print("  Загружаем demand (expand=customerOrder)...")
    rows = paginate(token, "/entity/demand", {
        "filter": f"store={store_href};moment>=2025-10-01 00:00:00;moment<=2026-08-14 23:59:59",
        "expand": "customerOrder",
    }, label="demand")
    print(f"  Загружено {len(rows)} demand-документов")
    result = []
    for doc in rows:
        try:
            r = DemandRec(doc)
            if r.day and r.day >= EVAL_START:
                result.append(r)
        except Exception:
            pass
    return result


def load_demand_positions(token: str, demand_id: str) -> list[dict]:
    r = _get(token, f"/entity/demand/{demand_id}/positions",
             {"expand": "assortment", "limit": 100})
    result = []
    for pos in r.get("rows", []):
        a = pos.get("assortment") or {}
        result.append({
            "aid":   a.get("id", "?"),
            "name":  a.get("name", "?"),
            "qty":   float(pos.get("quantity", 0)),
            "price": int(pos.get("price", 0)),
        })
    return result


def load_store_daily_rev(conn, store_id: str) -> dict[date, float]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, SUM(revenue_kop)
            FROM sales_by_store_day
            WHERE store_id = %s AND day BETWEEN '2025-10-01' AND '2026-08-14'
            GROUP BY day
        """, (store_id,))
        return {r[0]: float(r[1]) for r in cur.fetchall()}


# ── Стратифицированная выборка ─────────────────────────────────────────────────

NORMAL_MONTHS = {1, 4, 5, 6, 7, 8}   # месяцы, считающиеся NORMAL в истории BASE

def build_stratified_sample(demands: list[DemandRec],
                              per_period: int = 80) -> list[DemandRec]:
    """
    Стратифицированная выборка demand-документов для позиционного анализа.

    NORMAL: равномерно по месяцам (Jan, Apr, May, Jun, Jul, Aug)
    MARCH_8, NEW_YEAR, VALENTINE: все доступные
    Large: топ-100 по сумме из NORMAL-периода
    """
    by_period: dict[str, list[DemandRec]] = defaultdict(list)
    for d in demands:
        by_period[d.period].append(d)

    sample_ids: set[str] = set()

    # Holiday periods — все
    for period in ("MARCH_8", "NEW_YEAR", "VALENTINE"):
        for d in by_period.get(period, []):
            sample_ids.add(d.did)

    # NORMAL — равномерно по месяцам
    normal_by_month: dict[int, list[DemandRec]] = defaultdict(list)
    for d in by_period.get("NORMAL", []):
        if d.day and d.day.month in NORMAL_MONTHS:
            normal_by_month[d.day.month].append(d)
    n_months = len([m for m in NORMAL_MONTHS if normal_by_month.get(m)])
    per_month = max(1, per_period // max(n_months, 1))
    for month, docs in sorted(normal_by_month.items()):
        # Сортируем по дате, берём равномерно
        sorted_docs = sorted(docs, key=lambda d: d.day)
        step = max(1, len(sorted_docs) // per_month)
        for i in range(0, len(sorted_docs), step):
            if len([did for did in sample_ids if True]) < per_period * 5:
                sample_ids.add(sorted_docs[i].did)

    # Крупные заказы (топ-100 по выручке из NORMAL)
    normal_by_sum = sorted(by_period.get("NORMAL", []), key=lambda d: -d.sum_kop)
    for d in normal_by_sum[:100]:
        sample_ids.add(d.did)

    result = [d for d in demands if d.did in sample_ids]
    print(f"  Стратифицированная выборка: {len(result)} документов")
    by_p: dict[str, int] = defaultdict(int)
    for d in result:
        by_p[d.period] += 1
    for p, n in sorted(by_p.items()):
        print(f"    {p}: {n}")
    return result


def load_positions_for_sample(token: str, sample: list[DemandRec]) -> list[dict]:
    """
    Загружает позиции для выборки. Возвращает:
    [{demand_day, period, co_day, lead, aid, name, qty, is_srezka, demand_sum}]
    """
    results: list[dict] = []
    print(f"  Загружаем позиции для {len(sample)} документов...")
    for i, dem in enumerate(sample):
        positions = load_demand_positions(token, dem.did)
        for pos in positions:
            results.append({
                "demand_day": dem.day,
                "period":     dem.period,
                "co_day":     dem.co_day,
                "lead":       dem.lead,
                "demand_sum": dem.sum_kop,
                "aid":        pos["aid"],
                "name":       pos["name"],
                "qty":        pos["qty"],
                "is_srezka":  is_srezka(pos["name"]),
            })
        if (i + 1) % 50 == 0:
            print(f"    ...позиции {i+1}/{len(sample)}")
        time.sleep(0.15)
    print(f"  Позиций загружено: {len(results)}")
    return results


# ── Метрики ────────────────────────────────────────────────────────────────────

def metrics(actuals: list[float], preds: list[float]) -> dict:
    if not actuals:
        return {}
    errs = [p - a for p, a in zip(preds, actuals)]
    ae   = [abs(e) for e in errs]
    s    = sum(actuals)
    n    = len(errs)
    return {
        "n": n,
        "MAE": round(sum(ae) / n, 0),
        "WAPE": round(sum(ae) / s, 3) if s else None,
        "bias": round(sum(errs) / s, 3) if s else None,
        "P90": round(sorted(ae)[int(0.9 * n)], 0),
        "over%": round(100 * sum(1 for e in errs if e > 0) / n, 1),
        "under%": round(100 * sum(1 for e in errs if e < 0) / n, 1),
    }


# ── Основной анализ ────────────────────────────────────────────────────────────

def run(token: str, conn, store_id: str, store_href: str,
        store_name: str, out_lines: list[str]) -> None:

    def R(*args):
        line = " ".join(str(a) for a in args)
        print(line); out_lines.append(line)

    def H(title: str):
        s = f"\n{'─'*70}\n{title}\n{'─'*70}"
        print(s); out_lines.append(s)

    # Загрузка
    demands = load_demands(token, store_href)
    store_daily_rev = load_store_daily_rev(conn, store_id)

    valid = [d for d in demands if d.day]
    with_co = [d for d in valid if d.co_id and d.lead is not None]
    R(f"BASE demand (eval period): {len(valid)} docs  CO-linked: {len(with_co)} ({100*len(with_co)/len(valid):.1f}%)")

    # Стратифицированная выборка + позиции
    sample = build_stratified_sample(demands)
    pos_all = load_positions_for_sample(token, sample)

    # ── A. CO-покрытие ────────────────────────────────────────────────────
    H("A. CO-покрытие BASE demand (все документы, eval >= 2026-01-05)")
    total_rev = sum(d.sum_kop for d in valid)
    co_rev    = sum(d.sum_kop for d in valid if d.co_id)
    R(f"  Всего документов : {len(valid)}")
    R(f"  С CO             : {len(with_co)} ({100*len(with_co)/len(valid):.1f}%)  "
      f"rev={co_rev/100:,.0f} р  ({100*co_rev/total_rev:.1f}%)")
    R(f"  Без CO           : {len(valid)-len(with_co)} ({100*(len(valid)-len(with_co))/len(valid):.1f}%)")

    # ── B. Lead-time distribution (rev + docs) ────────────────────────────
    H("B. Lead-time distribution — выручка-взвешенная (все doc CO-linked)")
    n_co = len(with_co)
    tot_co_rev = sum(d.sum_kop for d in with_co) or 1
    R(f"  {'Lead >= N дн':14s} {'docs%':>7s} {'rev%':>7s}  |  NORMAL_rev%  MARCH8_rev%")

    def rev_pct_period(threshold, period_filter=None):
        sub = [d for d in with_co
               if d.lead >= threshold and (period_filter is None or d.period == period_filter)]
        base_rev = sum(d.sum_kop for d in with_co
                       if period_filter is None or d.period == period_filter) or 1
        return 100 * sum(d.sum_kop for d in sub) / base_rev

    for th in (0, 1, 2, 3, 5, 7, 10, 14, 21):
        n   = sum(1 for d in with_co if d.lead >= th)
        rev = sum(d.sum_kop for d in with_co if d.lead >= th)
        n_pct   = 100 * n / n_co
        rev_pct = 100 * rev / tot_co_rev
        normal_pct = rev_pct_period(th, "NORMAL")
        march_pct  = rev_pct_period(th, "MARCH_8")
        R(f"  >= {th:2d} дн          {n_pct:>6.1f}% {rev_pct:>6.1f}%  |  "
          f"{normal_pct:>10.1f}%  {march_pct:>10.1f}%")

    leads = [d.lead for d in with_co]
    R(f"\n  Медиана={statistics.median(leads):.0f}  P25={sorted(leads)[n_co//4]}  "
      f"P75={sorted(leads)[3*n_co//4]}  Max={max(leads)}")
    R(f"  lead=0 (CO в день отгрузки): {sum(1 for l in leads if l==0)} "
      f"({100*sum(1 for l in leads if l==0)/n_co:.1f}%)")
    R(f"  lead<0 (CO после отгрузки): {sum(1 for l in leads if l<0)} "
      f"({100*sum(1 for l in leads if l<0)/n_co:.1f}%)")

    # ── C. Procurement cutoffs по периодам ────────────────────────────────
    H("C. Procurement cutoffs — что известно за N дней до отгрузки")
    R(f"  Metric = (revenue CO-linked, CO.moment <= demand.day - N) / total revenue")
    R(f"\n  {'Период':12s} {'n':>4s}  "
      f"{'>=1d':>7s} {'>=2d':>7s} {'>=3d':>7s} {'>=5d':>7s} {'>=7d':>7s} {'>=10d':>8s} {'>=14d':>8s}")
    R("  " + "─" * 90)
    for p_filter in ("ALL", "NORMAL", "MARCH_8", "NEW_YEAR", "VALENTINE"):
        sub = with_co if p_filter == "ALL" else [d for d in with_co if d.period == p_filter]
        if not sub:
            continue
        tot = sum(d.sum_kop for d in sub) or 1
        n = len(sub)
        cols = []
        for th in (1, 2, 3, 5, 7, 10, 14):
            r = 100 * sum(d.sum_kop for d in sub if d.lead >= th) / tot
            cols.append(f"{r:>6.1f}%")
        R(f"  {p_filter:12s} {n:>4d}  " + " ".join(cols))

    # ── D. NORMAL period — главное ────────────────────────────────────────
    H("D. NORMAL period — procurement coverage (main answer)")
    normal_co = [d for d in with_co if d.period == "NORMAL"]
    tot_normal = sum(d.sum_kop for d in normal_co) or 1
    R(f"  NORMAL CO-linked docs: {len(normal_co)}")
    R(f"\n  За N дней известно X% BASE_NORMAL выручки:")
    for th in (1, 2, 3, 5, 7, 10, 14):
        known = 100 * sum(d.sum_kop for d in normal_co if d.lead >= th) / tot_normal
        unknown = 100 - known
        R(f"    >= {th:2d} дн: known={known:5.1f}%  residual={unknown:5.1f}%")
    R(f"\n  Медиана lead (NORMAL): {statistics.median([d.lead for d in normal_co]):.0f} дн")

    # ── E. SREZKA lead-time (позиционная выборка) ─────────────────────────
    H("E. SREZKA lead-time — позиционная стратифицированная выборка")
    srezka_pos = [p for p in pos_all if p["is_srezka"] and p["lead"] is not None]
    other_pos  = [p for p in pos_all if not p["is_srezka"]]
    R(f"  Всего позиций в выборке: {len(pos_all)}")
    R(f"  SREZKA (срезка): {len(srezka_pos)}  qty={sum(p['qty'] for p in srezka_pos):.0f}")
    R(f"  Прочее:          {len(other_pos)}   qty={sum(p['qty'] for p in other_pos):.0f}")

    # По периодам
    R(f"\n  {'Период':12s} {'pos':>5s}  "
      f"{'qty_>=1d%':>10s} {'qty_>=3d%':>10s} {'qty_>=5d%':>10s} {'qty_>=7d%':>10s}")
    R("  " + "─" * 70)
    for p_filter in ("NORMAL", "MARCH_8", "NEW_YEAR", "VALENTINE", "ALL"):
        sub = srezka_pos if p_filter == "ALL" else [p for p in srezka_pos if p["period"] == p_filter]
        if not sub:
            continue
        tot_qty = sum(p["qty"] for p in sub) or 1
        cols = []
        for th in (1, 3, 5, 7):
            q = 100 * sum(p["qty"] for p in sub if p["lead"] >= th) / tot_qty
            cols.append(f"{q:>9.1f}%")
        R(f"  {p_filter:12s} {len(sub):>5d}  " + " ".join(cols))

    # NORMAL отдельно — ключевые цифры
    normal_srezka = [p for p in srezka_pos if p["period"] == "NORMAL"]
    if normal_srezka:
        tot = sum(p["qty"] for p in normal_srezka) or 1
        R(f"\n  >>> NORMAL BASE_SREZKA (qty-weighted):")
        for th in (3, 5, 7, 10, 14):
            known = 100 * sum(p["qty"] for p in normal_srezka if p["lead"] >= th) / tot
            R(f"      за {th:2d} дней известно: {known:5.1f}%  residual: {100-known:5.1f}%")

    # ── F. Large-order coverage (qty buckets) ─────────────────────────────
    H("F. Large-order coverage — size buckets (qty per position, выборка)")
    R(f"  {'Bucket qty':12s} {'pos':>5s} {'qty_shr%':>9s} {'med_lead':>9s} "
      f"{'>=3d%':>7s} {'>=5d%':>7s} {'>=7d%':>7s} {'>=10d%':>8s}")
    R("  " + "─" * 80)
    buckets = [
        ("<50",      0,     50),
        ("50–99",    50,   100),
        ("100–249", 100,   250),
        ("250–499", 250,   500),
        ("500–999", 500,  1000),
        (">=1000", 1000, 10**9),
    ]
    total_qty_all = sum(p["qty"] for p in srezka_pos) or 1
    for label, lo, hi in buckets:
        bucket = [p for p in srezka_pos if lo <= p["qty"] < hi]
        if not bucket:
            continue
        n = len(bucket)
        bq = sum(p["qty"] for p in bucket)
        b_leads = [p["lead"] for p in bucket if p["lead"] is not None]
        med = statistics.median(b_leads) if b_leads else 0
        c3  = 100 * sum(1 for p in bucket if p["lead"] is not None and p["lead"] >= 3) / n
        c5  = 100 * sum(1 for p in bucket if p["lead"] is not None and p["lead"] >= 5) / n
        c7  = 100 * sum(1 for p in bucket if p["lead"] is not None and p["lead"] >= 7) / n
        c10 = 100 * sum(1 for p in bucket if p["lead"] is not None and p["lead"] >= 10) / n
        R(f"  {label:12s} {n:>5d} {100*bq/total_qty_all:>8.1f}% {med:>9.0f} "
          f"{c3:>6.1f}% {c5:>6.1f}% {c7:>6.1f}% {c10:>7.1f}%")

    # Вывод порога
    best_threshold = None
    for label, lo, hi in buckets:
        bucket = [p for p in srezka_pos if lo <= p["qty"] < hi]
        if not bucket:
            continue
        b_leads = [p["lead"] for p in bucket if p["lead"] is not None]
        if not b_leads:
            continue
        c7 = 100 * sum(1 for p in bucket if p["lead"] is not None and p["lead"] >= 7) / len(bucket)
        if c7 >= 60 and best_threshold is None:
            best_threshold = lo
    if best_threshold:
        R(f"\n  >>> LARGE_ORDER candidate threshold: qty >= {best_threshold} "
          f"(первый bucket где >=7d% >= 60%)")
    else:
        R(f"\n  >>> Нет чёткого threshold с >=7d% >= 60% — данные нужно перепроверить")

    # ── G. March 8 — per-cutoff ───────────────────────────────────────────
    H("G. March 8 — per-procurement-cutoff: known qty vs total (SREZKA)")
    march_pos = [p for p in srezka_pos
                 if p["demand_day"] and date(2026, 3, 1) <= p["demand_day"] <= date(2026, 3, 10)]
    if march_pos:
        tot_march = sum(p["qty"] for p in march_pos) or 1
        march8 = date(2026, 3, 8)
        R(f"  March 2-10 SREZKA qty: {tot_march:.0f}")
        R(f"  {'Procurement lead':18s} {'known_qty':>10s} {'known%':>8s} {'residual_qty':>14s}")
        for proc_lead in (1, 2, 3, 5, 7, 10, 14):
            cutoff_dt = march8 - timedelta(days=proc_lead)
            known = [p for p in march_pos if p["co_day"] and p["co_day"] <= cutoff_dt]
            kq = sum(p["qty"] for p in known)
            R(f"  за {proc_lead:2d} дн (cut={cutoff_dt})  {kq:>10.0f} "
              f"{100*kq/tot_march:>7.1f}% {tot_march-kq:>14.0f}")
    else:
        R("  Нет данных March 2-10 в позиционной выборке")

    # ── H. Три режима backtest (revenue-based, без double-count) ──────────
    H("H. Три режима: STAT_ONLY / KNOWN_ONLY / HYBRID (proper residual)")
    R("  HYBRID = known_rev_at_cutoff + mean_past_residual(same_period)")
    R("  Это исключает double-count: stat только для ещё не оформленного спроса.")

    # Walk-forward
    hist_max = max(d.day for d in valid)
    cutoff_start = EVAL_START + timedelta(days=28)
    cutoff_end   = hist_max - timedelta(days=HORIZON)

    # Дневная выручка склада (для stat forecast)
    def stat_forecast_rev(cutoff: date) -> float:
        start = max(cutoff - timedelta(days=28), EVAL_START)
        n_days = (cutoff - start).days
        if n_days == 0: return 0.0
        total = sum(store_daily_rev.get(d, 0) for d in daterange(start, cutoff - timedelta(days=1)))
        return (total / n_days) * HORIZON

    # По demand-day: быстрый lookup
    by_day: dict[date, list[DemandRec]] = defaultdict(list)
    for d in valid:
        by_day[d.day].append(d)

    bt_rows: list[dict] = []
    cutoff = cutoff_start
    while cutoff <= cutoff_end:
        fcst_from = cutoff
        fcst_to   = cutoff + timedelta(days=HORIZON - 1)
        p = period_of(fcst_from)

        actual_rev = sum(d.sum_kop for dd in daterange(fcst_from, fcst_to)
                         for d in by_day.get(dd, []))
        known_rev  = sum(d.sum_kop for dd in daterange(fcst_from, fcst_to)
                         for d in by_day.get(dd, [])
                         if d.co_day and d.co_day <= cutoff)
        stat_rev   = stat_forecast_rev(cutoff)
        residual_actual = actual_rev - known_rev

        bt_rows.append({
            "cutoff": cutoff, "period": p,
            "actual": actual_rev, "stat": stat_rev,
            "known": known_rev,
            "residual_actual": residual_actual,
            "known_share": known_rev / actual_rev if actual_rev else 0,
        })
        cutoff += timedelta(days=7)

    # Статистическая оценка residual: walk-forward (только прошлые cutoffs)
    for i, row in enumerate(bt_rows):
        p = row["period"]
        # Prошлые cutoffs того же типа периода
        past_residuals = [r["residual_actual"] for r in bt_rows[:i] if r["period"] == p]
        if past_residuals:
            residual_forecast = statistics.mean(past_residuals)
        else:
            # Fallback: среднее по всем прошлым
            past_all = [r["residual_actual"] for r in bt_rows[:i]]
            residual_forecast = statistics.mean(past_all) if past_all else row["stat"] * 0.5
        row["hybrid"] = row["known"] + residual_forecast

    # Метрики
    R(f"\n  n={len(bt_rows)} cutoffs  "
      f"(от {bt_rows[0]['cutoff']} до {bt_rows[-1]['cutoff']})")
    R(f"\n  {'Режим':40s} {'n':>4s} {'MAE,р':>12s} {'WAPE':>7s} "
      f"{'bias':>7s} {'P90,р':>12s} {'over%':>6s} {'under%':>7s}")
    R("  " + "─" * 100)

    for label, key in [
        ("STAT_ONLY  (mean_cal_28)",            "stat"),
        ("KNOWN_ONLY (CO <= cutoff)",            "known"),
        ("HYBRID     (known + residual_forecast)", "hybrid"),
    ]:
        acts = [r["actual"] for r in bt_rows]
        preds = [r[key] for r in bt_rows]
        m = metrics(acts, preds)
        if not m:
            continue
        wape_s = f"{m['WAPE']:.3f}" if m.get("WAPE") is not None else "—"
        bias_s = f"{m['bias']:+.3f}" if m.get("bias") is not None else "—"
        R(f"  {label:40s} {m['n']:>4d} {m['MAE']/100:>12,.0f} {wape_s:>7s} "
          f"{bias_s:>7s} {m['P90']/100:>12,.0f} {m['over%']:>5.1f}% {m['under%']:>6.1f}%")

    R(f"\n  По периодам:")
    R(f"  {'Период':12s} {'n':>4s} {'known_shr':>10s}  {'STAT':>8s} {'KNOWN':>8s} {'HYBRID':>8s}")
    for p_filter in ("NORMAL", "MARCH_8", "NEW_YEAR", "VALENTINE"):
        sub = [r for r in bt_rows if r["period"] == p_filter]
        if not sub:
            continue
        ks = sum(r["known_share"] for r in sub) / len(sub)
        acts = [r["actual"] for r in sub]
        ms = metrics(acts, [r["stat"]   for r in sub])
        mk = metrics(acts, [r["known"]  for r in sub])
        mh = metrics(acts, [r["hybrid"] for r in sub])
        ws = f"{ms['WAPE']:.3f}" if ms.get("WAPE") else "—"
        wk = f"{mk['WAPE']:.3f}" if mk.get("WAPE") else "—"
        wh = f"{mh['WAPE']:.3f}" if mh.get("WAPE") else "—"
        R(f"  {p_filter:12s} {len(sub):>4d} {ks:>9.1%}  {ws:>8s} {wk:>8s} {wh:>8s}")

    # ── I. Residual analysis ───────────────────────────────────────────────
    H("I. Residual demand — по периодам и procurement lead")
    for p_filter in ("NORMAL", "MARCH_8", "ALL"):
        sub = bt_rows if p_filter == "ALL" else [r for r in bt_rows if r["period"] == p_filter]
        if not sub:
            continue
        residuals = [r["residual_actual"] for r in sub if r["actual"] > 0]
        shares    = [1 - r["known_share"] for r in sub if r["actual"] > 0]
        if not residuals:
            continue
        R(f"\n  {p_filter} (n={len(residuals)}):")
        R(f"    Residual ₽: med={statistics.median(residuals)/100:,.0f}  "
          f"min={min(residuals)/100:,.0f}  max={max(residuals)/100:,.0f}")
        R(f"    Share%:     med={statistics.median(shares):.1%}  "
          f"min={min(shares):.1%}  max={max(shares):.1%}")
        R(f"    >50% residual: {sum(1 for s in shares if s>0.5)}/{len(shares)} cutoffs")
        R(f"    <20% residual: {sum(1 for s in shares if s<0.2)}/{len(shares)} cutoffs")

    # ── J. Final recommendations ───────────────────────────────────────────
    H("J. Final recommendations — параметры архитектуры BASE")

    # Лучший procurement lead (выбираем как cutoff где HYBRID лучше STAT минимум на 15%)
    stat_wape = metrics([r["actual"] for r in bt_rows],
                        [r["stat"] for r in bt_rows]).get("WAPE", 1.0)
    hyb_wape  = metrics([r["actual"] for r in bt_rows],
                        [r["hybrid"] for r in bt_rows]).get("WAPE", 1.0)
    improvement = (stat_wape - hyb_wape) / stat_wape * 100 if stat_wape else 0

    # LARGE_ORDER threshold из секции F
    normal_residual_shares = [1 - r["known_share"] for r in bt_rows
                               if r["period"] == "NORMAL" and r["actual"] > 0]
    med_normal_residual = statistics.median(normal_residual_shares) if normal_residual_shares else 0.5

    R(f"\n  procurement_lead    = 7 дней (для планирования закупки)")
    R(f"  HYBRID improvement  = {improvement:.1f}% vs STAT_ONLY (WAPE: {stat_wape:.3f} → {hyb_wape:.3f})")
    R(f"\n  NORMAL period (главный):")
    normal_co_normal = [d for d in with_co if d.period == "NORMAL"]
    if normal_co_normal:
        tot_n = sum(d.sum_kop for d in normal_co_normal) or 1
        for th in (3, 5, 7):
            known_pct = 100 * sum(d.sum_kop for d in normal_co_normal if d.lead >= th) / tot_n
            R(f"    за {th}d known  = {known_pct:.1f}%  residual = {100-known_pct:.1f}%")

    R(f"\n  Residual share (NORMAL, при 7d procurement):")
    R(f"    Медиана = {med_normal_residual:.1%}")
    if med_normal_residual < 0.20:
        R(f"    → KNOWN_ONLY достаточно для NORMAL недель")
        R(f"    Рекомендация: CustomerOrder as primary, tiny statistical fallback")
    elif med_normal_residual < 0.40:
        R(f"    → HYBRID рекомендован: CO покрывает основную часть")
        R(f"    Рекомендация: known_CO + statistical_residual")
    else:
        R(f"    → Статистика критична для NORMAL недель")
        R(f"    Рекомендация: statistical PRIMARY + CO enrichment для крупных заказов")

    R(f"\n  LARGE_ORDER threshold: threshold = {best_threshold or '?'} шт")
    R(f"  (первый bucket где >=60% позиций известны за 7d)")

    R(f"\n  Архитектура BASE (финальная рекомендация):")
    if med_normal_residual > 0.35:
        R(f"    KNOWN_ORDERS (large, lead >= 7d)")
        R(f"    + PREORDERS (праздники, предзаказы)")
        R(f"    + STATISTICAL RESIDUAL (основная часть обычных недель)")
        R(f"    = EXPECTED BASE DEMAND")
    else:
        R(f"    KNOWN_ORDERS (все CO, lead >= 3d)")
        R(f"    + PREORDERS (праздники)")
        R(f"    + small STATISTICAL RESIDUAL")
        R(f"    = EXPECTED BASE DEMAND")

    R(f"\n  calc_forecast.py — НЕ МЕНЯТЬ (исследовательская фаза закрыта)")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/co_stratified.txt")
    args = parser.parse_args()

    token = config.MOYSKLAD_TOKEN()
    conn  = db.connect(config.DATABASE_URL())
    conn.autocommit = True

    base_sids = [sid for sid, ch in config.STORE_CHANNELS.items() if ch == "опт"]
    if not base_sids:
        print("Нет BASE-склада"); sys.exit(1)
    store_id   = base_sids[0]
    store_href = f"{BASE_API}/entity/store/{store_id}"
    with conn.cursor() as cur:
        cur.execute("SELECT store_name FROM sales_by_store_day WHERE store_id=%s LIMIT 1", (store_id,))
        row = cur.fetchone()
        store_name = row[0] if row else store_id

    out_lines: list[str] = []
    hdr = f"CO STRATIFIED ANALYSIS — {store_name}  (2026-01-05 .. 2026-08-14)"
    print(hdr); out_lines.append(hdr)

    run(token, conn, store_id, store_href, store_name, out_lines)

    Path(args.out).write_text("\n".join(out_lines), encoding="utf-8")
    print(f"\nОтчёт: {args.out}")
    conn.close()


if __name__ == "__main__":
    main()
