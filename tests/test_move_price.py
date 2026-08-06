"""Регрессия E2.3: перемещения считаются по закупочным ценам из приёмок.

Для покрытых позиций перемещения закупочная цена на дату документа
(purchase_price_at) определена и является себестоимостью, а не ценой продажи
(цена продажи была бы примерно вдвое выше). Требует БД.
"""
import pytest


def _conn_or_skip():
    try:
        from hermes import config, db
        return db.connect(config.DATABASE_URL())
    except Exception as e:
        pytest.skip(f"нет доступной БД: {e}")


def test_move_covered_by_purchase_prices():
    conn = _conn_or_skip()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE pp.price_kop IS NOT NULL AND pp.price_kop > 0),
                   COUNT(*)
            FROM move_item i
            JOIN move_doc d ON d.doc_id = i.doc_id
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = i.product_id AND p.priced_from <= d.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
        """)
        covered, total = cur.fetchone()
    if not total:
        pytest.skip("нет позиций перемещений")
    # Хотя бы часть позиций покрыта приёмками — иначе product_id не связался.
    assert covered > 0, "ни одна позиция перемещения не покрыта закупочными ценами"


def test_move_purchase_price_is_cost_not_sale():
    conn = _conn_or_skip()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT AVG(pp.price_kop::numeric / NULLIF(i.cost_kop, 0)), COUNT(*)
            FROM move_item i
            JOIN move_doc d ON d.doc_id = i.doc_id
            JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = i.product_id AND p.priced_from <= d.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE i.cost_kop > 0
        """)
        ratio, n = cur.fetchone()
    if not n:
        pytest.skip("нет покрытых позиций перемещений")
    # Закупочная цена ≈ себестоимость (партии колеблются, но это не цена продажи ~2×).
    assert 0.4 < float(ratio) < 2.0, f"purchase/cost ratio={ratio} — похоже на цену продажи"
