#!/usr/bin/env python3
"""
CO lead-time backtest: CustomerOrder.moment → Demand.moment

Цель: определить реальное распределение lead time (CO создан → отгрузка)
по сегментам и найти оптимальную политику привязки CO к горизонту.

Выдаёт:
  - Percentiles lead time (P25/P50/P75/P90) по сегментам
  - Precision/Recall для каждого offset [1,2,3,5,7,14] дней
  - false_positive_qty / false_negative_qty (в штуках, не только в CO)
  - Рекомендацию: какой offset минимизирует qty-ошибку для каждого сегмента
  - Policy simulation: LARGE Tier B (age<=7, remaining>0) qty-weighted backtest

Флаги:
  --policy-sim   дополнительно симулировать политику LARGE Tier B из forecast_orders.py

Read-only. Не трогает production. Может занять 5–15 минут (пагинация API).

Сегменты:
  NORMAL     qty < LARGE_ORDER_THRESHOLD  и не предзаказ
  LARGE      qty >= LARGE_ORDER_THRESHOLD
  PREORDER   CO-имя содержит ключевые слова
"""
from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))
import hermes.config as config

BASE_API = "https://api.moysklad.ru/api/remap/1.2"
BASE_STORE_IDS = [sid for sid, ch in config.STORE_CHANNELS.items() if ch == "опт"]
LARGE_ORDER_THRESHOLD = getattr(config, "BASE_LARGE_ORDER_THRESHOLD", 500)
_PREORDER_KEYWORDS = ("предоплат", "предзаказ", "march_8", "8 марта", "14 февр")

# Горизонт анализа (дней назад)
LOOKBACK_CO_DAYS = 90       # создание CO
LOOKBACK_DEMAND_DAYS = 120  # отгрузки могут быть позже создания CO
OFFSETS_TO_TEST = [1, 2, 3, 5, 7, 10, 14]
HORIZON_DAYS = 7            # типичный горизонт прогноза


class LeadRecord(NamedTuple):
    co_id:      str
    co_date:    date
    demand_id:  str
    demand_date: date
    qty:        float
    segment:    str        # NORMAL / LARGE / PREORDER


def _is_preorder(co_name: str) -> bool:
    n = co_name.lower()
    return any(k in n for k in _PREORDER_KEYWORDS)


def _segment(qty: float, is_pre: bool) -> str:
    if is_pre:
        return "PREORDER"
    if qty >= LARGE_ORDER_THRESHOLD:
        return "LARGE"
    return "NORMAL"


def _get(token: str, path: str, params: dict | None = None) -> dict:
    url = BASE_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("User-Agent", "hermes-co-backtest/1.0")
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(3 * attempt)
                continue
            return {}
        except Exception:
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            return {}
    return {}


