"""ETL цен номенклатуры: снимок цен из карточек товара /entity/product.

Блок G: закупочная цена (buyPrice) берётся из карточки товара, а не из приёмок.
Снимок раз в сутки даёт историю: если цену поменяли 20.07, продажи 15.07
считаются по старой цене (по снимку на ту дату), а не задним числом.
"""
from __future__ import annotations

import json
import logging

from .moysklad import MoyskladClient
from . import config

log = logging.getLogger("hermes.etl_prices")

_PAGE = 100   # /entity/product с большим limit отдаёт огромный медленный ответ
_MAX_KOP = 10**15   # sentinel-мусор из карточек (как в поставках) — не хранить


def _price_value(node) -> int:
    """buyPrice/minPrice → копейки. МойСклад отдаёт value в копейках."""
    if isinstance(node, dict):
        return round(node.get("value", 0) or 0)
    return 0


def run(client: MoyskladClient, conn, day=None) -> int:
    """Снимок цен всех товаров на дату day (по умолчанию сегодня, МСК)."""
    day = day or config.msk_today()
    offset = 0
    records: list[tuple] = []
    while True:
        page = client._get("/entity/product", {"limit": _PAGE, "offset": offset})
        rows = page.get("rows", [])
        for pr in rows:
            pid = (pr.get("id", "") or "").split("?")[0]
            if not pid:
                continue
            name = pr.get("name", "")
            buy = _price_value(pr.get("buyPrice"))
            mn  = _price_value(pr.get("minPrice"))
            if buy > _MAX_KOP:
                buy = 0
            if mn > _MAX_KOP:
                mn = 0
            sale: dict[str, int] = {}
            for sp in pr.get("salePrices", []) or []:
                pt = (sp.get("priceType") or {}).get("name", "")
                val = round(sp.get("value", 0) or 0)
                if pt and 0 < val <= _MAX_KOP:
                    sale[pt] = val
            records.append((day, pid, name, buy, mn, json.dumps(sale, ensure_ascii=False)))
        size = page.get("meta", {}).get("size", 0)
        offset += len(rows)
        log.info("  цены: %d/%d товаров", offset, size)
        if offset >= size or not rows:
            break

    if records:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO product_price
                    (day, product_id, product_name, buy_price_kop, min_price_kop, sale_prices)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (day, product_id) DO UPDATE SET
                    product_name=EXCLUDED.product_name,
                    buy_price_kop=EXCLUDED.buy_price_kop,
                    min_price_kop=EXCLUDED.min_price_kop,
                    sale_prices=EXCLUDED.sale_prices,
                    synced_at=now()
            """, records)
        conn.commit()

    with_buy = sum(1 for r in records if r[3] > 0)
    log.info("Снимок цен на %s: %d товаров (с закупочной ценой: %d)", day, len(records), with_buy)
    return len(records)
