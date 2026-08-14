"""
Слой CustomerOrder — known demand для BASE.

Загружает оформленные заказы из МойСклад API и возвращает
позиции, которые попадают в горизонт прогноза.

Правило: NO FUTURE LEAKAGE.
  Используем только CO, созданные до cutoff_date.
  CO.moment <= cutoff_date — жёсткое ограничение.

Три уровня привязки к горизонту (по убыванию точности):
  A. deliveryPlannedMoment в [horizon_from, horizon_to]          date_source="explicit"
  B1. LARGE 500–999 + нет DPM + age 0–5d + remaining>0          date_source="large_estimated"
  B2. LARGE_PLUS >=1000 + нет DPM + age 0–3d + remaining>0      date_source="large_estimated"
  C. is_preorder + event-window (MARCH_8/VALENTINE)              date_source="preorder_event"

Tier B policy принята по policy-sim v2 (backtest глобальный ground truth):
  LARGE 500–999:   age 0–5d → precision 0.76, recall 0.81, FP 25.7%, FN 18.9%
  LARGE_PLUS >=1000: age 0–3d → precision 0.87, recall 1.00, FP 14.7%, FN 0.2%
  NORMAL <500 без DPM → не включать (P50 lead=0, 77% same-day → высокий FP, recall=1.00 NORMAL ≠ сигнал).
  age=0 включён: исключение age=0 убивает recall ([1,3] → FN=69% для LARGE).

Tier C — вне event-window CO-предзаказы не включаются.
  Флаг: PREORDER_NO_EVENT_WINDOW.

Параметры хранятся в конфигурации и переоцениваются по мере накопления истории.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from hermes.forecast_models import ForecastMode, forecast_mode_for

BASE_API = "https://api.moysklad.ru/api/remap/1.2"

LARGE_ORDER_THRESHOLD_DEFAULT: int = 500    # штук — нижняя граница LARGE
LARGE_PLUS_ORDER_THRESHOLD:   int = 1000   # штук — LARGE_PLUS (>=1000)
MAX_CO_AGE_FOR_LARGE:         int = 5      # дней — tier B для LARGE 500–999
MAX_CO_AGE_FOR_LARGE_PLUS:    int = 3      # дней — tier B для LARGE_PLUS >=1000

# Обратная совместимость
MAX_CO_AGE_FOR_ESTIMATED = MAX_CO_AGE_FOR_LARGE


@dataclass
class COPosition:
    """Позиция CustomerOrder, попадающая в горизонт прогноза."""
    co_id:        str
    co_date:      date        # CO.moment (дата создания)
    delivery_date: date | None  # deliveryPlannedMoment (None если не заполнен)
    product_id:   str
    product_name: str
    ordered_qty:  float       # суммарное количество в позиции
    remaining_qty: float      # ordered - shipped; = ordered если shipped неизвестен
    remaining_qty_uncertain: bool  # True если shipped не вернулся из API
    age_days:     int         # (cutoff_date - co_date).days
    is_preorder:  bool        # ПРЕДОПЛАТА или праздничный флаг
    lead_days:    int | None  # co_date → delivery_date (None если нет delivery)
    date_source:  str = "explicit"
    # Значения: "explicit" | "large_estimated" | "preorder_event"


def _get(token: str, path: str, params: dict | None = None) -> dict:
    url = BASE_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("User-Agent", "hermes-forecast/1.0")
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


def _paginate(token: str, path: str, params: dict) -> Iterator[dict]:
    offset, total = 0, None
    while True:
        r = _get(token, path, {**params, "limit": 100, "offset": offset})
        rows = r.get("rows", [])
        yield from rows
        if total is None:
            total = r.get("meta", {}).get("size", 0)
        offset += len(rows)
        if not rows or offset >= (total or 0):
            break
        time.sleep(0.2)


_PREORDER_KEYWORDS = ("предоплат", "предзаказ", "march_8", "8 марта", "14 февр")

def _is_preorder(co_name: str) -> bool:
    name_lower = co_name.lower()
    return any(k.lower() in name_lower for k in _PREORDER_KEYWORDS)


def load_known_customer_orders(
    token:        str,
    store_href:   str,
    cutoff_date:  date,
    horizon_from: date,
    horizon_to:   date,
    large_order_threshold: int = LARGE_ORDER_THRESHOLD_DEFAULT,
    large_plus_threshold:  int = LARGE_PLUS_ORDER_THRESHOLD,
    max_age_large:         int = MAX_CO_AGE_FOR_LARGE,
    max_age_large_plus:    int = MAX_CO_AGE_FOR_LARGE_PLUS,
    typical_lead_days: int = 3,  # deprecated, kept for API compat
) -> list[COPosition]:
    """
    Загружает CO-позиции в горизонт прогноза с 3-уровневым тиром.

    Tier A: deliveryPlannedMoment явно попадает в [horizon_from, horizon_to].
            Количество = ordered_qty (DPM подтверждён).

    Tier B: CO без DPM, LARGE (total_qty >= threshold), remaining > 0.
            Количество = remaining_qty = ordered_qty - shipped_qty.
            date_source = "large_estimated".
            LARGE 500–999: age 0–5d; LARGE_PLUS >=1000: age 0–3d.
            NORMAL <500 без DPM → пропуск (P50 lead=0, 77% same-day → высокий FP).

    Tier C: Предзаказ (ключевые слова в названии CO) БЕЗ явного DPM,
            ТОЛЬКО в event-window (MARCH_8/VALENTINE).
            Вне event-window → пропуск (флаг PREORDER_NO_EVENT_WINDOW в aggregate).
            date_source = "preorder_event".
    """
    mode = forecast_mode_for(horizon_from)
    event_window = mode in (ForecastMode.MARCH_8, ForecastMode.VALENTINE)

    positions: list[COPosition] = []
    stats_log: dict[str, int] = {
        "fetched": 0,
        "future_co_skipped": 0,
        "tier_a": 0,
        "tier_b": 0,
        "tier_c": 0,
        "preorder_out_of_window": 0,
        "out_of_horizon": 0,
        "positions_added": 0,
        "positions_skipped_remaining_zero": 0,
    }

    params = {
        "filter": (
            f"store={store_href}"
            f";moment<={cutoff_date.strftime('%Y-%m-%d 23:59:59')}"
        ),
        "order": "moment,desc",
        "expand": "positions.assortment",
    }

    for co in _paginate(token, "/entity/customerorder", params):
        stats_log["fetched"] += 1

        co_moment_str = co.get("moment", "")
        if not co_moment_str:
            continue
        co_date = date.fromisoformat(co_moment_str[:10])

        # NO FUTURE LEAKAGE
        if co_date > cutoff_date:
            stats_log["future_co_skipped"] += 1
            continue

        co_name = co.get("name", "")
        is_pre  = _is_preorder(co_name)
        age_days = (cutoff_date - co_date).days

        # deliveryPlannedMoment
        dpm_str = co.get("deliveryPlannedMoment", "") or ""
        delivery_date: date | None = None
        if dpm_str:
            try:
                delivery_date = date.fromisoformat(dpm_str[:10])
            except ValueError:
                pass

        # ── Определяем tier ──────────────────────────────────────────────────
        rows = co.get("positions", {}).get("rows", [])
        if not rows:
            continue

        # Суммарный ordered_qty для threshold-проверки Tier B
        total_ordered = sum(float(p.get("quantity", 0)) for p in rows)

        if delivery_date is not None:
            # ── Tier A ───────────────────────────────────────────────────────
            if horizon_from <= delivery_date <= horizon_to:
                date_src = "explicit"
                stats_log["tier_a"] += 1
            else:
                stats_log["out_of_horizon"] += 1
                continue

        elif is_pre:
            # ── Tier C ───────────────────────────────────────────────────────
            if event_window:
                date_src = "preorder_event"
                stats_log["tier_c"] += 1
            else:
                stats_log["preorder_out_of_window"] += 1
                continue  # вне event-window — не включаем

        elif total_ordered >= large_order_threshold:
            # ── Tier B: LARGE / LARGE_PLUS ───────────────────────────────────
            # LARGE_PLUS (>=1000): age 0–3d; LARGE (500–999): age 0–5d
            if total_ordered >= large_plus_threshold:
                max_age = max_age_large_plus
            else:
                max_age = max_age_large
            if age_days <= max_age:
                date_src = "large_estimated"
                stats_log["tier_b"] += 1
            else:
                stats_log["out_of_horizon"] += 1
                continue

        else:
            # NORMAL <500 без DPM → не включаем
            stats_log["out_of_horizon"] += 1
            continue

        lead = (delivery_date - co_date).days if delivery_date else None

        # ── Позиции ──────────────────────────────────────────────────────────
        for pos in rows:
            a = pos.get("assortment") or {}
            pid   = a.get("id", "")
            pname = a.get("name", "")
            qty   = float(pos.get("quantity", 0))
            if not pid or qty <= 0:
                continue

            # Вычисляем remaining_qty
            shipped_raw = pos.get("shipped")
            if shipped_raw is not None:
                shipped = float(shipped_raw)
                remaining_qty      = max(0.0, qty - shipped)
                remaining_uncertain = False
            else:
                remaining_qty      = qty  # консервативно: считаем, что всё ещё нужно
                remaining_uncertain = True

            # Tier B: пропускаем позиции с remaining <= 0 (уже отгружено полностью)
            if date_src == "large_estimated" and remaining_qty <= 0:
                stats_log["positions_skipped_remaining_zero"] += 1
                continue

            positions.append(COPosition(
                co_id=co.get("id", ""),
                co_date=co_date,
                delivery_date=delivery_date,
                product_id=pid,
                product_name=pname,
                ordered_qty=qty,
                remaining_qty=remaining_qty,
                remaining_qty_uncertain=remaining_uncertain,
                age_days=age_days,
                is_preorder=is_pre,
                lead_days=lead,
                date_source=date_src,
            ))
            stats_log["positions_added"] += 1

    load_known_customer_orders._last_stats = stats_log  # type: ignore[attr-defined]
    return positions


def aggregate_known_demand(
    positions: list[COPosition],
    large_order_threshold: int = LARGE_ORDER_THRESHOLD_DEFAULT,
) -> dict[str, dict]:
    """
    Агрегирует COPosition → {product_id: {...}} с 4-компонентной декомпозицией.

    Компоненты:
      explicit_qty        — Tier A (DPM в горизонте): ordered_qty
      estimated_large_qty — Tier B (LARGE estimated): remaining_qty
      preorder_qty        — Tier C (preorder event): ordered_qty
      known_qty           — сумма всех трёх (no double count)

    Доказательство no-double-count:
      expected = known_qty + max(0, stat - known_qty) = max(stat, known_qty)
    Ни одна позиция не попадает в два тира одновременно (date_source уникален).
    """
    result: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()  # (co_id, product_id)

    for pos in positions:
        pid = pos.product_id
        key = (pos.co_id, pid)
        if key in seen:
            continue
        seen.add(key)

        if pid not in result:
            result[pid] = {
                "known_qty":              0.0,
                "explicit_qty":           0.0,   # Tier A
                "estimated_large_qty":    0.0,   # Tier B
                "preorder_qty":           0.0,   # Tier C
                "large_order_qty":        0.0,   # backward compat
                "heuristic_qty":          0.0,   # deprecated (old Tier B)
                "has_remaining_uncertain": False, # любая Tier-B позиция с uncertain remaining
            }

        # Tier B использует remaining_qty; Tier A/C — ordered_qty
        if pos.date_source == "large_estimated":
            qty = pos.remaining_qty
            result[pid]["estimated_large_qty"] += qty
            if pos.remaining_qty_uncertain:
                result[pid]["has_remaining_uncertain"] = True
        elif pos.date_source == "explicit":
            qty = pos.ordered_qty
            result[pid]["explicit_qty"] += qty
        else:  # preorder_event
            qty = pos.ordered_qty
            result[pid]["preorder_qty"] += qty

        result[pid]["known_qty"] += qty

        if pos.ordered_qty >= large_order_threshold:
            result[pid]["large_order_qty"] += qty

    return result
