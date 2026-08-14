"""
Слой загрузки данных для forecast-движка (read-only).

Все функции принимают открытое psycopg3-соединение.
Никакой бизнес-логики — только SQL → Python-структуры.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import NamedTuple

_PACK_RE = re.compile(r'(\d{1,3})\s*шт\.?', re.IGNORECASE)


def _parse_pack_size(name: str) -> int:
    m = _PACK_RE.search(name)
    if m:
        n = int(m.group(1))
        if 2 <= n <= 500:
            return n
    return 1


# ── Типы ─────────────────────────────────────────────────────────────────────

class ProductInfo(NamedTuple):
    product_id:   str
    product_name: str
    folder_path:  str
    is_srezka:    bool
    pack_size:    int = 1


class DailySales(NamedTuple):
    """Продажи одного SKU в одном магазине за один день."""
    day:           date
    store_id:      str
    assortment_id: str
    sell_qty:      float
    revenue_kop:   int


class StockSnapshot(NamedTuple):
    product_id:      str
    store_id:        str
    available_stock: float
    stock_all:       float
    reserve_qty:     float


# ── Справочники ───────────────────────────────────────────────────────────────

def load_srezka_products(conn) -> dict[str, ProductInfo]:
    """Все товары с is_srezka=TRUE из product_dim."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, product_name, COALESCE(folder_path, ''), is_srezka
            FROM product_dim
            WHERE is_srezka = TRUE
        """)
        return {
            r[0]: ProductInfo(r[0], r[1], r[2], r[3], _parse_pack_size(r[1]))
            for r in cur.fetchall()
        }


def load_products_by_store(conn, store_id: str) -> dict[str, ProductInfo]:
    """Все товары с продажами в указанном магазине (за всё время)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (p.product_id)
                p.product_id, p.product_name,
                COALESCE(p.folder_path, ''), p.is_srezka
            FROM product_dim p
            JOIN sales_by_product_day s ON s.assortment_id = p.product_id
            WHERE s.store_id = %s AND s.sell_qty > 0
        """, (store_id,))
        return {
            r[0]: ProductInfo(r[0], r[1], r[2], r[3], _parse_pack_size(r[1]))
            for r in cur.fetchall()
        }


# ── Продажи ───────────────────────────────────────────────────────────────────

def load_daily_sales(
    conn,
    store_id: str,
    date_from: date,
    date_to:   date,
    product_ids: list[str] | None = None,
) -> list[DailySales]:
    """
    Продажи по дням для магазина. Фильтр по product_ids опционален.
    """
    with conn.cursor() as cur:
        if product_ids is not None:
            cur.execute("""
                SELECT day, store_id, assortment_id,
                       COALESCE(sell_qty, 0), COALESCE(revenue_kop, 0)
                FROM sales_by_product_day
                WHERE store_id = %s
                  AND day BETWEEN %s AND %s
                  AND sell_qty > 0
                  AND assortment_id = ANY(%s)
                ORDER BY day
            """, (store_id, date_from, date_to, product_ids))
        else:
            cur.execute("""
                SELECT day, store_id, assortment_id,
                       COALESCE(sell_qty, 0), COALESCE(revenue_kop, 0)
                FROM sales_by_product_day
                WHERE store_id = %s
                  AND day BETWEEN %s AND %s
                  AND sell_qty > 0
                ORDER BY day
            """, (store_id, date_from, date_to))
        return [DailySales(r[0], r[1], r[2], float(r[3]), int(r[4])) for r in cur.fetchall()]


def load_store_daily_revenue(
    conn,
    store_id: str,
    date_from: date,
    date_to:   date,
) -> dict[date, float]:
    """Суммарная выручка магазина по дням (в копейках)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT day, SUM(revenue_kop)
            FROM sales_by_store_day
            WHERE store_id = %s AND day BETWEEN %s AND %s
            GROUP BY day
        """, (store_id, date_from, date_to))
        return {r[0]: float(r[1]) for r in cur.fetchall()}


def load_product_daily_sales_matrix(
    conn,
    store_id: str,
    date_from: date,
    date_to:   date,
) -> dict[str, dict[date, float]]:
    """
    Возвращает {product_id: {day: qty}} для эффективного расчёта rolling window.
    """
    rows = load_daily_sales(conn, store_id, date_from, date_to)
    matrix: dict[str, dict[date, float]] = defaultdict(dict)
    for row in rows:
        matrix[row.assortment_id][row.day] = row.sell_qty
    return dict(matrix)


def rolling_mean(
    sales_by_day: dict[date, float],
    end_date: date,
    window_days: int,
    oper_start: date | None = None,
) -> float:
    """
    Среднедневные продажи за calendar-window до end_date.
    Дни до oper_start исключаются (предоперационные нули не учитываются).
    Если данных нет — возвращает 0.0.
    """
    start = end_date - timedelta(days=window_days - 1)
    if oper_start is not None:
        start = max(start, oper_start)
    n_days = (end_date - start).days + 1
    if n_days <= 0:
        return 0.0
    total = sum(
        sales_by_day.get(start + timedelta(days=i), 0.0)
        for i in range(n_days)
    )
    return total / n_days


# ── Остаток ───────────────────────────────────────────────────────────────────

def load_stock_snapshot(
    conn,
    store_id: str,
    product_ids: list[str] | None = None,
    snap_day: date | None = None,
) -> dict[str, StockSnapshot]:
    """
    Последний известный остаток из stock_snapshot.

    snap_day — конкретный день снимка (обычно MAX(day) <= cutoff_date).
    Если не задан — берётся MAX(day) по данному магазину.
    Возвращает пустой словарь если таблица не существует или данных нет.
    """
    try:
        with conn.cursor() as cur:
            if snap_day is None:
                cur.execute(
                    "SELECT MAX(day) FROM stock_snapshot WHERE store_id = %s",
                    (store_id,),
                )
                row = cur.fetchone()
                snap_day = row[0] if row and row[0] else None
            if snap_day is None:
                return {}

            if product_ids is not None:
                cur.execute("""
                    SELECT product_id, store_id,
                           COALESCE(available_qty, 0),
                           COALESCE(stock_qty, 0),
                           COALESCE(reserve_qty, 0)
                    FROM stock_snapshot
                    WHERE store_id = %s AND day = %s AND product_id = ANY(%s)
                """, (store_id, snap_day, product_ids))
            else:
                cur.execute("""
                    SELECT product_id, store_id,
                           COALESCE(available_qty, 0),
                           COALESCE(stock_qty, 0),
                           COALESCE(reserve_qty, 0)
                    FROM stock_snapshot
                    WHERE store_id = %s AND day = %s
                """, (store_id, snap_day))
            return {
                r[0]: StockSnapshot(r[0], r[1], float(r[2]), float(r[3]), float(r[4]))
                for r in cur.fetchall()
            }
    except Exception:
        return {}


# ── Магазины ──────────────────────────────────────────────────────────────────

def load_store_names(conn) -> dict[str, str]:
    """store_id → store_name из sales_by_store_day."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT store_id, store_name
            FROM sales_by_store_day
        """)
        return {r[0]: r[1] for r in cur.fetchall()}


def load_store_operational_start(conn, store_id: str) -> date | None:
    """Первый день с ненулевыми продажами в магазине."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MIN(day)
            FROM sales_by_store_day
            WHERE store_id = %s AND revenue_kop > 0
        """, (store_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None
