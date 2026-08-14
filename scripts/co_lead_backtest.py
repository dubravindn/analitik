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
    co_id:          str
    co_date:        date
    demand_id:      str
    demand_date:    date
    qty:            float   # qty из Demand (одна отгрузка)
    co_ordered_qty: float   # total ordered в CO (из позиций CO)
    segment:        str     # NORMAL / LARGE / LARGE_PLUS / PREORDER


def _is_preorder(co_name: str) -> bool:
    n = co_name.lower()
    return any(k in n for k in _PREORDER_KEYWORDS)


def _segment(co_ordered_qty: float, is_pre: bool) -> str:
    """Сегментация по ordered_qty CO, не по qty отдельной отгрузки."""
    if is_pre:
        return "PREORDER"
    if co_ordered_qty >= 1000:
        return "LARGE_PLUS"   # крупнейшие — отдельно
    if co_ordered_qty >= LARGE_ORDER_THRESHOLD:
        return "LARGE"        # 500–999
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

    # ── Шаг 2: CO за LOOKBACK_CO_DAYS — с positions для ordered_qty ─────────
    co_from = today - timedelta(days=LOOKBACK_CO_DAYS)
    print(f"  Загружаем CustomerOrder за {LOOKBACK_CO_DAYS} дней (с positions)...", flush=True)
    cos = _paginate(token, "/entity/customerorder", {
        "filter": (
            f"store={store_href}"
            f";moment>={co_from.strftime('%Y-%m-%d 00:00:00')}"
            f";moment<={today.strftime('%Y-%m-%d 23:59:59')}"
        ),
        "order": "moment,asc",
        "expand": "positions",  # нужен ordered_qty из позиций CO
    })
    print(f"  CustomerOrder загружено: {len(cos)}")

    # ── Шаг 3: Индекс CO → meta (включая ordered_qty) ───────────────────
    co_meta: dict[str, dict] = {}
    for co in cos:
        cid = co.get("id", "")
        if not cid:
            continue
        positions = co.get("positions", {}).get("rows", [])
        ordered_qty = sum(float(p.get("quantity", 0)) for p in positions)
        co_meta[cid] = {
            "moment":      co.get("moment", "")[:10],
            "name":        co.get("name", ""),
            "ordered_qty": ordered_qty,  # total ordered — без future leakage
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
        co_ordered = co_info["ordered_qty"]
        is_pre = _is_preorder(co_name)

        # qty из Demand (одна отгрузка, может быть частичной)
        positions = demand.get("positions", {}).get("rows", [])
        total_qty = sum(float(p.get("quantity", 0)) for p in positions)
        if total_qty <= 0:
            continue

        # Сегментация по CO ordered_qty, а не по qty одной отгрузки
        seg = _segment(co_ordered, is_pre)

        records.append(LeadRecord(
            co_id=co_id,
            co_date=co_date,
            demand_id=demand.get("id", ""),
            demand_date=d_date,
            qty=total_qty,
            co_ordered_qty=co_ordered,
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


MAX_CO_AGE_DAYS_TIER_B = 7   # верхняя граница исследования; финал выбирается по backtest


def simulate_tier_b_policy(records: list[LeadRecord]) -> None:
    """
    CORRECTED backtest v2: FN считается относительно глобального ground truth.

    Методологическая ошибка v1: FN считался только среди CO, которые policy уже включила.
    Исключённые CO полностью исчезали из знаменателя → FN=0 при любом окне → recall=1.00 везде.

    v2-методология:
      cutoff_dates = все уникальные даты создания CO в датасете

      Для каждого cutoff:
        ground_truth_qty = SUM(actual_demand в [cutoff+1, cutoff+HORIZON])
                           по ВСЕМ LARGE CO с co_date <= cutoff

        Для каждой policy (age_min, age_max):
          Для каждого CO:
            predicted = remaining_at_cutoff  если included, иначе 0
            TP += min(predicted, actual_in_h)
            FP += max(0, predicted - actual_in_h)
            FN += max(0, actual_in_h - predicted)

      Исключённый CO с реальным спросом → FN += actual_in_h (predicted=0).
      Расширение окна должно давать нормальный trade-off: меньше FN, больше FP.

    HISTORICAL_REMAINING_APPROXIMATE:
      ordered_qty взят из CO positions на дату запроса (не на historical cutoff).
      Если позиции отменены/изменены с тех пор — remaining занижен → FP занижен → precision завышена.
      approximate_CO_count = все CO в датасете (консервативная оценка, точный флаг нужно добавить).

    Подсегменты: LARGE (500–999) и LARGE_PLUS (>=1000) — анализируются отдельно.
    LARGE_PLUS с n < 20 CO помечается EVIDENCE_PROMISING | SAMPLE_SMALL.
    """

    def _pct(v: float, d: float) -> float:
        return 100.0 * v / d if d > 0 else float("nan")

    def _quantile(lst: list[float], p: float) -> float:
        if not lst:
            return 0.0
        s = sorted(lst)
        return s[min(int(len(s) * p), len(s) - 1)]

    def _f(v: float) -> str:
        return f"{v:.2f}" if v == v else "  —  "

    def _run_segment(seg_label: str, seg_recs: list[LeadRecord]) -> None:
        if not seg_recs:
            print(f"\n  Нет {seg_label} записей.")
            return

        # CO-level структура
        co_data: dict[str, dict] = {}
        for r in seg_recs:
            if r.co_id not in co_data:
                co_data[r.co_id] = {
                    "co_date":     r.co_date,
                    "ordered_qty": r.co_ordered_qty,  # из CO positions
                    "shipments":   [],                 # (date, qty)
                }
            co_data[r.co_id]["shipments"].append((r.demand_date, r.qty))

        n_co = len(co_data)
        small_sample = n_co < 20

        # Набор cutoff-дат = все уникальные даты создания CO
        cutoff_dates = sorted(set(info["co_date"] for info in co_data.values()))

        print(f"\n{'═'*74}")
        print(f"POLICY SIM v2 — глобальный ground truth: {seg_label}")
        if small_sample:
            print(f"  ⚠  EVIDENCE_PROMISING | SAMPLE_SMALL (n={n_co} CO) — не выбирать финальное правило")
        print(f"  Уникальных CO: {n_co}  |  Cutoff-дат: {len(cutoff_dates)}  |  Horizon: {HORIZON_DAYS}d")
        print(f"  HISTORICAL_REMAINING_APPROXIMATE:")
        print(f"    approximate_CO_count  = {n_co} (все; точный флаг нужно добавить в LeadRecord)")
        print(f"    approximate_qty_share = неизвестна (зависит от доли отменённых позиций)")
        print(f"    Эффект: ordered_qty может быть занижен → FP занижен → precision завышена")
        print()
        print(f"  FN теперь включает CO, исключённые policy, у которых был реальный спрос в горизонте.")
        print(f"  Расширение окна → меньше FN (больше CO включено), больше FP (больше ложного объёма).")
        print()

        # Политики для тестирования
        variants = [
            (0, 1), (0, 2), (0, 3), (0, 5), (0, 7),
            (1, 3), (1, 5), (1, 7),
        ]

        hdr = (f"  {'policy':>8}  {'gt_qty':>8} {'pred':>8} {'TP':>8} {'FP':>8} {'FN':>8} "
               f"{'prec':>5} {'recall':>6} {'F1':>5} "
               f"{'fp/gt%':>7} {'fn/gt%':>7} {'fp_med':>7} {'fp_p90':>7} {'fn_med':>7}")
        print(hdr)
        print(f"  {'-'*(len(hdr) - 2)}")

        for age_min, age_max in variants:
            total_gt = total_pred = total_tp = total_fp = total_fn = 0.0
            per_fp: list[float] = []
            per_fn: list[float] = []
            n_active = 0

            for cutoff in cutoff_dates:
                h_from = cutoff + timedelta(days=1)
                h_to   = cutoff + timedelta(days=HORIZON_DAYS)

                c_gt = c_pred = c_tp = c_fp = c_fn = 0.0

                for co_id, info in co_data.items():
                    age = (cutoff - info["co_date"]).days
                    if age < 0:
                        continue  # CO не существует на этот cutoff

                    actual_in_h = sum(q for d, q in info["shipments"] if h_from <= d <= h_to)
                    c_gt += actual_in_h

                    shipped_before = sum(q for d, q in info["shipments"] if d <= cutoff)
                    remaining = max(0.0, info["ordered_qty"] - shipped_before)
                    included  = (age_min <= age <= age_max) and remaining > 0
                    predicted = remaining if included else 0.0

                    c_pred += predicted
                    c_tp   += min(predicted, actual_in_h)
                    c_fp   += max(0.0, predicted - actual_in_h)
                    c_fn   += max(0.0, actual_in_h - predicted)

                if c_gt > 0 or c_pred > 0:
                    total_gt   += c_gt
                    total_pred += c_pred
                    total_tp   += c_tp
                    total_fp   += c_fp
                    total_fn   += c_fn
                    per_fp.append(c_fp)
                    per_fn.append(c_fn)
                    n_active += 1

            prec   = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else float("nan")
            recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else float("nan")
            f1     = (2*prec*recall/(prec+recall)
                      if (prec == prec and recall == recall and prec + recall > 0)
                      else float("nan"))

            fp_gt_pct = _pct(total_fp, total_gt)
            fn_gt_pct = _pct(total_fn, total_gt)
            fp_med = _quantile(per_fp, 0.5)
            fp_p90 = _quantile(per_fp, 0.9)
            fn_med = _quantile(per_fn, 0.5)

            pol = f"[{age_min},{age_max}]"
            print(f"  {pol:>8}  {total_gt:>8.0f} {total_pred:>8.0f} {total_tp:>8.0f} "
                  f"{total_fp:>8.0f} {total_fn:>8.0f} {_f(prec):>5} {_f(recall):>6} {_f(f1):>5} "
                  f"{_f(fp_gt_pct):>7} {_f(fn_gt_pct):>7} "
                  f"{fp_med:>7.0f} {fp_p90:>7.0f} {fn_med:>7.0f}  n={n_active}")

        print()
        print(f"  Легенда:")
        print(f"    gt_qty   = реальный спрос в горизонте (не меняется при смене policy)")
        print(f"    fp/gt%   = FP / gt × 100%  ← лишняя закупка % от реального спроса")
        print(f"    fn/gt%   = FN / gt × 100%  ← пропущенный спрос % от реального")
        print(f"    fp_med   = медиана FP за один cutoff  ← типичный риск одной закупки")
        print(f"    fp_p90   = P90 FP за один cutoff      ← worst-case без хвоста")
        print(f"    fn_med   = медиана FN за один cutoff  ← типичный недозаказ")
        print(f"    n        = число активных cutoff-дат (gt>0 или pred>0)")

    large_recs      = [r for r in records if r.segment == "LARGE"]
    large_plus_recs = [r for r in records if r.segment == "LARGE_PLUS"]

    _run_segment("LARGE (500–999 шт.)", large_recs)
    _run_segment("LARGE_PLUS (>=1000 шт.)", large_plus_recs)

    if not large_recs and not large_plus_recs:
        print("\n  Нет LARGE/LARGE_PLUS записей для policy simulation.")

    print()
    print(f"  Кандидат на временное правило (ожидает валидации по цифрам выше):")
    print(f"    LARGE ≥500 без DPM: 1 <= order_age_days <= 3 → ESTIMATED_LARGE_ORDER")
    print(f"    age=0 исключён: LARGE age=0 даёт precision 0.74 — слишком много FP.")
    print(f"    Финальное правило выбирается по fp/gt% и fn/gt%, не по абсолютным штукам.")


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
