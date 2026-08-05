"""Тесты фильтра группы товаров (_group_filter) — пункт 0.5.

В PDF-контексте секции «Остатки»/«Залежалые» всегда вызываются с группой
«СРЕЗКА», поэтому фильтр по «Ассортимент/%» применяется. Без группы (None)
фильтр не накладывается.
"""
from hermes.report_stock import _group_filter


def test_group_filter_none_no_filter():
    sql, params = _group_filter(None)
    assert sql == ""
    assert params == []


def test_group_filter_srezka_applies_assortment():
    # То, что реально уходит из PDF (folder_group='СРЕЗКА').
    sql, params = _group_filter("СРЕЗКА")
    assert "folder_path LIKE %s" in sql
    assert "SPLIT_PART(folder_path, '/', 2) = %s" in sql
    assert "Ассортимент/%" in params
    assert "СРЕЗКА" in params


def test_group_filter_param_count_matches_placeholders():
    sql, params = _group_filter("СРЕЗКА")
    assert sql.count("%s") == len(params)
