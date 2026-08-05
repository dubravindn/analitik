"""Регрессия D0.2: цена в перемещении — себестоимость, а не цена продажи.

Позиции, встречающиеся и в move_item, и в stock_snapshot за близкие даты,
должны иметь близкую цену. Если МойСклад где-то отдаст в price цену продажи,
она была бы примерно вдвое выше себестоимости — тест это поймёт. Требует БД.
"""
import pytest


def _conn_or_skip():
    try:
        from hermes import config, db
        return db.connect(config.DATABASE_URL())
    except Exception as e:
        pytest.skip(f"нет доступной БД: {e}")


def test_move_price_is_cost_not_sale():
    conn = _conn_or_skip()
    with conn.cursor() as cur:
        # Средняя цена позиции в перемещениях и её себестоимость в остатках
        # (последний снимок), сопоставление по названию товара.
        cur.execute("""
            SELECT AVG(mi.cost_kop::numeric / NULLIF(ss.cost, 0))
            FROM (
                SELECT product_name, AVG(cost_kop) AS cost_kop
                FROM move_item WHERE cost_kop > 0 GROUP BY product_name
            ) mi
            JOIN (
                SELECT product_name, AVG(cost_price_kop) AS cost
                FROM stock_snapshot
                WHERE day = (SELECT MAX(day) FROM stock_snapshot) AND cost_price_kop > 0
                GROUP BY product_name
            ) ss ON ss.product_name = mi.product_name
        """)
        ratio = cur.fetchone()[0]
    if ratio is None:
        pytest.skip("нет пересечения move_item и stock_snapshot")
    # Себестоимость ≈ себестоимость: отношение около 1. Цена продажи дала бы ~2.
    assert 0.5 < float(ratio) < 1.5, f"move/stock cost ratio={ratio} — похоже на цену продажи"
