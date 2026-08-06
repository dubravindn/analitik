"""I5: расходы деревом «статья → документы по убыванию суммы».

Проверяем, что статьи идут по убыванию суммы, документы внутри статьи — тоже
по убыванию, а «по складам» — только компактная сводка (не полное дерево).
"""
from datetime import date, datetime

from hermes import report_cashflow


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
        if "SUM(amount_kop), COUNT(*)" in sql and "GROUP BY" not in sql:
            return [(500000, 3)]
        if "no_proj" in sql:
            return [("Киров, Ленина 102А", 350000, 2, False),
                    ("Слободской, Советская 64", 150000, 1, False)]
        if "ORDER BY amount_kop DESC" in sql:
            return [
                (datetime(2026, 8, 1, 10, 0), "cashout", "Аренда ООО", "", "Аренда",
                 "Киров, Ленина 102А", 300000),
                (datetime(2026, 8, 2, 12, 0), "paymentout", "Поставщик", "", "Закупка",
                 "Слободской, Советская 64", 150000),
                (datetime(2026, 8, 3, 9, 0), "cashout", "Мелочь", "", "Аренда",
                 "Киров, Ленина 102А", 50000),
            ]
        return []


def test_expense_tree_sorted():
    text = report_cashflow.build_expenses_report(_Conn(), date(2026, 8, 1), date(2026, 8, 5))
    # Сводка по складам присутствует и компактна.
    assert "── По складам (сводка) ──" in text
    # Дерево по статьям: «Аренда» (350к) раньше «Закупка» (150к).
    assert text.index("▸ Аренда:") < text.index("▸ Закупка:")
    # Внутри «Аренда» документ 3000 раньше 500 (по убыванию суммы).
    a = text.index("▸ Аренда:")
    z = text.index("▸ Закупка:")
    block = text[a:z]
    assert block.index("10:00 [Касса] 3 000 ₽") < block.index("09:00 [Касса] 500 ₽")
    # Заголовок статьи с суммой и числом операций.
    assert "▸ Аренда: 3 500 ₽ (2 опер.)" in text
