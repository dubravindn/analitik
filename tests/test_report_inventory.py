from datetime import date

from hermes import report_inventory


def _row(kind, store, day, positions, raw, value, doc_id):
    return {
        "kind": kind, "store": store, "day": day, "positions": positions,
        "raw_kop": raw, "value_kop": value, "doc_id": doc_id,
        "moment": None, "description": "", "project": "",
        "qty": positions, "missing_prices": 0,
        "quantity_anomalies": [],
    }


def test_inventory_sessions_exclude_ordinary_retail_spoilage(monkeypatch):
    day = date(2026, 8, 17)
    retail = "Розница Воровского 107/1"
    rows = [
        _row("enter", retail, day, 40, 0, 50_000, "E1"),
        _row("loss", retail, day, 22, 0, 70_000, "L-INV"),
        _row("loss", retail, day, 10, 146_100, 146_100, "L-SPOIL"),
    ]
    monkeypatch.setattr(report_inventory, "_doc_rows", lambda *_: rows)
    sessions = report_inventory.load_inventory_sessions(object(), day, day)
    assert len(sessions) == 1
    assert [doc["doc_id"] for doc in sessions[0]["loss_docs"]] == ["L-INV"]
    assert sessions[0]["net_kop"] == -20_000


def test_inventory_report_explains_both_sides_and_missing_points(monkeypatch):
    day = date(2026, 8, 17)
    retail = "Розница Воровского 107/1"
    rows = [
        _row("enter", retail, day, 40, 0, 50_000, "E1"),
        _row("loss", retail, day, 22, 0, 70_000, "L1"),
    ]
    monkeypatch.setattr(report_inventory, "_doc_rows", lambda *_: rows)
    text = report_inventory.build_inventory_report(object(), day, day)
    assert "Списание = фактически меньше учётного остатка" in text
    assert "Списание № L1" in text
    assert "Оприходование № E1" in text
    assert "Недостача по учёту" in text
    assert "За выбранный период инвентаризация не найдена" in text
    assert "гипотезы, а не установленная причина" in text


def test_inventory_marks_absurd_position_quantity_as_unreliable(monkeypatch):
    day = date(2026, 8, 17)
    row = _row("loss", "Киров, Ленина 102А", day, 119, 0, 3_200_000_000, "L1")
    row["quantity_anomalies"] = [{"product": "Лента", "qty": 499_972}]
    monkeypatch.setattr(report_inventory, "_doc_rows", lambda *_: [row])
    text = report_inventory.build_inventory_report(object(), day, day)
    assert "Критическая аномалия ввода: «Лента» — 499 972 ед." in text
    assert "Денежный итог инвентаризации считать недостоверным" in text
