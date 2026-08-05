"""Чистые расчётные функции — считают деньги. Покрыты тестами (tests/test_calc.py).

Здесь НЕТ обращений к сети или БД: на вход числа, на выходе числа. Это позволяет
проверять формулы на известных ответах и гарантировать воспроизводимость.

Деньги внутри системы — в КОПЕЙКАХ (целые). В рубли переводим только для показа.
"""
from __future__ import annotations


def kop_to_rub(kop: int) -> float:
    """Копейки → рубли."""
    return kop / 100


def gross_profit(revenue_kop: int, cost_kop: int) -> int:
    """Грязная прибыль = Выручка − Себестоимость (в копейках)."""
    return revenue_kop - cost_kop


def gross_margin_pct(revenue_kop: int, cost_kop: int) -> float:
    """% грязной прибыли = Грязная прибыль / Выручка × 100.

    Если выручка ноль — процент не определён, возвращаем 0.0 (а не деление на ноль).
    """
    if revenue_kop == 0:
        return 0.0
    return gross_profit(revenue_kop, cost_kop) / revenue_kop * 100


def avg_check(revenue_kop: int, checks: int) -> int:
    """Средний чек = Выручка / Количество чеков (в копейках, округление к ближайшему).

    Нет чеков — нет среднего, возвращаем 0.
    """
    if checks == 0:
        return 0
    return round(revenue_kop / checks)


def delta_pct(current: float, previous: float) -> float | None:
    """Изменение в % относительно прошлого периода.

    Прошлое значение ноль → сравнение не определено, возвращаем None (в отчёте покажем «—»).
    """
    if previous == 0:
        return None
    return (current - previous) / previous * 100


# --- Ценообразование (контроль прайса), правила владельца ---
COEFF_TRANSFER = 1.10   # Цена по переводу/карте = Наличка × 1,10
COEFF_RETAIL = 1.95     # Розничная цена = Наличка × 1,95


def expected_transfer_price(cash_price: float) -> float:
    return round(cash_price * COEFF_TRANSFER, 2)


def expected_retail_price(cash_price: float) -> float:
    return round(cash_price * COEFF_RETAIL, 2)


def purchase_price_at(conn, product_id: str, day: "date") -> "int | None":
    """Закупочная цена товара (копейки) из последней приёмки на дату day или раньше.

    Именно на дату операции: продажа 15.07 считается по цене приёмки от 10.07,
    даже если 20.07 пришла партия дороже. None — если приёмок до этой даты нет.
    """
    if not product_id:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_kop FROM purchase_price_asof
            WHERE product_id = %s AND priced_from <= %s
            ORDER BY priced_from DESC
            LIMIT 1
        """, (product_id, day))
        row = cur.fetchone()
    return int(row[0]) if row else None


def purchase_prices_asof(conn, day: "date", product_ids=None) -> "dict[str, int]":
    """Пакетно: для товаров — закупочная цена на дату day (один запрос, без N+1).

    DISTINCT ON берёт по каждому product_id строку с максимальным priced_from<=day.
    product_ids=None — по всем товарам. Возвращает {product_id: price_kop}.
    """
    with conn.cursor() as cur:
        if product_ids is not None:
            ids = [pid for pid in set(product_ids) if pid]
            if not ids:
                return {}
            cur.execute("""
                SELECT DISTINCT ON (product_id) product_id, price_kop
                FROM purchase_price_asof
                WHERE priced_from <= %s AND product_id = ANY(%s)
                ORDER BY product_id, priced_from DESC
            """, (day, ids))
        else:
            cur.execute("""
                SELECT DISTINCT ON (product_id) product_id, price_kop
                FROM purchase_price_asof
                WHERE priced_from <= %s
                ORDER BY product_id, priced_from DESC
            """, (day,))
        return {r[0]: int(r[1]) for r in cur.fetchall()}


def weekday_baseline(conn, day: "date", weeks: int = 8) -> "tuple[int, int] | None":
    """Средняя выручка по тому же дню недели за прошлые N недель.

    Returns (avg_revenue_kop, n_data_points) or None if no data.
    """
    from datetime import timedelta
    past_days = [day - timedelta(weeks=w) for w in range(1, weeks + 1)]
    placeholders = ", ".join(["%s"] * len(past_days))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT SUM(revenue_kop), COUNT(DISTINCT day)
            FROM sales_by_store_day
            WHERE day IN ({placeholders})
        """, past_days)
        row = cur.fetchone()
    if not row or not row[1]:
        return None
    total_kop, n_days = int(row[0] or 0), int(row[1])
    if n_days == 0:
        return None
    return total_kop // n_days, n_days
