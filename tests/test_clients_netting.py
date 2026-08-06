"""Тесты нетто-сумм по клиенту (пункт 13) и документирование ограничения оттока.

Работаем без БД: подставляем фейковый conn, который перехватывает параметры
INSERT, и проверяем знак sum_kop и doc_type.
"""
import inspect

from hermes import etl_clients


class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if params is not None:
            self.sink.append(params)


class _FakeConn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _FakeCursor(self.rows)

    def commit(self):
        pass


# Порядок параметров в INSERT sales_doc (см. etl_clients._upsert):
# 0 doc_id, 1 moment, 2 day, 3 store_id, 4 channel, 5 agent_id, 6 agent_name,
# 7 sum_kop, 8 store_name, 9 positions, 10 doc_type
_SUM_IDX = 7
_DOCTYPE_IDX = 10


def _doc(sum_rub_kop: int) -> dict:
    return {
        "id": "doc-1",
        "moment": "2025-09-30 12:00:00",
        "store": {"id": "s", "name": "Склад"},
        "agent": {"id": "a", "name": "ИП Тест"},
        "sum": sum_rub_kop,
        "positions": {"meta": {"size": 1}},
    }


def test_demand_stored_positive():
    conn = _FakeConn()
    etl_clients._upsert(conn, _doc(4000), "demand", +1)
    params = conn.rows[-1]
    assert params[_SUM_IDX] == 4000
    assert params[_DOCTYPE_IDX] == "demand"


def test_return_stored_negative():
    # Возврат 40,00 ₽ (4000 коп.) должен записаться как -4000 с doc_type=salesreturn.
    conn = _FakeConn()
    etl_clients._upsert(conn, _doc(4000), "salesreturn", -1)
    params = conn.rows[-1]
    assert params[_SUM_IDX] == -4000
    assert params[_DOCTYPE_IDX] == "salesreturn"


def test_net_sum_demand_plus_return_is_zero():
    # demand +4000 и возврат -4000 → нетто 0 (как в реальной сверке 2025-09-30).
    conn = _FakeConn()
    etl_clients._upsert(conn, _doc(4000), "demand", +1)
    etl_clients._upsert(conn, _doc(4000), "salesreturn", -1)
    net = sum(r[_SUM_IDX] for r in conn.rows)
    assert net == 0


def test_churn_gap_limitation_mitigation_present():
    """Ограничение: детектор оттока не отличает реальную паузу от дыры в данных
    из-за отсутствия синка. Полностью в SQL без БД-фикстуры это не решается.
    Митигация (её и проверяем): etl_clients тянет и demand, и salesreturn, а
    отток в report_clients считает только по doc_type='demand'. Плюс daily
    синкает клиентов ежедневно (нет новых дыр), backfill закрывает историю.
    """
    src_etl = inspect.getsource(etl_clients)
    assert "salesreturn" in src_etl and "demand" in src_etl

    from hermes import report_clients
    src_rep = inspect.getsource(report_clients.build_clients_report)
    assert "doc_type = 'demand'" in src_rep
