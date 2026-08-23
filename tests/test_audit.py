"""Аудит документов: общий контроль и точечные сигналы по заказам/отгрузкам."""
from datetime import date

from hermes import report_audit


class _FakeClient:
    """Пустой общий аудит с фиксацией запрошенных endpoint."""

    def __init__(self, deleted_rows=None, doc_rows=None):
        self.deleted_rows = deleted_rows or []
        self.doc_rows = doc_rows or []
        self.paths = []

    def _get(self, path, params):
        self.paths.append(path)
        if path == "/audit":
            return {"meta": {"size": 0}, "rows": []}
        return {"meta": {"size": 0}, "rows": []}


def test_audit_empty_message():
    text = report_audit.build_audit_report(
        _FakeClient(), date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "важных изменений и удалений документов нет" in text
    assert "УДАЛЁННЫЕ" not in text and "ИЗМЕНЁННЫЕ" not in text


def test_receipts_all_payments_and_orders_remain_but_loss_is_not_requested():
    class Client(_FakeClient):
        def _get(self, path, params):
            self.paths.append(path)
            if path == "/audit":
                return {"meta": {"size": 0}, "rows": []}
            if path.startswith("/entity/loss"):
                raise AssertionError("Документы списания запрашивать нельзя")
            if path == "/entity/supply/deleted":
                return {"rows": [{
                    "deletedMoment": "2026-08-05 10:00:00",
                    "sum": 1_250_000,
                    "store": {"name": "База Воровского 107/1"},
                    "owner": {"name": "Иванов"},
                    "name": "ПР-00123",
                }]}
            financial_docs = {
                "/entity/paymentin": ("ВП-001", "Входящие платежи"),
                "/entity/paymentout": ("ИП-002", "Исходящие платежи"),
                "/entity/cashin": ("ПО-003", "Приходные ордера"),
                "/entity/cashout": ("РО-004", "Расходные ордера"),
            }
            if path in financial_docs:
                number, _label = financial_docs[path]
                return {"meta": {"size": 1}, "rows": [{
                    "moment": "2026-08-05 09:00:00",
                    "updated": "2026-08-05 09:05:00",
                    "sum": 34_000_000,
                    "project": {"name": "База Воровского 107/1"},
                    "owner": {"name": "Петров"},
                    "name": number,
                }]}
            return {"meta": {"size": 0}, "rows": []}

    client = Client()
    text = report_audit.build_audit_report(
        client, date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "Приёмки № ПР-00123" in text
    assert "Входящие платежи № ВП-001" in text
    assert "Исходящие платежи № ИП-002" in text
    assert "Приходные ордера № ПО-003" in text
    assert "Расходные ордера № РО-004" in text
    assert any(path.startswith("/entity/supply") for path in client.paths)
    assert any(path.startswith("/entity/paymentin") for path in client.paths)
    assert any(path.startswith("/entity/paymentout") for path in client.paths)
    assert any(path.startswith("/entity/cashin") for path in client.paths)
    assert any(path.startswith("/entity/cashout") for path in client.paths)
    assert not any(path.startswith("/entity/loss") for path in client.paths)
    assert "Списания" not in text


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


def test_multiday_audit_reads_later_pages_without_losing_early_event(monkeypatch):
    class Client:
        def __init__(self):
            self.offsets = []

        def _get(self, path, params):
            if path == "/audit":
                flt = params["filter"]
                offset = params["offset"]
                if "moment>=2026-08-20 00:00:00" in flt and "customerorder" in flt:
                    self.offsets.append(offset)
                    rows = (
                        [{"id": "noise1"}, {"id": "noise2"}]
                        if offset == 0 else [{"id": "wanted"}]
                    )
                    return {"meta": {"size": 3}, "rows": rows}
                return {"meta": {"size": 0}, "rows": []}
            if path == "/audit/wanted/events":
                return {"rows": [{
                    "eventType": "update", "entityType": "customerorder",
                    "moment": "2026-08-20 12:00:00", "uid": "manager",
                    "name": "13619", "diff": {"moment": {
                        "oldValue": "2026-08-20 09:00:00",
                        "newValue": "2026-08-21 09:00:00",
                    }},
                }]}
            return {"rows": []}

    monkeypatch.setattr(report_audit, "_AUDIT_PAGE_SIZE", 2)
    client = Client()
    text = report_audit.build_audit_report(
        client, date(2026, 8, 16), date(2026, 8, 21),
    )
    assert client.offsets == [0, 2]
    assert "Заказ покупателя № 13619" in text
    assert "Дата заказа: 20.08.2026 09:00 → 21.08.2026 09:00" in text


def test_technical_sync_events_are_skipped_before_detail_requests(monkeypatch):
    class Client:
        def __init__(self):
            self.event_paths = []

        def _get(self, path, params):
            if path == "/audit":
                if "entityType=demand" in params["filter"]:
                    return {"meta": {"size": 0}, "rows": []}
                return {"meta": {"size": 2}, "rows": [
                    {
                        "id": "robot-event",
                        "uid": "robots.nirguna@dubravin_flowers",
                    },
                    {"id": "human-event", "uid": "manager@example"},
                ]}
            self.event_paths.append(path)
            if path == "/audit/robot-event/events":
                raise AssertionError("Техническое событие раскрывать нельзя")
            if path == "/audit/human-event/events":
                return {"rows": [{
                    "eventType": "update", "entityType": "customerorder",
                    "moment": "2026-08-20 12:00:00", "uid": "manager@example",
                    "name": "14111", "diff": {"moment": {
                        "oldValue": "2026-08-20 09:00:00",
                        "newValue": "2026-08-21 09:00:00",
                    }},
                }]}
            return {"rows": [], "meta": {"size": 0}}

    monkeypatch.setattr(report_audit, "_AUDIT_REQUEST_START_INTERVAL_SECONDS", 0)
    client = Client()
    text = report_audit.build_audit_report(
        client, date(2026, 8, 20), date(2026, 8, 20),
    )
    audit_event_paths = [p for p in client.event_paths if p.startswith("/audit/")]
    assert audit_event_paths == ["/audit/human-event/events"]
    assert "Заказ покупателя № 14111" in text


def test_completed_audit_days_are_reused_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(report_audit, "_AUDIT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(report_audit, "_AUDIT_REQUEST_START_INTERVAL_SECONDS", 0)

    class Client:
        def __init__(self):
            self.summary_calls = 0

        def _get(self, path, params):
            if path == "/audit":
                self.summary_calls += 1
                if "entityType=demand" in params["filter"]:
                    return {"meta": {"size": 0}, "rows": []}
                return {"meta": {"size": 1}, "rows": [{
                    "id": "cached-event", "uid": "manager@example",
                }]}
            if path == "/audit/cached-event/events":
                return {"rows": [{
                    "eventType": "update", "entityType": "customerorder",
                    "moment": "2026-08-20 12:00:00", "uid": "manager@example",
                    "name": "14222", "diff": {"moment": {
                        "oldValue": "2026-08-20 09:00:00",
                        "newValue": "2026-08-21 09:00:00",
                    }},
                }]}
            return {"rows": [], "meta": {"size": 0}}

    client = Client()
    first = report_audit._load_sales_document_audit(
        client, date(2026, 8, 20), date(2026, 8, 20),
    )
    first_calls = client.summary_calls
    second = report_audit._load_sales_document_audit(
        client, date(2026, 8, 20), date(2026, 8, 20),
    )

    assert first == second
    assert first_calls == 2
    assert client.summary_calls == first_calls
