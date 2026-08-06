"""Регрессия A1: сумма в шапке отчёта = сумме в строке «ИТОГО».

Раньше переменная цикла позиций затирала сводную total_kop, и «ИТОГО»
показывало сумму последней позиции последнего документа. Тест строит отчёт
на фейковом conn и сверяет число в шапке «Сумма: X ₽» со строкой «ИТОГО ... X ₽».
Позиция последнего документа намеренно мелкая — при старом баге «ИТОГО»
показало бы её, а не сводную сумму.
"""
import re
from datetime import date

from hermes import report_loss, report_move


class _Cursor:
    def __init__(self, script):
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows = self.script(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, script):
        self.script = script

    def cursor(self):
        return _Cursor(self.script)


def _num(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))


def _header_and_total(text: str) -> tuple[int, int]:
    header = next(l for l in text.split("\n") if l.startswith("📋") and "Сумма" in l)
    total = next(l for l in text.split("\n") if l.startswith("═══ ИТОГО"))
    h = _num(header.split("Сумма:")[1].split("₽")[0])
    t = _num(total.split("·")[-1].split("₽")[0])
    return h, t


def test_loss_header_equals_total():
    # H2: отчёт из трёх блоков. Сверяем «🌸 Порча (розница): X ₽» с
    # «═══ ИТОГО ПОРЧА … X ₽» (инвариант A1 для блока порчи).
    def script(sql):
        if "COUNT(DISTINCT d.doc_id)" in sql and "GROUP BY" not in sql:   # _loss_sum
            return [(2, 513, 4_941_500)] if "NOT (d.store_name = ANY" in sql else [(0, 0, 0)]
        if "expense_item_name ILIKE" in sql:                # возвраты (cashflow)
            return [(0, 0)]
        if "GROUP BY d.store_name" in sql:                  # по складам
            return [("Склад", 2, 513, 4_941_500)]
        if "NULLIF(d.project_name" in sql:                  # по проектам
            return []
        if "i.product_name, SUM" in sql:                    # топ
            return [("Роза", 500, 4_940_000)]
        if "d.doc_id, d.moment" in sql:                     # документы (6 колонок)
            return [("doc1", None, date(2026, 8, 1), "Склад", "", "")]
        if "i.cost_kop, i.total_kop" in sql:                # позиции +pp.price_kop (5 колонок)
            return [("Лента", 5, 1900, 9500, 1900)]
        return []
    text = report_loss.build_loss_report(_Conn(script), date(2026, 8, 1), date(2026, 8, 5))
    header = next(l for l in text.split("\n") if l.startswith("🌸 Порча"))
    total = next(l for l in text.split("\n") if l.startswith("═══ ИТОГО ПОРЧА"))
    h = _num(header.split(":")[1].split("₽")[0])
    t = _num(total.split("·")[-1].split("₽")[0])
    assert h == t == 49415, (h, t, text)


def test_move_header_equals_total():
    def script(sql):
        # сводка: cnt, qty, покупная сумма, покрытая-МС, вся-МС
        if "SELECT COUNT(DISTINCT d.doc_id)" in sql:
            return [(12, 2473, 25_225_800, 25_225_800, 25_225_800)]
        if "store_from_name, d.store_to_name" in sql and "GROUP BY" in sql:
            return [("A", "B", 12, 2473, 25_225_800)]
        if "i.product_name, SUM(i.qty)" in sql:
            return [("Роза", 2000, 25_000_000)]
        if "d.doc_id, d.moment" in sql:               # +d.day (6 колонок)
            return [("doc1", None, date(2026, 8, 1), "A", "B", "")]
        if "i.cost_kop, i.total_kop" in sql:          # позиции +pp.price_kop (5 колонок)
            return [("Лента", 2, 9500, 19000, 9500)]
        return []
    text = report_move.build_move_report(_Conn(script), date(2026, 8, 1), date(2026, 8, 5))
    h, t = _header_and_total(text)
    assert h == t == 252258, (h, t, text)
