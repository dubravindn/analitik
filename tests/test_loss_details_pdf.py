"""Полная детализация списаний в управленческом PDF."""
from datetime import date, datetime

from hermes.report_sales_pdf import _get_loss_details_by_store


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.conn.sql = sql
        self.conn.params = params

    def fetchall(self):
        return [
            (
                "База Воровского 107/1", date(2026, 8, 20),
                datetime(2026, 8, 20, 9, 0), "doc-base",
                "Роза длинное полное наименование", 25, 9_300, 232_500,
            ),
            (
                "Розница Воровского 107/1", date(2026, 8, 20),
                datetime(2026, 8, 20, 10, 0), "doc-retail",
                "Хризантема", 5, 7_000, 35_000,
            ),
        ]


class _Conn:
    def cursor(self):
        return _Cursor(self)


def test_loss_details_include_every_store_and_have_no_row_limit():
    conn = _Conn()
    rows = _get_loss_details_by_store(
        conn, date(2026, 8, 20), date(2026, 8, 21), {"supplier-product"},
    )

    assert set(rows) == {
        "База Воровского 107/1", "Розница Воровского 107/1",
    }
    assert rows["База Воровского 107/1"][0][3] == (
        "Роза длинное полное наименование"
    )
    # Единственный LIMIT относится к выбору последней закупочной цены;
    # итоговый список позиций не обрезается.
    assert conn.sql.upper().count("LIMIT") == 1
    assert "NOT (d.store_name" not in conn.sql
    assert conn.params == [
        ["supplier-product"], ["supplier-product"],
        date(2026, 8, 20), date(2026, 8, 21),
    ]
