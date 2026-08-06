"""H3: отчёт качества цен — товары без закупочной цены, приоритет продаваемым."""
from datetime import date

from hermes import report_prices, config


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows = self.conn.script(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def cursor(self):
        return _Cur(self)

    def script(self, sql):
        today = config.msk_today()
        if "MAX(day) FROM product_price" in sql:
            return [(today,)]
        if "FROM product_dim pd" in sql and "LEFT JOIN product_price" in sql:
            return [
                ("p1", "Роза Ред Наоми 60 см. 25 шт.", "СРЕЗКА", True, 0, {"Наличка": "9900"}),
                ("p2", "Гвоздика Кустовая", "СРЕЗКА", True, 5000, {"Наличка": "4000"}),
                ("p3", "Оазис флор", "АКСЕССУАРЫ", False, 0, None),
                ("p4", "Роза Фридом 50 см. 25 шт.", "СРЕЗКА", True, 7500, {"Наличка": "12000"}),
            ]
        if "assortment_id" in sql and "sales_by_product_day" in sql:
            return [("p1", 1_000_000)]  # Роза Ред Наоми продавалась, 10 000 ₽ выручки
        return []


def test_price_quality_report():
    text = report_prices.build_price_quality_report(_Conn())
    # Сводка: 2 из 4 без цены.
    assert "без закупочной цены: 2 (50%)" in text
    # Приоритет — продаваемая позиция без цены.
    assert "⚠️ Продаётся, но нет закупочной цены" in text
    assert "Роза Ред Наоми 60 см. 25 шт." in text
    # Непродаваемая без цены — в сводке по категориям.
    assert "АКСЕССУАРЫ: 1" in text
    # Аномалия «Наличка ниже закупочной».
    assert "Гвоздика Кустовая: закуп 50 ₽ · нал 40 ₽" in text
    # Позиции с ценой (p4) не в списке приоритетов.
    assert "Роза Фридом 50 см. 25 шт." not in text
