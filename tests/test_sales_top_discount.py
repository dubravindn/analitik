"""Регрессия: топ товаров использует фактическую закупку после скидки.

Общая сводка уже вычитает 7% для товаров ООО «Поставщик». Этот тест
не даёт топу вернуться к цене из карточки без скидки.
"""

from hermes import config
from hermes.report_sales_pdf import _PCOST


def test_top_product_cost_applies_supplier_discount():
    assert "spd.assortment_id = ANY(%s::text[])" in _PCOST
    assert f"THEN {config.SUPPLIER_DISCOUNT_MULTIPLIER}::numeric" in _PCOST
    assert round(10_000 * config.SUPPLIER_DISCOUNT_MULTIPLIER) == 9_300


def test_top_product_cost_keeps_fallback_without_extra_discount():
    assert "ELSE round(spd.revenue_kop * 0.6) END" in _PCOST
