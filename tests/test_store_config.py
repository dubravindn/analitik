"""Справочник складов должен быть единым для ETL, меню и PDF."""

from hermes import config
from hermes.report_stock import _STORE_ORDER
from hermes.report_stock_pdf import _RETAIL_STORES


def test_fabrika_is_registered_as_restaurant():
    fabrika = next(store for store in config.STORES if store["name"] == "ФАБРИКА")

    assert fabrika["id"] == "670a34d1-925d-11f1-0a80-01b0000e74fc"
    assert config.STORE_CHANNELS[fabrika["id"]] == "ресторан"
    assert "ФАБРИКА" in _STORE_ORDER


def test_only_retail_stores_enter_stale_stock_section():
    assert "Киров, Ленина 102А" in _RETAIL_STORES
    assert "ФАБРИКА" not in _RETAIL_STORES
    assert "СОБРАНИЕ" not in _RETAIL_STORES
