"""J4: прогноз из пяти блоков в фиксированном порядке.

Воспроизводим пример владельца: Роза Фридом 50 — прошлая неделя 180, заказано
200 (3 заказа), свободный остаток 63 → к заказу 317, округление до упаковки 25
→ 325 ед. (13 упак.). Плюс проверяем блок 5: перезапас (Гвоздика) и залежалая
СРЕЗКА без продаж (Калла).
"""
from datetime import date, timedelta

from hermes import report_forecast
from hermes import config

_ROSE = "Роза Фридом Эквадор 50 см. 25 шт."
_CARN = "Гвоздика Красная Эквадор 20 шт."
_CALLA = "Калла Белая"


class _FakeClient:
    def _get(self, path, params=None):
        if path == "/entity/project":
            return {"rows": [{"name": "Ближайшая поставка",
                              "meta": {"href": "PROJ"}}]}
        if path == "/entity/customerorder/metadata":
            return {"states": [{"name": "Под заказ", "meta": {"href": "ST"}}]}
        if path == "/entity/customerorder":
            return {"rows": [{"id": "o1"}, {"id": "o2"}, {"id": "o3"}],
                    "meta": {"size": 3}}
        if path.startswith("/entity/customerorder/") and path.endswith("/positions"):
            oid = path.split("/")[3]
            q = {"o1": 100.0, "o2": 60.0, "o3": 40.0}[oid]
            return {"rows": [{"assortment": {"id": "p-rose", "name": _ROSE},
                              "quantity": q}],
                    "meta": {"size": 1}}
        return {"rows": [], "meta": {"size": 0}}


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows = self.conn.script(sql, params, self.conn)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self):
        self.counters = {}

    def cursor(self):
        return _Cursor(self)

    def script(self, sql, params, conn):
        if "available_qty >= %s" in sql:                         # sentinel-лог
            return []
        if "FROM product_dim" in sql and "product_name, folder_path" in sql:
            return [("p-rose", _ROSE, "Ассортимент/СРЕЗКА"),
                    ("p-carn", _CARN, "Ассортимент/СРЕЗКА"),
                    ("p-calla", _CALLA, "Ассортимент/СРЕЗКА")]
        if "sales_by_product_day WHERE day BETWEEN" in sql and "GROUP BY assortment_id" in sql:
            n = conn.counters.get("sales", 0)
            conn.counters["sales"] = n + 1
            if n == 0:                                           # прошлая неделя
                return [("p-rose", 180), ("p-carn", 105)]
            return [("p-rose", 210)]                             # год назад
        if "stock_snapshot" in sql and "GROUP BY ss.product_id" in sql:
            return [("p-rose", 63), ("p-carn", 460), ("p-calla", 40)]
        if "assortment_id, MAX(day)" in sql:                     # последняя продажа
            today = config.msk_today()
            return [("p-rose", today - timedelta(days=1)),
                    ("p-carn", today - timedelta(days=2)),
                    ("p-calla", today - timedelta(days=12))]
        if "MIN(day)" in sql:                                    # старт истории
            return [(date(2000, 1, 1),)]
        if "FROM supply_doc" in sql:                             # фургоны
            return []
        if "FROM holiday" in sql:
            return []
        return []


def _report():
    return report_forecast.build_forecast_report(_FakeClient(), _Conn())


def test_five_blocks_in_order():
    text = _report()
    i1 = text.index("═══ 1. ЧТО ЗАКАЗАНО ═══")
    i2 = text.index("═══ 2. ЗАКАЗАНО МИНУС ОСТАТОК ═══")
    i3 = text.index("═══ 3. ПРОГНОЗ СПРОСА ═══")
    i4 = text.index("4. ИТОГО К ЗАКАЗУ НА ФУРГОН")
    i5 = text.index("═══ 5. ЧТО БРАТЬ НЕ НАДО ═══")
    assert i1 < i2 < i3 < i4 < i5


def test_block1_orders():
    text = _report()
    assert f"• {_ROSE}: 200 ед. (3 заказ.)" in text
    assert "Итого: 1 поз. · 200 ед." in text


def test_block2_shortfall():
    text = _report()
    assert (f"• {_ROSE}: заказано 200 · свободный остаток 63 → докупить 137"
            in text)


def test_block3_year_pct():
    text = _report()
    # 180 vs 210 → −14%
    assert f"• {_ROSE}: прошлая неделя 180 ед. · год назад 210 ед. (−14%)" in text


def test_block4_to_order_rounds_to_package():
    text = _report()
    assert (f"• {_ROSE}: 180 + 200 − 63 = 317 → К ЗАКАЗУ 325 ед. (13 упак.)"
            in text)


def test_block5_overstock_and_stale():
    text = _report()
    assert (f"• {_CARN}: остаток 460 · продажи/нед 105 — запас на 4+ недель"
            in text)
    assert f"• {_CALLA}: 40 ед. без продаж 12 дн. — не брать, продавать остаток" in text
