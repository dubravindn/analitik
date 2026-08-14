"""
Слой CustomerOrder — known demand для BASE.

Загружает оформленные заказы из МойСклад API и возвращает
позиции, которые будут отгружены в горизонте прогноза.

Правило: NO FUTURE LEAKAGE.
  Используем только CO, созданные до cutoff_date.
  CO.moment <= cutoff_date — жёсткое ограничение.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Iterator

BASE_API = "https://api.moysklad.ru/api/remap/1.2"

# Из backtest: первый bucket, где >60% объёма известно за >=7 дней.
# Вынесено в константу — не прятать в формулу.
# Переоценивать через 3-6 месяцев данных.
LARGE_ORDER_THRESHOLD_DEFAULT: int = 500  # штук


@dataclass
class COPosition:
    """Позиция CustomerOrder, попадающая в горизонт прогноза."""
    co_id:        str
    co_date:      date       # CO.moment (дата создания заказа)
    delivery_date: date | None  # планируемая дата отгрузки (может быть None)
    product_id:   str
    product_name: str
    ordered_qty:  float
    is_preorder:  bool       # ПРЕДОПЛАТА или праздничный флаг
    lead_days:    int | None  # co_date → delivery_date (None если нет delivery)


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
) -> list[COPosition]:
    """
    Загружает CO-позиции, которые:
      1. CO создан ДО cutoff_date (CO.moment <= cutoff_date — no future leakage)
      2. Delivery ожидается в [horizon_from, horizon_to]
         (если delivery_date неизвестна — не включаем без явного флага)

    Возвращает список COPosition для суммирования по product_id.

    Примечание: delivery_date = поле deliveredBy в CO или отсутствует.
    Если поле не заполнено, CO попадает в список только при явном is_preorder.
    """
    positions: list[COPosition] = []

    # Загружаем CO за период, чуть шире горизонта
    filter_from = horizon_from.strftime("%Y-%m-%d 00:00:00")
    filter_to   = (horizon_to if horizon_to >= cutoff_date else cutoff_date).strftime(
        "%Y-%m-%d 23:59:59"
    )

    params = {
        "filter": f"store={store_href};moment>={filter_from};moment<={filter_to}",
        "expand": "positions.assortment",
    }

    for co in _paginate(token, "/entity/customerorder", params):
        co_moment_str = co.get("moment", "")
        if not co_moment_str:
            continue
        co_date = date.fromisoformat(co_moment_str[:10])

        # NO FUTURE LEAKAGE: только CO созданные до cutoff
        if co_date > cutoff_date:
            continue

        delivered_by_str = co.get("deliveredBy", "") or ""
        delivery_date: date | None = None
        if delivered_by_str:
            try:
                delivery_date = date.fromisoformat(delivered_by_str[:10])
            except ValueError:
                pass

        co_name = co.get("name", "")
        is_pre = _is_preorder(co_name)

        # CO без delivery_date включаем только если это предзаказ
        if delivery_date is None and not is_pre:
            continue

        # Delivery должна быть в горизонте (или предзаказ без даты)
        if delivery_date and not (horizon_from <= delivery_date <= horizon_to):
            continue

        lead = (delivery_date - co_date).days if delivery_date else None

        # Позиции CO
        rows = co.get("positions", {}).get("rows", [])
        for pos in rows:
            a = pos.get("assortment") or {}
            pid = a.get("id", "")
            pname = a.get("name", "")
            qty = float(pos.get("quantity", 0))
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
            ))

    return positions


def aggregate_known_demand(
    positions: list[COPosition],
    large_order_threshold: int = LARGE_ORDER_THRESHOLD_DEFAULT,
) -> dict[str, dict]:
    """
    Агрегирует COPosition → {product_id: {known_qty, preorder_qty, large_order_qty}}.
    large_order_qty = qty из CO с ordered_qty >= threshold (по позиции).
    """
    result: dict[str, dict] = {}
    for pos in positions:
        pid = pos.product_id
        if pid not in result:
            result[pid] = {"known_qty": 0.0, "preorder_qty": 0.0, "large_order_qty": 0.0}
        result[pid]["known_qty"] += pos.ordered_qty
        if pos.is_preorder:
            result[pid]["preorder_qty"] += pos.ordered_qty
        if pos.ordered_qty >= large_order_threshold:
            result[pid]["large_order_qty"] += pos.ordered_qty
    return result
