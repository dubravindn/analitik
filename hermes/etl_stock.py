"""ETL остатков: выгружает снимок stock/all по каждому складу из МойСклад.

Запускать раз в день (утром, перед формированием отчёта).
"""
from __future__ import annotations

import logging
from datetime import date

from . import config
from .moysklad import MoyskladClient, BASE_URL

log = logging.getLogger("hermes.etl_stock")

_PAGE = 1000


def _build_folder_index(client: MoyskladClient) -> dict[str, dict]:
    """dict: folder_id → {path, is_srezka}. is_srezka если путь содержит 'СРЕЗКА'."""
    index: dict[str, dict] = {}
    offset = 0
    while True:
        page = client._get("/entity/productfolder", {"limit": 100, "offset": offset})
        rows = page.get("rows", [])
        for f in rows:
            fid = f["id"]
            path_name = f.get("pathName", "")
            name = f.get("name", "")
            full_path = f"{path_name}/{name}".strip("/")
            index[fid] = {
                "path": full_path,
                "is_srezka": "СРЕЗКА" in full_path,
            }
        size = page.get("meta", {}).get("size", 0)
        offset += len(rows)
        if offset >= size or not rows:
            break
    srezka_count = sum(1 for v in index.values() if v["is_srezka"])
    log.info("Загружено групп товаров: %d (СРЕЗКА: %d)", len(index), srezka_count)
    return index


def _fetch_store_stock(client: MoyskladClient, store_href: str) -> list[dict]:
    """Остатки конкретного склада (все страницы)."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = client._get("/report/stock/all", {
            "limit": _PAGE,
            "offset": offset,
            "filter": f"store={store_href}",
        })
        batch = page.get("rows", [])
        rows.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    return rows


def run(client: MoyskladClient, conn, day: date | None = None) -> int:
    """Сохранить снимок остатков на дату `day` (по умолчанию — сегодня).

    Возвращает суммарное количество записей.
    """
    if day is None:
        day = date.today()

    folder_idx = _build_folder_index(client)

    total = 0
    store_channels = config.STORE_CHANNELS  # dict: store_id → channel

    # Получаем имена складов из API
    store_name_by_id = {s["id"]: s["name"] for s in client.stores()}

    for store_id, channel in store_channels.items():
        store_href = f"{BASE_URL}/entity/store/{store_id}"
        store_name = store_name_by_id.get(store_id, store_id)
        raw_rows = _fetch_store_stock(client, store_href)

        records = []
        for r in raw_rows:
            meta_href = r.get("meta", {}).get("href", "")
            # Пропускаем услуги и комплекты — только физические товары
            if not meta_href or "/entity/product/" not in meta_href:
                continue
            product_id = meta_href.split("/")[-1]

            stock_qty = float(r.get("stock", 0) or 0)
            if stock_qty <= 0:
                continue  # остаток нулевой — незачем хранить

            folder_meta = r.get("folder", {}).get("meta", {}).get("href", "")
            folder_id = folder_meta.split("/")[-1] if folder_meta else None
            folder_info = folder_idx.get(folder_id, {"path": "", "is_srezka": False})

            records.append((
                day,
                store_id,
                store_name,
                product_id,
                r.get("name", ""),
                folder_id,
                folder_info["path"],
                folder_info["is_srezka"],
                stock_qty,
                float(r.get("reserve", 0) or 0),
                float(r.get("quantity", 0) or 0),
                round(r.get("price", 0) or 0),
            ))

        if records:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO stock_snapshot
                        (day, store_id, store_name, product_id, product_name,
                         folder_id, folder_path, is_srezka,
                         stock_qty, reserve_qty, available_qty, cost_price_kop)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (day, store_id, product_id) DO UPDATE SET
                        store_name     = EXCLUDED.store_name,
                        product_name   = EXCLUDED.product_name,
                        folder_id      = EXCLUDED.folder_id,
                        folder_path    = EXCLUDED.folder_path,
                        is_srezka      = EXCLUDED.is_srezka,
                        stock_qty      = EXCLUDED.stock_qty,
                        reserve_qty    = EXCLUDED.reserve_qty,
                        available_qty  = EXCLUDED.available_qty,
                        cost_price_kop = EXCLUDED.cost_price_kop,
                        synced_at      = now()
                    """,
                    records,
                )
            conn.commit()
        log.info("Остатки на %s | %s | %d позиций", day, store_id[:8], len(records))
        total += len(records)

    log.info("Снимок остатков на %s: итого %d позиций", day, total)
    return total