def _paginate(token: str, path: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    offset, total = 0, None
    while True:
        r = _get(token, path, {**params, "limit": 100, "offset": offset})
        batch = r.get("rows", [])
        rows.extend(batch)
        if total is None:
            total = r.get("meta", {}).get("size", 0)
        offset += len(batch)
        if not batch or offset >= (total or 0):
            break
        time.sleep(0.2)
    return rows


def _percentile(values: list[float], p: int) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


def collect_leads(token: str, store_id: str) -> list[LeadRecord]:
    """
    Собирает пары (CO, Demand) для BASE-склада.

    Алгоритм:
      1. Загружаем Demand (отгрузки) за последние LOOKBACK_DEMAND_DAYS дней.
         Demand содержит поле customerOrder с href → id создавшего CO.
      2. Загружаем CO за последние LOOKBACK_CO_DAYS дней.
      3. Джойним Demand → CO по co_id.
      4. Для каждой пары считаем lead = demand.moment - co.moment.
    """
    store_href = f"{BASE_API}/entity/store/{store_id}"
    today = date.today()

    # ── Шаг 1: Demands ─────────────────────────────────────────────────────
    demand_from = today - timedelta(days=LOOKBACK_DEMAND_DAYS)
    print(f"  Загружаем Demand за {LOOKBACK_DEMAND_DAYS} дней...", flush=True)
    demands = _paginate(token, "/entity/demand", {
        "filter": (
            f"store={store_href}"
            f";moment>={demand_from.strftime('%Y-%m-%d 00:00:00')}"
            f";moment<={today.strftime('%Y-%m-%d 23:59:59')}"
        ),
        "order": "moment,asc",
        "expand": "positions.assortment,customerOrder",
    })
    print(f"  Demand загружено: {len(demands)}")

    # ── Шаг 2: CO за LOOKBACK_CO_DAYS ──────────────────────────────────────
    co_from = today - timedelta(days=LOOKBACK_CO_DAYS)
    print(f"  Загружаем CustomerOrder за {LOOKBACK_CO_DAYS} дней...", flush=True)
    cos = _paginate(token, "/entity/customerorder", {
        "filter": (
            f"store={store_href}"
            f";moment>={co_from.strftime('%Y-%m-%d 00:00:00')}"
            f";moment<={today.strftime('%Y-%m-%d 23:59:59')}"
        ),
        "order": "moment,asc",
    })
    print(f"  CustomerOrder загружено: {len(cos)}")

    # ── Шаг 3: Индекс CO → meta ──────────────────────────────────────────
    co_meta: dict[str, dict] = {}
    for co in cos:
        cid = co.get("id", "")
        if not cid:
            continue
        co_meta[cid] = {
            "moment": co.get("moment", "")[:10],
            "name":   co.get("name", ""),
        }

    # ── Шаг 4: Джойн Demand → CO ───────────────────────────────────────────
    records: list[LeadRecord] = []
    for demand in demands:
        d_moment_str = demand.get("moment", "")
        if not d_moment_str:
            continue
        d_date = date.fromisoformat(d_moment_str[:10])

        # CO из demand
        co_ref = demand.get("customerOrder") or {}
        co_meta_ref = co_ref.get("meta") or {}
        co_href = co_meta_ref.get("href", "")
        if not co_href:
            continue  # нет связки с CO

        # Извлекаем co_id из href
        co_id = co_href.rsplit("/", 1)[-1]
        co_info = co_meta.get(co_id)
        if not co_info or not co_info["moment"]:
            continue  # CO вне нашего окна

        co_date = date.fromisoformat(co_info["moment"])
        co_name = co_info["name"]
        is_pre = _is_preorder(co_name)

        # Суммарный qty по позициям demand
        positions = demand.get("positions", {}).get("rows", [])
        total_qty = sum(float(p.get("quantity", 0)) for p in positions)
        if total_qty <= 0:
            continue

        seg = _segment(total_qty, is_pre)
        lead = (d_date - co_date).days

        # Отрицательный lead = demand раньше CO (аномалия, включаем)
        records.append(LeadRecord(
            co_id=co_id,
            co_date=co_date,
            demand_id=demand.get("id", ""),
            demand_date=d_date,
            qty=total_qty,
            segment=seg,
        ))

    return records


def analyze(records: list[LeadRecord]) -> None:
    by_seg: dict[str, list[LeadRecord]] = defaultdict(list)
    for r in records:
        by_seg[r.segment].append(r)

    print(f"\n{'═'*70}")
    print(f"Всего записей (CO, Demand): {len(records)}")

    for seg in ("NORMAL", "LARGE", "PREORDER"):
        recs = by_seg.get(seg, [])
        print(f"\n{'─'*70}")
        print(f"Сегмент: {seg}  ({len(recs)} отгрузок)")
        if not recs:
            print("  Нет данных.")
            continue

        leads = [(r.demand_date - r.co_date).days for r in recs]
        qtys  = [r.qty for r in recs]

        print(f"  Lead time (дней от CO до Demand):")
        print(f"    min={min(leads):5.1f}  P25={_percentile(leads,25):5.1f}  "
              f"P50={_percentile(leads,50):5.1f}  P75={_percentile(leads,75):5.1f}  "
              f"P90={_percentile(leads,90):5.1f}  max={max(leads):5.1f}")
        print(f"  Отрицательный lead (аномалии): {sum(1 for l in leads if l < 0)}")
        print(f"  Lead 0 дней (CO=Demand): {sum(1 for l in leads if l == 0)}")
        print(f"  Суммарный qty: {sum(qtys):.0f} шт.")

        # Распределение по days
        bins: dict[str, int] = {}
        for l in leads:
            if l < 0:   b = "отриц"
            elif l == 0: b = "0"
            elif l <= 1: b = "1"
            elif l <= 3: b = "2–3"
            elif l <= 7: b = "4–7"
            elif l <= 14: b = "8–14"
            else:         b = "15+"
            bins[b] = bins.get(b, 0) + 1
        print(f"  Распределение lead по бакетам:")
        for bucket in ("отриц", "0", "1", "2–3", "4–7", "8–14", "15+"):
            cnt = bins.get(bucket, 0)
            bar = "█" * min(cnt, 40)
            print(f"    {bucket:6}: {cnt:4d}  {bar}")

        # ── Backtest: для каждого offset ─────────────────────────────────
        print(f"\n  Backtest horizon={HORIZON_DAYS}d, policy=CO.moment+offset:")
        print(f"  {'offset':>7} {'TP':>6} {'FP':>6} {'FN':>6} "
              f"{'TP_qty':>10} {'FP_qty':>10} {'FN_qty':>10} {'prec':>7} {'recall':>7}")
        print(f"  {'-'*85}")

        # «Истина»: CO чья отгрузка попала в [horizon_from, horizon_from+HORIZON_DAYS)
        # Симулируем: cutoff = CO.moment (дата создания заказа)
        # Мы «предсказываем» включить CO в horizon = [cutoff+1, cutoff+HORIZON_DAYS]
        # если policy говорит estimated_date = co_date + offset
        # и estimated_date попадает в [cutoff+1, cutoff+HORIZON_DAYS]

        # Для простоты: used_as_true = demand_date - co_date <= HORIZON_DAYS
        # (т.е. реально отгружено в горизонте)

        for offset in OFFSETS_TO_TEST:
            tp = fp = fn = 0
            tp_qty = fp_qty = fn_qty = 0.0

            for r in recs:
                lead = (r.demand_date - r.co_date).days
                actually_in = 1 <= lead <= HORIZON_DAYS      # реально в горизонте
                predicted_in = 1 <= offset <= HORIZON_DAYS   # предсказан ли

                if actually_in and predicted_in:
                    tp += 1; tp_qty += r.qty
                elif not actually_in and predicted_in:
                    fp += 1; fp_qty += r.qty
                elif actually_in and not predicted_in:
                    fn += 1; fn_qty += r.qty

            prec   = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
            recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
            prec_s   = f"{prec:.2f}" if prec == prec else "  —  "
            recall_s = f"{recall:.2f}" if recall == recall else "  —  "

            print(f"  +{offset:6d}  {tp:6d}  {fp:6d}  {fn:6d}  "
                  f"{tp_qty:10.0f}  {fp_qty:10.0f}  {fn_qty:10.0f}  "
                  f"{prec_s:>7}  {recall_s:>7}")

    # ── Итоговые рекомендации ──────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("Интерпретация:")
    print("  FP_qty = штуки ошибочно включённых CO → завышение закупки")
    print("  FN_qty = штуки пропущенных CO → занижение закупки")
    print("  Если P50(lead) >> offset → много FN (занижение)")
    print("  Если P50(lead) << offset → много FP (завышение)")
    print("  Для NORMAL: предпочтительно смещение к FN (не завышать)")
    print("  Для LARGE:  критично → отдельная политика после анализа")
    print()


MAX_CO_AGE_DAYS_TIER_B = 7   # из forecast_orders.py: MAX_CO_AGE_FOR_ESTIMATED


def simulate_tier_b_policy(records: list[LeadRecord]) -> None:
    """
    Симулирует политику LARGE Tier B из forecast_orders.py:
      - Сегмент LARGE (qty >= 500)
      - age_at_cutoff <= 7 дней  (в backtest-данных: co_date → demand_date как прокси)
      - Включаем как estimated — проверяем, попало ли demand в [1, HORIZON_DAYS]

    В реальном движке age = cutoff - co_date.
    В backtest: cutoff = co_date (мы смотрим из точки создания CO).
    age_at_cutoff = 0 всегда (CO только что создан) — это и есть самый жёсткий тест.

    Для исторической симуляции:
      Cutoffs = [co_date + n for n in range(0, MAX_CO_AGE+1)]
      «CO виден» на cutoff если (cutoff - co_date) <= MAX_CO_AGE_DAYS_TIER_B
      и demand_date - cutoff in [1, HORIZON_DAYS]

    Это более реалистично: CO возрастом 0..7 дней включается в прогноз.
    """
    large_recs = [r for r in records if r.segment == "LARGE"]
    if not large_recs:
        print("\n  Нет LARGE записей для policy simulation.")
        return

    print(f"\n{'═'*70}")
    print(f"POLICY SIMULATION: LARGE Tier B (age <= {MAX_CO_AGE_DAYS_TIER_B}d, horizon={HORIZON_DAYS}d)")
    print(f"  Логика: для каждого CO в день создания и age=0..{MAX_CO_AGE_DAYS_TIER_B}")
    print(f"  CO включается если (cutoff - co_date) <= {MAX_CO_AGE_DAYS_TIER_B}")
    print(f"  «Правильно» если demand_date - cutoff in [1, {HORIZON_DAYS}]")
    print(f"  Всего LARGE CO-Demand пар: {len(large_recs)}")
    print()

    # Для каждого co_id: множество дней (lead) до отгрузки
    # Симулируем включение CO на cutoff=co_date, co_date+1, ..., co_date+7
    # Для каждого cutoff: demand включён если demand_date in [cutoff+1, cutoff+HORIZON_DAYS]

    # Уникальные CO (объединяем позиции)
    co_info: dict[str, dict] = {}
    for r in large_recs:
        if r.co_id not in co_info:
            co_info[r.co_id] = {"co_date": r.co_date, "demands": []}
        co_info[r.co_id]["demands"].append((r.demand_date, r.qty))

    print(f"  Уникальных LARGE CO: {len(co_info)}")
    print()

    # Симуляция по age (дней от создания CO до cutoff)
    print(f"  {'age':>5} {'incl_co':>8} {'tp_co':>8} {'fp_co':>8} "
          f"{'tp_qty':>10} {'fp_qty':>10} {'fn_qty':>10} {'prec':>7} {'recall':>7} {'bias':>8}")
    print(f"  {'-'*90}")

    all_large_demand_qty = sum(r.qty for r in large_recs)
    actual_in_horizon_qty = sum(
        r.qty for r in large_recs
        if 1 <= (r.demand_date - r.co_date).days <= HORIZON_DAYS
    )

    for age in range(0, MAX_CO_AGE_DAYS_TIER_B + 1):
        tp_co = fp_co = 0
        tp_qty = fp_qty = fn_qty = 0.0
        incl_co = 0

        for co_id, info in co_info.items():
            co_date = info["co_date"]
            demands = info["demands"]

            cutoff = co_date + timedelta(days=age)
            h_from = cutoff + timedelta(days=1)
            h_to   = cutoff + timedelta(days=HORIZON_DAYS)

            # CO попадает под Tier B: age = (cutoff - co_date) <= MAX_CO_AGE_DAYS_TIER_B
            if age > MAX_CO_AGE_DAYS_TIER_B:
                continue
            incl_co += 1

            co_qty = sum(q for _, q in demands)
            actual_in_h = sum(q for d, q in demands if h_from <= d <= h_to)
            not_in_h = co_qty - actual_in_h

            if actual_in_h > 0:
                tp_co  += 1
                tp_qty += actual_in_h
            if not_in_h > 0:
                fp_co += 1
                fp_qty += not_in_h

        fn_qty = actual_in_horizon_qty - tp_qty  # что не поймали

        prec   = tp_qty / (tp_qty + fp_qty) if (tp_qty + fp_qty) > 0 else float("nan")
        recall = tp_qty / actual_in_horizon_qty if actual_in_horizon_qty > 0 else float("nan")
        bias   = tp_qty + fp_qty - actual_in_horizon_qty  # >0 = завышение, <0 = занижение
        prec_s   = f"{prec:.2f}" if prec == prec else "  —  "
        recall_s = f"{recall:.2f}" if recall == recall else "  —  "
        bias_s   = f"{bias:+.0f}"

        print(f"  {age:>5}  {incl_co:>8}  {tp_co:>8}  {fp_co:>8}  "
              f"{tp_qty:>10.0f}  {fp_qty:>10.0f}  {fn_qty:>10.0f}  "
              f"{prec_s:>7}  {recall_s:>7}  {bias_s:>8}")

    print()
    print(f"  Всего LARGE demand qty в горизонте 1-{HORIZON_DAYS}d: {actual_in_horizon_qty:.0f}")
    print(f"  Всего LARGE demand qty (все периоды): {all_large_demand_qty:.0f}")
    print()
    print(f"  Интерпретация policy LARGE Tier B (age=0..{MAX_CO_AGE_DAYS_TIER_B}):")
    print(f"    tp_qty = штуки LARGE CO, которые реально отгружены в горизонте (правильно включены)")
    print(f"    fp_qty = штуки LARGE CO, включённых estimated, но отгружены ВНЕ горизонта (завышение)")
    print(f"    fn_qty = штуки LARGE demand в горизонте, НЕ включённые в estimated (занижение)")
    print(f"    bias > 0 = систематическое завышение прогноза")
    print(f"    bias < 0 = систематическое занижение")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-sim", action="store_true",
                        help="Дополнительно симулировать политику LARGE Tier B")
    args = parser.parse_args()

    token = config.MOYSKLAD_TOKEN()
    all_records: list[LeadRecord] = []

    for store_id in BASE_STORE_IDS:
        print(f"\n{'═'*70}")
        print(f"Склад: {store_id}")
        recs = collect_leads(token, store_id)
        all_records.extend(recs)
        print(f"  Собрано LeadRecord: {len(recs)}")

    if not all_records:
        print("\nНет данных для анализа. Возможные причины:")
        print("  - Нет Demand с полем customerOrder за период")
        print("  - CO не создавались для этого склада")
        return

    analyze(all_records)

    if args.policy_sim:
        simulate_tier_b_policy(all_records)
    else:
        print(f"\n  Подсказка: запустите с --policy-sim для qty-weighted backtest Tier B LARGE")

    # Сохраняем сырые данные для дальнейшего анализа
    out = Path("/tmp/co_lead_backtest.json")
    out.write_text(json.dumps([
        {
            "co_id": r.co_id,
            "co_date": str(r.co_date),
            "demand_id": r.demand_id,
            "demand_date": str(r.demand_date),
            "lead_days": (r.demand_date - r.co_date).days,
            "qty": r.qty,
            "segment": r.segment,
        }
        for r in all_records
    ], ensure_ascii=False, indent=2))
    print(f"Сырые данные: {out}")


if __name__ == "__main__":
    main()
