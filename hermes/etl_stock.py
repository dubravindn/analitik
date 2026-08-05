"""ETL остатков: выгружает снимок stock/all из МойСклад и сохраняет в БД.

Запускать раз в день (утром, перед формированием отчёта).
"""
from __future__ import annotations

import logging
from datetime import date

from .moysklad import MoyskladClient

log = logging.getLogger("hermes.etl_stock")

_PAGE = 1000


def _build_folder_index(client: MoyskladClient) -> dict[str, dict]:
    """Возвращает dict: folder_id → {path, is_srezka}.

    is_srezka=True если полный путь папки содержит 'СРЕЗКА'.
    """
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
    log.info("Загружено групп товаров: %d (СРЕЗКА: %d)",
             len(index), sum(1 for v in index.values() if v["is_srezka"]))
    return index


def _fetch_stock(client: MoyskladClient) -> list[dict]:
    """Полная выгрузка report/stock/all, сгруппированная по товару."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = client._get("/report/stock/all", {
            "limit": _PAGE,
            "offset": offset,
            "groupBy": "product",
        })
        batch = page.get("rows", [])
        rows.extend(batch)
        size = page.get("meta", {}).get("size", 0)
        offset += len(batch)
        if offset >= size or not batch:
            break
    log.info("Получено строк stock/all: %d", len(rows))
    return rows


def run(client: MoyskladClient, conn, day: date | None = None) -> int:
    """Сохранить снимок остатков на дату `day` (по умолчанию — сегодня).

    Возвращает количество записей.
    """
    if day is None:
        day = date.today()

    folder_idx = _build_folder_index(client)
    stock_rows = _fetch_stock(client)

    records = []
    for r in stock_rows:
        # ID товара из meta.href; пропускаем услуги (/entity/service/) и комплекты (/bundle/)
        meta_href = r.get("meta", {}).get("href", "")
        if not meta_href or "/entity/product/" not in meta_href:
            continue
        product_id = meta_href.split("/")[-1]

        folder_meta = r.get("folder", {}).get("meta", {}).get("href", "")
        folder_id = folder_meta.split("/")[-1] if folder_meta else None
        folder_info = folder_idx.get(folder_id, {"path": "", "is_srezka": False})

        stock_qty = r.get("stock", 0) or 0
        reserve_qty = r.get("reserve", 0) or 0
        available_qty = r.get("quantity", 0) or 0
        # price — средневзвешенная себестоимость единицы в копейках
        cost_price_kop = round(r.get("price", 0) or 0)

        records.append((
            day,
            product_id,
            r.get("name", ""),
            folder_id,
            folder_info["path"],
            folder_info["is_srezka"],
            stock_qty,
            reserve_qty,
            available_qty,
            cost_price_kop,
        ))

    if not records:
        log.warning("stock/all вернул 0 строк")
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO stock_snapshot
                (day, product_id, product_name, folder_id, folder_path, is_srezka,
                 stock_qty, reserve_qty, available_qty, cost_price_kop)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (day, product_id) DO UPDATE SET
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
    log.info("Снимок остатков на %s: %d позиций", day, len(records))
    return len(records)
