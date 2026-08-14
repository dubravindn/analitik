"""
Слой CustomerOrder — known demand для BASE.

Загружает оформленные заказы из МойСклад API и возвращает
позиции, которые будут отгружены в горизонте прогноза.

Правило: NO FUTURE LEAKAGE.
  Используем только CO, созданные до cutoff_date.
  CO.moment <= cutoff_date — жёсткое ограничение.

Привязка к горизонту — три уровня (по убыванию точности):
  A. deliveryPlannedMoment в [horizon_from, horizon_to]
  B. CO.moment + TYPICAL_LEAD_DAYS попадает в горизонт (heuristic)
  C. is_preorder без даты — включается всегда
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

BASE_API = "https://api.moysklad.ru/api/remap/1.2"

# Из backtest: первый bucket, где >60% объёма известно за >=7 дней.
LARGE_ORDER_THRESHOLD_DEFAULT: int = 500  # штук

# Типичный lead CO→отгрузка (дней) когда deliveryPlannedMoment не заполнен.
# Используется только как fallback (tier B). Не прибавляется к stat.
TYPICAL_LEAD_DAYS_DEFAULT: int = 3


@dataclass
class COPosition:
    """Позиция CustomerOrder, попадающая в горизонт прогноза."""
    co_id:        str
    co_date:      date       # CO.moment (дата создания заказа)
    delivery_date: date | None  # deliveryPlannedMoment (None если не заполнен)
    product_id:   str
    product_name: str
    ordered_qty:  float
    is_preorder:  bool       # ПРЕДОПЛАТА или праздничный флаг
    lead_days:    int | None  # co_date → delivery_date (None если нет delivery)
    date_source:  str = "explicit"  # "explicit" | "heuristic" | "preorder_no_date"


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


_PREORDER_KEYWORDS = ("предоплат", "предзаказ", "MARCH_8", "8 марта", "14 февр")

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
    typical_lead_days: int = TYPICAL_LEAD_DAYS_DEFAULT,
) -> list[COPosition]:
    """
    Загружает CO-позиции, которые попадают в горизонт прогноза.

    Фильтрация:
      1. CO.moment <= cutoff_date  (NO FUTURE LEAKAGE — жёсткое)
      2. Привязка к горизонту [horizon_from, horizon_to] — три уровня:
         A. deliveryPlannedMoment в горизонте  (точно)
         B. CO.moment + typical_lead_days в горизонте  (эвристика)
         C. is_preorder без даты  (всегда включаем)

    Уровень B используется ТОЛЬКО если deliveryPlannedMoment не заполнен.
    Это позволяет не выбрасывать реальный спрос молча.

    Примечание: когда deliveryPlannedMoment всегда пустой (типичная ситуация),
    большинство CO попадают через tier B. В этом случае stat_residual остаётся
    корректным upper bound — формула max(known, stat) не меняется.
    """
    positions: list[COPosition] = []
    stats_log: dict[str, int] = {
        "fetched": 0,
        "future_co_skipped": 0,
        "tier_a": 0,
        "tier_b": 0,
        "tier_c": 0,
        "out_of_horizon": 0,
        "positions_added": 0,
    }

    # Фильтруем CO созданные до cutoff (правильный фильтр)
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

        # NO FUTURE LEAKAGE (дублируем проверку на случай ошибки API)
        if co_date > cutoff_date:
            stats_log["future_co_skipped"] += 1
            continue

        co_name = co.get("name", "")
        is_pre  = _is_preorder(co_name)

        # Читаем deliveryPlannedMoment (правильное поле МойСклад)
        dpm_str = co.get("deliveryPlannedMoment", "") or ""
        delivery_date: date | None = None
        if dpm_str:
            try:
                delivery_date = date.fromisoformat(dpm_str[:10])
            except ValueError:
                pass

        # ── Tier A: deliveryPlannedMoment явно в горизонте ───────────────────
        if delivery_date is not None:
            if horizon_from <= delivery_date <= horizon_to:
                date_src = "explicit"
                stats_log["tier_a"] += 1
            else:
                stats_log["out_of_horizon"] += 1
                continue  # дата есть, но вне горизонта → пропуск

        # ── Tier B: нет даты → эвристика CO.moment + typical_lead ───────────
        elif not is_pre:
            estimated = co_date + timedelta(days=typical_lead_days)
            if horizon_from <= estimated <= horizon_to:
                date_src = "heuristic"
                delivery_date = estimated  # используем как оценку
                stats_log["tier_b"] += 1
            else:
                stats_log["out_of_horizon"] += 1
                continue  # эвристика тоже вне горизонта → пропуск

        # ── Tier C: предзаказ без даты → включаем всегда ────────────────────
        else:
            date_src = "preorder_no_date"
            stats_log["tier_c"] += 1

        lead = (delivery_date - co_date).days if delivery_date else None

        # Позиции CO
        rows = co.get("positions", {}).get("rows", [])
        for pos in rows:
            a = pos.get("assortment") or {}
            pid   = a.get("id", "")
            pname = a.get("name", "")
            qty   = float(pos.get("quantity", 0))
            if not pid or qty <= 0:
                continue
            positions.append(COPosition(
                co_id=co.get("id", ""),
                co_date=co_date,
                delivery_date=delivery_date,
                product_id=pid,
                product_name=pname,
                ordered_qty=qty,
                is_preorder=is_pre,
                lead_days=lead,
                date_source=date_src,
            ))
            stats_log["positions_added"] += 1

    # Сохраняем статистику на объект для shadow-runner
    load_known_customer_orders._last_stats = stats_log  # type: ignore[attr-defined]
    return positions


def aggregate_known_demand(
    positions: list[COPosition],
    large_order_threshold: int = LARGE_ORDER_THRESHOLD_DEFAULT,
) -> dict[str, dict]:
    """
    Агрегирует COPosition → {product_id: {known_qty, preorder_qty, large_order_qty}}.

    Deduplication: каждая физическая COPosition считается ровно один раз.
    preorder_qty — это подмножество known_qty (та же позиция, флаг is_preorder=True).
    known_qty = preorder_qty + regular_qty (их сумма, без двойного счёта).

    Доказательство no-double-count:
      expected_demand = known_qty + max(0, stat_demand - known_qty)
                      = max(stat_demand, known_qty)
    Ни preorder, ни regular_CO не складываются с stat — stat вычитается.
    """
    result: dict[str, dict] = {}
    seen_positions: set[tuple[str, str]] = set()  # (co_id, product_id) для dedup

    for pos in positions:
        pid = pos.product_id
        key = (pos.co_id, pid)

        # Дедупликация на уровне позиций (одна CO × product_id = одна запись)
        if key in seen_positions:
            continue
        seen_positions.add(key)

        if pid not in result:
            result[pid] = {
                "known_qty": 0.0,
                "preorder_qty": 0.0,
                "large_order_qty": 0.0,
                "heuristic_qty": 0.0,   # из tier B (меньше уверенности)
            }
        result[pid]["known_qty"] += pos.ordered_qty
        if pos.is_preorder:
            result[pid]["preorder_qty"] += pos.ordered_qty
        if pos.ordered_qty >= large_order_threshold:
            result[pid]["large_order_qty"] += pos.ordered_qty
        if pos.date_source == "heuristic":
            result[pid]["heuristic_qty"] += pos.ordered_qty

    return result
