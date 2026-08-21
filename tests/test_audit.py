"""J3: аудит изменений — плоский список с суммой и «кто», внятная пустота.

Проверяем три инварианта:
1. когда изменённых и удалённых нет — одна понятная строка, не пустота;
2. удалённые и изменённые выводятся списком «тип · дата · склад · сумма · кто»;
3. правка, где updated почти совпадает с moment (создание), НЕ считается изменением.
"""
from datetime import date

from hermes import report_audit


class _FakeClient:
    """Отдаёт заданные строки только для одного типа документа (loss),
    чтобы не задваивать по шести эндпоинтам."""
    def __init__(self, deleted_rows=None, doc_rows=None):
        self.deleted_rows = deleted_rows or []
        self.doc_rows = doc_rows or []

    def _get(self, path, params):
        if not path.startswith("/entity/loss"):
            return {"rows": []}
        return {"rows": self.deleted_rows if path.endswith("/deleted") else self.doc_rows}


def test_audit_empty_message():
    text = report_audit.build_audit_report(_FakeClient(), date(2026, 8, 5), date(2026, 8, 5))
    assert "изменённых и удалённых документов нет" in text
    # никаких пустых блоков-заголовков
    assert "УДАЛЁННЫЕ" not in text and "ИЗМЕНЁННЫЕ" not in text


def test_audit_lists_deleted_and_modified():
    deleted = [{
        "deletedMoment": "2026-08-05 10:00:00", "sum": 1_250_000,
        "store": {"name": "Розница Воровского 107/1"},
        "owner": {"name": "Иванов"}, "name": "00123",
    }]
    docs = [
        {  # изменён: правка через 5 минут после создания
            "moment": "2026-08-05 09:00:00", "updated": "2026-08-05 09:05:00",
            "sum": 34_000_000, "store": {"name": "База Воровского 107/1"},
            "owner": {"name": "Петров"},
        },
        {  # НЕ изменён: updated почти совпадает с moment (создание)
            "moment": "2026-08-05 09:10:00", "updated": "2026-08-05 09:10:03",
            "sum": 500, "store": {"name": "Ленина"}, "owner": {"name": "Сидоров"},
        },
    ]
    text = report_audit.build_audit_report(
        _FakeClient(deleted, docs), date(2026, 8, 5), date(2026, 8, 5)
    )
    assert "🗑 УДАЛЁННЫЕ (1):" in text
    assert "Списания № 00123 · 05.08.2026 · Розница Воровского 107/1 · 12 500 ₽ · Иванов" in text
    assert "✏️ ИЗМЕНЁННЫЕ (1):" in text          # только один, не два
    assert "340 000 ₽ · Петров" in text
    assert "Сидоров" not in text                   # мгновенное создание не считается правкой


class _CustomerOrderAuditClient:
    def _get(self, path, params):
        if path == "/audit":
            event_type = "delete" if "eventType=delete" in params["filter"] else "update"
            row = (
                {
                    "id": "a2", "eventType": "delete",
                    "entityType": "customerorder",
                    "moment": "2026-08-05 13:00:00",
                }
                if event_type == "delete"
                else {
                    "id": "a1", "eventType": "update",
                    "entityType": "customerorder",
                    "moment": "2026-08-05 12:00:00",
                }
            )
            return {
                "meta": {"size": 1},
                "rows": [row],
            }
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
                    "positions": [{
                        "oldValue": {
                            "assortment": {"name": "Роза Эксплорер"},
                            "quantity": 25.0, "reserve": 25.0,
                        },
                        "newValue": {
                            "assortment": {"name": "Роза Эксплорер"},
                            "quantity": 50.0, "reserve": 50.0,
                        },
                    }],
                },
            }]}
        if path == "/audit/a2/events":
            return {"rows": [{
                "eventType": "delete", "entityType": "customerorder",
                "moment": "2026-08-05 13:00:00", "uid": "owner@example",
                "name": "14000", "diff": {},
            }]}
        return {"rows": []}


def test_customer_orders_use_real_audit_diff():
    text = report_audit.build_audit_report(
        _CustomerOrderAuditClient(), date(2026, 8, 5), date(2026, 8, 5),
    )
    assert "ЗАКАЗЫ ПОКУПАТЕЛЕЙ — ИСТОРИЯ (2)" in text
    assert "Заказ покупателя № 13999" in text
    assert "кто: manager@example" in text
    assert "Статус: Под заказ → Выполнен" in text
    assert "Плановая дата доставки: 06.08.2026 10:00 → 07.08.2026 11:00" in text
    assert "«Роза Эксплорер»: количество 25 → 50, резерв 25 → 50" in text
    assert "Заказ покупателя № 14000 · удалён" in text


def test_mass_same_change_is_grouped_but_keeps_order_numbers():
    rows = [
        {
            "event_type": "update", "moment": "2026-08-05 08:07:01",
            "uid": "manager@example", "number": number,
            "diff": {"project": {
                "oldValue": {"name": "1 сентября"},
                "newValue": {"name": "Ближайшая поставка"},
            }},
        }
        for number in ("12188", "12211", "12854")
    ]
    rendered = "\n".join(report_audit._render_customer_order_audit(rows, 3))
    assert "Массовое изменение: 3 заказов" in rendered
    assert "Номера: 12188, 12211, 12854" in rendered
    assert "Проект: 1 сентября → Ближайшая поставка" in rendered
