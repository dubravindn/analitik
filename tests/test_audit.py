"""Аудит документов: общий контроль и точечные сигналы по заказам/отгрузкам."""
from datetime import date

from hermes import report_audit


class _FakeClient:
    """Отдаёт строки только для списаний, чтобы не задваивать общий аудит."""

    def __init__(self, deleted_rows=None, doc_rows=None):
        self.deleted_rows = deleted_rows or []
        self.doc_rows = doc_rows or []

    def _get(self, path, params):
        if not path.startswith("/entity/loss"):
            return {"rows": []}
        return {"rows": self.deleted_rows if path.endswith("/deleted") else self.doc_rows}


def test_audit_empty_message():
    text = report_audit.build_audit_report(
        _FakeClient(), date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "изменённых и удалённых документов нет" in text
    assert "УДАЛЁННЫЕ" not in text and "ИЗМЕНЁННЫЕ" not in text


def test_audit_lists_deleted_and_modified():
    deleted = [{
        "deletedMoment": "2026-08-05 10:00:00", "sum": 1_250_000,
        "store": {"name": "Розница Воровского 107/1"},
        "owner": {"name": "Иванов"}, "name": "00123",
    }]
    docs = [
        {
            "moment": "2026-08-05 09:00:00", "updated": "2026-08-05 09:05:00",
            "sum": 34_000_000, "store": {"name": "База Воровского 107/1"},
            "owner": {"name": "Петров"},
        },
        {
            "moment": "2026-08-05 09:10:00", "updated": "2026-08-05 09:10:03",
            "sum": 500, "store": {"name": "Ленина"}, "owner": {"name": "Сидоров"},
        },
    ]
    text = report_audit.build_audit_report(
        _FakeClient(deleted, docs), date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "🗑 УДАЛЁННЫЕ (1):" in text
    assert "Списания № 00123 · 05.08.2026 · Розница Воровского 107/1 · 12 500 ₽ · Иванов" in text
    assert "✏️ ИЗМЕНЁННЫЕ (1):" in text
    assert "340 000 ₽ · Петров" in text
    assert "Сидоров" not in text


def _position(name, product_id, price, discount=0, quantity=1):
    return {
        "assortment": {
            "name": name,
            "meta": {
                "href": (
                    "https://api.moysklad.ru/api/remap/1.2/"
                    f"entity/product/{product_id}"
                ),
            },
        },
        "quantity": quantity,
        "price": price,
        "discount": discount,
    }


class _SalesDocumentAuditClient:
    def _get(self, path, params):
        if path == "/audit":
            if "entityType=demand" in params["filter"]:
                row = {
                    "id": "d1", "eventType": "update", "entityType": "demand",
                    "moment": "2026-08-05 13:00:00",
                }
            else:
                row = {
                    "id": "a1", "eventType": "update", "entityType": "customerorder",
                    "moment": "2026-08-05 12:00:00",
                }
            return {"meta": {"size": 1}, "rows": [row]}
        if path == "/audit/a1/events":
            return {"rows": [{
                "eventType": "update", "entityType": "customerorder",
                "moment": "2026-08-05 12:00:00", "uid": "manager@example",
                "name": "13999",
                "diff": {
                    "state": {
                        "oldValue": {"name": "Под заказ"},
                        "newValue": {"name": "Выполнен"},
                    },
                    "deliveryPlannedMoment": {
                        "oldValue": "2026-08-06 10:00:00",
                        "newValue": "2026-08-07 11:00:00",
                    },
                    "positions": [
                        {
                            "oldValue": _position("Роза Эксплорер", "p1", 120),
                            "newValue": _position("Роза Эксплорер", "p1", 80),
                        },
                        {
                            "oldValue": {
                                **_position("Гвоздика", "p2", 100, quantity=1),
                                "reserve": 1,
                            },
                            "newValue": {
                                **_position("Гвоздика", "p2", 100, quantity=5),
                                "reserve": 5,
                            },
                        },
                        {"newValue": _position("Добавленный товар", "p3", 50)},
                        {"oldValue": _position("Удалённый товар", "p4", 50)},
                    ],
                },
            }]}
        if path == "/audit/d1/events":
            return {"rows": [{
                "eventType": "update", "entityType": "demand",
                "moment": "2026-08-05 13:00:00", "uid": "owner@example",
                "name": "14000",
                "diff": {
                    "moment": {
                        "oldValue": "2026-07-20 10:00:00",
                        "newValue": "2026-07-19 10:00:00",
                    },
                    "positions": [{
                        "oldValue": _position("Хризантема", "p2", 100),
                        "newValue": _position("Хризантема", "p2", 70),
                    }],
                    "applicable": {"oldValue": True, "newValue": False},
                },
            }]}
        if path == "/entity/product/p1":
            return {"salePrices": [{
                "priceType": {"name": "Наличка"}, "value": 9_000,
            }]}
        if path == "/entity/product/p2":
            return {"salePrices": [{
                "priceType": {"name": "Наличка"}, "value": 8_000,
            }]}
        if path == "/entity/product/p3":
            return {"salePrices": [{
                "priceType": {"name": "Наличка"},
                "value": 9_999_999_999_900,
            }]}
        return {"rows": []}


def test_orders_and_shipments_show_only_dates_and_prices_below_cash():
    text = report_audit.build_audit_report(
        _SalesDocumentAuditClient(), date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "ЗАКАЗЫ И ОТГРУЗКИ — ВАЖНЫЕ ИЗМЕНЕНИЯ (2)" in text
    assert "Заказ покупателя № 13999" in text
    assert "Плановая дата доставки: 06.08.2026 10:00 → 07.08.2026 11:00" in text
    assert "«Роза Эксплорер»: цена снижена 120 → 80 ₽" in text
    assert "«Наличка» 90 ₽" in text

    # Отгрузка из июля попала в отчёт по дате изменения 05.08.
    assert "Отгрузка № 14000 · изменён 05.08.2026 13:00" in text
    assert "Дата отгрузки: 20.07.2026 10:00 → 19.07.2026 10:00" in text
    assert "«Хризантема»: цена снижена 100 → 70 ₽" in text

    # Статус, количество, резерв и добавление/удаление скрыты.
    assert "Статус" not in text
    assert "количество" not in text
    assert "резерв" not in text
    assert "Добавленный товар" not in text
    assert "Удалённый товар" not in text


def test_discount_change_uses_actual_price_but_price_increase_is_hidden():
    client = _SalesDocumentAuditClient()
    discounted = report_audit._position_diff_lines([{
        "oldValue": _position("Роза", "p1", 100, discount=0),
        "newValue": _position("Роза", "p1", 100, discount=20),
    }], client)
    assert "цена после скидки 20% снижена 100 → 80 ₽" in discounted[0]

    increased_but_below_cash = report_audit._position_diff_lines([{
        "oldValue": _position("Роза", "p1", 70),
        "newValue": _position("Роза", "p1", 80),
    }], client)
    assert increased_but_below_cash == []

    sentinel_cash_price = report_audit._position_diff_lines([{
        "oldValue": _position("Служебная позиция", "p3", 100),
        "newValue": _position("Служебная позиция", "p3", 50),
    }], client)
    assert sentinel_cash_price == []


def test_mass_same_date_change_is_grouped_but_keeps_order_numbers():
    rows = [
        {
            "event_type": "update", "entity_type": "customerorder",
            "moment": "2026-08-05 08:07:01", "uid": "manager@example",
            "number": number,
            "diff": {"moment": {
                "oldValue": "2026-08-05 09:00:00",
                "newValue": "2026-08-06 09:00:00",
            }},
        }
        for number in ("12188", "12211", "12854")
    ]
    rendered = "\n".join(report_audit._render_customer_order_audit(rows, 3))
    assert "Массовое изменение: 3 заказов" in rendered
    assert "Номера: 12188, 12211, 12854" in rendered
    assert "Дата заказа: 05.08.2026 09:00 → 06.08.2026 09:00" in rendered
