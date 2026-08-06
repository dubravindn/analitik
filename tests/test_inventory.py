"""J2: инвентаризация Базы — отдельный блок со списаниями И оприходованиями.

Проверяем, что блок показывает обе стороны (списано/оприходовано), итог
корректировки со знаком и позиции с «−»/«+». Отчёт строится по складу «База»,
поэтому после блока инвентаризации функция сразу возвращается (детализацию
порчи-розницы не трогаем — упрощает фейковый conn).
"""
from datetime import date

from hermes import report_loss

_BASE = "База Воровского 107/1"


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


def _script(sql):
    if "COUNT(DISTINCT d.doc_id)" in sql and "GROUP BY" not in sql:
        if "enter_doc" in sql:                       # сводка оприходований
            return [(1, 800, 15_000_000)]
        if "NOT (d.store_name = ANY" in sql:         # порча (розница) — по Базе нет
            return [(0, 0, 0)]
        return [(1, 2313, 21_845_400)]               # корректировки Базы (списано)
    if "expense_item_name ILIKE" in sql:             # возвраты
        return [(0, 0)]
    if "FROM loss_doc d" in sql and "ORDER BY d.moment" in sql:
        return [("L1", None, date(2026, 7, 20), "инвентаризация")]
    if "FROM enter_doc d" in sql and "ORDER BY d.moment" in sql:
        return [("E1", None, date(2026, 7, 22), "инвентаризация")]
    if "FROM loss_item i" in sql:                    # позиции списания (5 колонок)
        return [("Роза Эксплорер Эквадор 60 см. 25 шт.", 1363, 10800, 1363 * 10800, 10800)]
    if "FROM enter_item i" in sql:                   # позиции оприходования (5 колонок)
        return [("Роза Фридом Эквадор 50 см. 25 шт.", 800, 7500, 800 * 7500, 7500)]
    return []


def test_inventory_block_two_sided():
    text = report_loss.build_loss_report(
        _Conn(_script), date(2026, 7, 1), date(2026, 7, 31), _BASE
    )
    assert "ИНВЕНТАРИЗАЦИЯ (База)" in text
    # Обе стороны в сводной строке.
    assert "Списано: 2 313 ед. · 218 454 ₽ · Оприходовано: 800 ед. · 150 000 ₽" in text
    # Итог корректировки: 218 454 − 150 000 = 68 454, недостача → знак «−».
    assert "Итог корректировки: −68 454 ₽" in text
    # Документы обеих сторон и знаки в позициях.
    assert "· Списание · инвентаризация" in text
    assert "· Оприходование · инвентаризация" in text
    assert "1 363 ед. × 108 ₽/ед. = −147 204 ₽" in text
    assert "800 ед. × 75 ₽/ед. = +60 000 ₽" in text
