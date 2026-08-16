"""PDF «Состояние на сегодня» — остатки, залежалые, резервы, прогноз по каждому складу."""
from __future__ import annotations

from datetime import date, timedelta
import logging

from hermes import config, pdf_kit as pk
from hermes.report_stock import STALE_SREZKA_MIN_DAYS as _STALE_DAYS  # утверждённый бизнес-порог = 5 дней
from hermes.moysklad import MoyskladClient


_BASE_STORE = "База Воровского 107/1"
_BASE_STORE_ID = "b4a45a8e-3d5e-11f0-0a80-0b690011c5d1"
_SREZKA_PREFIX = "Ассортимент/СРЕЗКА%"
_POTTED_EXCLUDE = "%/я.Горшечные%"
_BALLOONS_PATTERN = "%Шары гелиевые%"
_RELATED_PATTERN = "%Сопутствующие товары%"

log = logging.getLogger("hermes.report_current_state")


# ── Форматирование ─────────────────────────────────────────────────────────────

def _rub(kop) -> str:
    if kop is None:
        return "—"
    kop = int(kop or 0)
    rub = abs(kop) // 100
    s = "-" if kop < 0 else ""
    return s + f"{rub:,}".replace(",", " ")


def _qty(q) -> str:
    if q is None:
        return "—"
    f = float(q)
    return f"{int(f):,}".replace(",", " ") if f == int(f) else f"{f:,.1f}".replace(",", " ")


def _pct(num, denom) -> str:
    try:
        return f"{float(num) / float(denom) * 100:.0f}%"
    except (TypeError, ZeroDivisionError):
        return "—"


# ── SQL-помощники ──────────────────────────────────────────────────────────────

def _latest_day(conn) -> date:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(day) FROM stock_snapshot WHERE day <= CURRENT_DATE")
        r = cur.fetchone()
    return r[0] if r and r[0] else date.today()


def _stores_with_data(conn, day: date) -> list[str]:
    """В этом PDF показываем только центральный склад БАЗА."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT store_name FROM stock_snapshot WHERE day = %s",
            (day,),
        )
        found = {r[0] for r in cur.fetchall()}
    return [_BASE_STORE] if _BASE_STORE in found else []


def _stock_rows(conn, day: date, store_name: str) -> list[tuple]:
    """
    (product_id, product_name, group, stock_qty, reserve_qty,
     available, purchase_price_kop, purchase_value_kop,
     cash_price_kop, cash_sale_value_kop)
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ss.product_id,
                   ss.product_name,
                   COALESCE(NULLIF(regexp_replace(ss.folder_path, '^.*/', ''), ''), 'Другое') AS grp,
                   ss.stock_qty,
                   ss.reserve_qty,
                   ss.stock_qty - ss.reserve_qty                                       AS available,
                   COALESCE(pp.price_kop, ss.cost_price_kop, 0)                       AS cpu,
                   ss.stock_qty * COALESCE(pp.price_kop, ss.cost_price_kop, 0)        AS sv,
                   rp.cash_price_kop,
                   CASE WHEN rp.cash_price_kop > 0
                        THEN ss.stock_qty * rp.cash_price_kop
                        ELSE NULL END                                                  AS cash_sv
            FROM stock_snapshot ss
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = ss.product_id AND p.priced_from <= ss.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            LEFT JOIN LATERAL (
                SELECT NULLIF(pr.sale_prices ->> 'Наличка', '')::bigint AS cash_price_kop
                FROM product_price pr
                WHERE pr.product_id = ss.product_id AND pr.day <= ss.day
                ORDER BY pr.day DESC LIMIT 1
            ) rp ON true
            WHERE ss.day = %s AND ss.store_name = %s
              AND ss.folder_path LIKE %s
              AND ss.folder_path NOT ILIKE %s
            ORDER BY grp, ss.product_name
        """, (day, store_name, _SREZKA_PREFIX, _POTTED_EXCLUDE))
        return cur.fetchall()


def _zero_order_rows_db(conn, day: date, store_name: str) -> list[tuple]:
    """Гелиевые шары и сопутствующие без доступного остатка.

    Каталог product_dim берём за основу, поэтому в список попадают и товары,
    которых вообще нет в снимке остатков: для них остаток = 0.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pd.product_id,
                   pd.product_name,
                   CASE
                     WHEN pd.folder_path ILIKE %s THEN 'Гелиевые шары'
                     WHEN pd.folder_path ILIKE %s THEN 'Сопутствующие товары'
                     ELSE COALESCE(
                         NULLIF(regexp_replace(pd.folder_path, '^.*/', ''), ''),
                         'СРЕЗКА'
                     )
                   END AS grp,
                   COALESCE(ss.stock_qty, 0) AS physical,
                   COALESCE(ss.reserve_qty, 0) AS reserve,
                   COALESCE(ss.stock_qty, 0) - COALESCE(ss.reserve_qty, 0) AS available
            FROM product_dim pd
            LEFT JOIN stock_snapshot ss
              ON ss.product_id = pd.product_id
             AND ss.day = %s
             AND ss.store_name = %s
            WHERE (
                    pd.folder_path ILIKE %s
                    OR pd.folder_path ILIKE %s
                  )
              AND COALESCE(ss.stock_qty, 0) - COALESCE(ss.reserve_qty, 0) <= 0
            ORDER BY grp, pd.product_name
        """, (
            _BALLOONS_PATTERN, _RELATED_PATTERN, day, store_name,
            _BALLOONS_PATTERN, _RELATED_PATTERN,
        ))
        return cur.fetchall()


def _zero_order_rows(
    conn, token: str, day: date, store_name: str,
) -> list[tuple]:
    """Все нули/минусы из полного актуального каталога МойСклад.

    ``stock_snapshot`` намеренно хранит только позиции с положительным
    физическим остатком. Поэтому каталог нельзя строить только по этой таблице:
    новые и давно обнулённые товары там отсутствуют. Полный список берём из
    ``/entity/product``, а остаток БАЗЫ - из краткого текущего stock-отчёта.
    При временной недоступности API сохраняем работоспособность через DB-фолбэк.
    """
    try:
        client = MoyskladClient(token)
        catalog: dict[str, tuple[str, str]] = {}
        offset = 0
        while True:
            page = client._get("/entity/product", {
                "limit": 1000,
                "offset": offset,
                "order": "name,asc",
            })
            batch = page.get("rows", [])
            for product in batch:
                if product.get("archived"):
                    continue
                product_id = product.get("id") or ""
                name = product.get("name") or "Без названия"
                path = product.get("pathName") or ""
                is_balloons = "Шары гелиевые" in path
                is_related = "Сопутствующие товары" in path
                if not product_id or not (is_balloons or is_related):
                    continue
                if is_balloons:
                    group = "Гелиевые шары"
                elif is_related:
                    group = "Сопутствующие товары"
                catalog[product_id] = (name, group)
            size = page.get("meta", {}).get("size", 0)
            offset += len(batch)
            if offset >= size or not batch:
                break

        if not catalog:
            raise RuntimeError("полный каталог МойСклад вернул 0 целевых товаров")

        # Краткий текущий отчёт умеет возвращать нулевые строки. Три вызова
        # нужны потому, что API отдаёт только один тип количества за запрос.
        # Фильтр storeId гарантирует, что остатки других складов не смешиваются
        # с БАЗОЙ.
        current: dict[str, dict[str, float]] = {}
        for stock_type in ("stock", "reserve", "quantity"):
            data = client._get("/report/stock/all/current", {
                "stockType": stock_type,
                "include": "zeroLines",
                "filter": f"storeId={_BASE_STORE_ID}",
            })
            rows = data if isinstance(data, list) else data.get("rows", [])
            current[stock_type] = {
                row.get("assortmentId"): float(row.get(stock_type, 0) or 0)
                for row in rows
                if row.get("assortmentId")
            }

        result = []
        for product_id, (name, group) in catalog.items():
            physical = current["stock"].get(product_id, 0.0)
            reserve = current["reserve"].get(product_id, 0.0)
            # quantity в кратком отчёте = «Доступно» в интерфейсе МойСклад.
            available = current["quantity"].get(
                product_id, physical - reserve,
            )
            if available <= 0:
                result.append((
                    product_id, name, group, physical, reserve, available,
                ))
        result.sort(key=lambda row: (row[2].casefold(), row[1].casefold()))
        return result
    except Exception as exc:
        log.warning("Полный каталог для нулевых остатков недоступен: %s", exc)
        return _zero_order_rows_db(conn, day, store_name)


def _last_sales(conn) -> dict[str, date]:
    """product_id → дата последней продажи."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT assortment_id, MAX(day) FROM sales_by_product_day"
            " WHERE sell_qty > 0 GROUP BY assortment_id"
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _last_receipts(conn) -> dict[str, date]:
    """product_id → дата последней приёмки для центрального склада.

    До открытия БАЗЫ приёмки оформлялись на розницу Воровского,
    поэтому история собирается по обоим складам.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT si.product_id, MAX(sd.day)
            FROM supply_item si
            JOIN supply_doc sd ON sd.doc_id = si.doc_id
            WHERE sd.store_name = ANY(%s)
              AND si.qty > 0
            GROUP BY si.product_id
        """, ([_BASE_STORE, "Розница Воровского 107/1"],))
        return {r[0]: r[1] for r in cur.fetchall()}


def _recent_sales(conn, snap_day: date) -> dict[str, tuple]:
    """product_id → (sales_7d, sales_14d, sales_30d)"""
    d30 = snap_day - timedelta(days=30)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT assortment_id,
                   SUM(CASE WHEN day >= %s - 7  THEN sell_qty ELSE 0 END),
                   SUM(CASE WHEN day >= %s - 14 THEN sell_qty ELSE 0 END),
                   SUM(sell_qty)
            FROM sales_by_product_day
            WHERE sell_qty > 0 AND day >= %s
            GROUP BY assortment_id
        """, (snap_day, snap_day, d30))
        return {r[0]: (float(r[1] or 0), float(r[2] or 0), float(r[3] or 0))
                for r in cur.fetchall()}


def _forecast_by_store(conn, token: str, today: date) -> dict[str, list]:
    """NEW engine: результаты прогноза сгруппированные по store_name."""
    from hermes.calc_forecast import build_forecast_new, SREZKA_STORE_CONFIGS
    monday   = today - timedelta(days=today.weekday())
    next_mon = monday   + timedelta(days=7)
    next_sun = next_mon + timedelta(days=6)
    try:
        base_configs = [c for c in SREZKA_STORE_CONFIGS if c.store_id == _BASE_STORE_ID]
        results = build_forecast_new(
            conn, token, base_configs,
            cutoff_date=today,
            horizon_from=next_mon,
            horizon_to=next_sun,
        )
    except Exception:
        return {}
    by_store: dict[str, list] = {}
    for r in results:
        by_store.setdefault(r.store_name or r.store_id, []).append(r)
    return by_store


# ── Основная функция ──────────────────────────────────────────────────────────

def build_current_state_pdf(conn, token: str) -> bytes:
    """PDF по складу БАЗА: СРЕЗКА + нули по шарам/сопутствующим для заказа."""
    today    = date.today()
    snap_day = _latest_day(conn)
    period   = snap_day.strftime("%d.%m.%Y")
    gen_at   = config.msk_now().strftime("%d.%m.%Y %H:%M")

    stores        = _stores_with_data(conn, snap_day)
    ls_map        = _last_sales(conn)
    lr_map        = _last_receipts(conn)
    fc_map        = _forecast_by_store(conn, token, today)
    store_stock   = {sn: _stock_rows(conn, snap_day, sn) for sn in stores}
    zero_orders   = {
        sn: _zero_order_rows(conn, token, snap_day, sn) for sn in stores
    }

    # ── Глобальный KPI ─────────────────────────────────────────────────────────
    total_sku = total_physical = total_reserve = total_avail = 0
    total_zero = total_neg = stale_sku = 0

    for rows in store_stock.values():
        for pid, pname, grp, stock, res, avail, cpu, sv, cash_price, cash_sv in rows:
            total_sku      += 1
            total_physical += float(stock or 0)
            total_reserve  += float(res   or 0)
            total_avail    += float(avail or 0)
            if (avail or 0) == 0:
                total_zero += 1
            if (avail or 0) < 0:
                total_neg  += 1
            last = ls_map.get(pid)
            days_idle = (snap_day - last).days if last else 9999
            if days_idle > _STALE_DAYS and (stock or 0) > 0:
                stale_sku += 1

    all_fc    = fc_map.get(_BASE_STORE, [])
    order_sku = len({r.product_id for r in all_fc if (r.recommended_order_qty or 0) > 0})
    order_qty = int(sum(r.recommended_order_qty or 0 for r in all_fc))

    # ── PDF ────────────────────────────────────────────────────────────────────
    pdf = pk.HermesPDF(section_title="Склад БАЗА", period=period)

    # ─── Страница 1: управленческая сводка ───────────────────────────────────
    pdf.add_page()
    pk.cover(pdf, f"СКЛАД БАЗА  ·  СРЕЗКА  ·  {gen_at}")

    pk.kpi_row(pdf, [
        ("Позиций СРЕЗКА", _qty(total_sku),      ""),
        ("Физ. остаток",    _qty(total_physical), "шт."),
        ("В резерве",       _qty(total_reserve),  "шт."),
        ("Доступно",        _qty(total_avail),    "шт."),
    ])
    pdf.ln(3)
    pk.kpi_row(pdf, [
        ("Нет доступного", _qty(total_zero + total_neg), "поз."),
        ("Нули и минусы", _qty(sum(len(v) for v in zero_orders.values())), "поз."),
        ("Залежалые",       _qty(stale_sku),      "поз."),
        ("К заказу",        f"{order_sku} / {order_qty} шт.", ""),
    ])
    pdf.ln(5)

    # Сводная таблица по складам
    pk.section_header(pdf, "Сводка по складу БАЗА")
    summary_rows = []
    for sn in stores:
        rows = store_stock.get(sn, [])
        sn_phys = sum(float(r[3] or 0) for r in rows)
        sn_res  = sum(float(r[4] or 0) for r in rows)
        sn_fc   = fc_map.get(sn, [])
        sn_ord  = int(sum(r.recommended_order_qty or 0 for r in sn_fc
                          if (r.recommended_order_qty or 0) > 0))
        summary_rows.append([
            sn[:45],
            str(len(rows)),
            _qty(sn_phys),
            _qty(sn_res),
            str(sn_ord) if sn_ord else "—",
        ])
    pk.table(
        pdf,
        headers=["Склад", "Поз.", "Физ. остаток", "В резерве", "К заказу, шт."],
        rows=summary_rows,
        col_widths=[90, 16, 28, 26, 28],
        aligns=["L", "R", "R", "R", "R"],
    )

    # ─── По каждому складу ────────────────────────────────────────────────────
    for sn in stores:
        rows = store_stock.get(sn, [])
        if not rows:
            continue

        # === ОСТАТКИ ===
        pdf.add_page()
        pk.cover(pdf, f"ОСТАТКИ СРЕЗКА  ·  {sn}")
        pk.section_header(pdf, f"Снимок: {period}")

        s_phys  = sum(float(r[3] or 0) for r in rows)
        s_res   = sum(float(r[4] or 0) for r in rows)
        s_avail = sum(float(r[5] or 0) for r in rows)
        s_val   = sum(float(r[7] or 0) for r in rows)
        s_cash  = sum(float(r[9] or 0) for r in rows)
        s_neg   = sum(1 for r in rows if (r[5] or 0) < 0)
        s_zero  = sum(1 for r in rows if (r[3] or 0) == 0)

        pk.kpi_row(pdf, [
            ("Позиций",          str(len(rows)),     ""),
            ("Физ. остаток",     _qty(s_phys),       "шт."),
            ("Резерв",           _qty(s_res),        "шт."),
            ("Сумма закупки",    _rub(int(s_val)),   "₽"),
            ("Сумма продажи (нал.)", _rub(int(s_cash)), "₽"),
        ])
        pdf.ln(2)

        if s_neg > 0:
            pk.callout(
                pdf,
                f"Доступный остаток меньше нуля: {s_neg} позиций. "
                "Это значит, что резерв больше фактического остатка.",
                kind="warn",
            )
        if s_zero > 0:
            pk.callout(pdf, f"Нулевой физический остаток: {s_zero} позиций", kind="warn")
        pdf.ln(1)

        # Таблица: только позиции с ненулевым физическим остатком
        pos_rows = [r for r in rows if (r[3] or 0) > 0]
        if pos_rows:
            pk.section_header(pdf, "Позиции с остатком")
            pk.table(
                pdf,
                headers=["Товар", "Группа", "Физ.", "Рез.", "Дост.",
                         "Цена закуп.", "Сумма закуп.", "Сумма налич."],
                rows=[[
                    r[1][:31], r[2][:12],
                    _qty(r[3]), _qty(r[4]), _qty(r[5]),
                    _rub(int(r[6] or 0)), _rub(int(r[7] or 0)), _rub(r[9]),
                ] for r in pos_rows],
                col_widths=[50, 22, 12, 12, 12, 20, 22, 24],
                aligns=["L", "L", "R", "R", "R", "R", "R", "R"],
                font_size=6.8,
                max_rows=80,
                overflow_note="… ещё {n} позиций не показано",
            )

        # Отдельно: нулевые позиции из каталога, чтобы их можно было заказать.
        order_zero = zero_orders.get(sn, [])
        if order_zero:
            pdf.add_page()
            pk.cover(pdf, "НУЛИ И МИНУСЫ")
            pk.section_header(
                pdf, "К заказу: гелиевые шары и сопутствующие товары",
            )
            balloons = [r for r in order_zero if r[2] == "Гелиевые шары"]
            related = [r for r in order_zero if r[2] == "Сопутствующие товары"]

            def _split(rows):
                return (
                    sum(1 for r in rows if float(r[5] or 0) == 0),
                    sum(1 for r in rows if float(r[5] or 0) < 0),
                )

            bl0, bln = _split(balloons)
            rl0, rln = _split(related)
            pk.callout(
                pdf,
                f"Только склад БАЗА · Доступно = 0 или меньше · всего {len(order_zero)} поз.\n"
                f"Гелиевые шары: {bl0} нулевых + {bln} отриц. · "
                f"Сопутствующие: {rl0} нулевых + {rln} отриц.",
                kind="warn",
            )
            pdf.ln(2)
            pk.table(
                pdf,
                headers=["Группа", "Товар", "Физ.", "Резерв", "Доступно"],
                rows=[[
                    r[2][:24], r[1][:48], _qty(r[3]), _qty(r[4]), _qty(r[5]),
                ] for r in order_zero],
                col_widths=[36, 86, 14, 16, 22],
                aligns=["L", "L", "R", "R", "R"],
                font_size=7.8,
            )

        # === ЗАЛЕЖАЛЫЕ ===
        pdf.add_page()
        pk.cover(pdf, f"ЗАЛЕЖАЛЫЕ  ·  {sn}")

        stale: list[tuple] = []
        for pid, pname, grp, stock, res, avail, cpu, sv, cash_price, cash_sv in rows:
            if not stock or float(stock) <= 0:
                continue
            last = ls_map.get(pid)
            days_idle = (snap_day - last).days if last else 9999
            if days_idle <= _STALE_DAYS:
                continue
            last_receipt = lr_map.get(pid)
            days_in_stock = ((snap_day - last_receipt).days
                             if last_receipt else None)
            stale.append((pname, float(stock), last_receipt, days_in_stock,
                          last, days_idle, float(sv or 0)))

        stale.sort(key=lambda x: (x[3] is None, -(x[3] or 0), x[0]))

        if not stale:
            pk.callout(pdf, "Залежалых позиций не обнаружено.", kind="ok")
        else:
            stale_kop = int(sum(r[6] for r in stale))
            pk.kpi_row(pdf, [
                ("Залежалых позиций", str(len(stale)), ""),
                ("Заморожено",   _rub(stale_kop), "₽"),
                (f"Порог: >{_STALE_DAYS} дн.", "без продаж", ""),
            ])
            pdf.ln(2)
            pk.callout(
                pdf,
                "«На складе, дн.» считается от последней приёмки. "
                "История приёмок объединена по БАЗЕ и рознице Воровского. "
                "«Без продаж, дн.» — отдельный показатель.",
                kind="info",
            )
            pdf.ln(1)
            pk.table(
                pdf,
                headers=["Товар", "Ост.", "Приёмка", "На скл.",
                         "Прод.", "Без прод.", "Сумма закуп."],
                rows=[[
                    r[0][:32], _qty(r[1]),
                    r[2].strftime("%d.%m.%y") if r[2] else "нет приёмки",
                    str(r[3]) if r[3] is not None else "—",
                    r[4].strftime("%d.%m.%y") if r[4] else "нет истор.",
                    str(r[5]) if r[5] < 999 else "?",
                    _rub(int(r[6])),
                ] for r in stale],
                col_widths=[62, 12, 21, 15, 21, 17, 26],
                aligns=["L", "R", "C", "R", "C", "R", "R"],
                font_size=7.0,
                max_rows=60,
                overflow_note="… ещё {n} позиций не показано",
            )

        # === РЕЗЕРВЫ ===
        res_rows = [r for r in rows if (r[4] or 0) > 0]
        if res_rows:
            pdf.add_page()
            pk.cover(pdf, f"РЕЗЕРВЫ  ·  {sn}")

            total_res_kop  = int(sum(float(r[4] or 0) * float(r[6] or 0) for r in res_rows))
            over_stock_cnt = sum(1 for r in res_rows if float(r[4] or 0) > float(r[3] or 0))
            neg_avail_cnt  = sum(1 for r in res_rows if float(r[5] or 0) < 0)

            pk.kpi_row(pdf, [
                ("Позиций в резерве", str(len(res_rows)),   ""),
                ("Резерв > остатка", str(over_stock_cnt),  "поз."),
                ("Доступно < 0",      str(neg_avail_cnt),   "поз."),
                ("Стоим. резерва",   _rub(total_res_kop),  "₽"),
            ])
            pdf.ln(2)

            pk.table(
                pdf,
                headers=["Товар", "Физ.", "Резерв", "Доступно", "Доля", "Стоим. рез., ₽"],
                rows=[[
                    r[1][:52],
                    _qty(r[3]), _qty(r[4]), _qty(r[5]),
                    _pct(float(r[4] or 0), float(r[3] or 1)),
                    _rub(int(float(r[4] or 0) * float(r[6] or 0))),
                ] for r in sorted(res_rows, key=lambda x: -(float(x[4] or 0)))],
                col_widths=[80, 18, 18, 18, 16, 28],
                aligns=["L", "R", "R", "R", "R", "R"],
                font_size=8.0,
                max_rows=60,
                overflow_note="… ещё {n} позиций не показано",
            )

        # === ПРОГНОЗ ===
        fc_list  = fc_map.get(sn, [])
        from hermes.report_forecast_pdf import (
            _query_sales_period, _query_year_ago_receipts, forecast_group_name,
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_id, product_name, folder_path
                FROM product_dim
                WHERE is_srezka = TRUE
                  AND folder_path NOT ILIKE %s
            """, (_POTTED_EXCLUDE,))
            product_meta = {r[0]: (r[1], r[2] or "") for r in cur.fetchall()}

        fc_order = [
            r for r in fc_list
            if r.product_id in product_meta and (r.recommended_order_qty or 0) > 0
        ]
        fc_order.sort(key=lambda r: (
            forecast_group_name(product_meta.get(r.product_id, ("", ""))[1]),
            r.product_name,
        ))
        if fc_order:
            today_monday = today - timedelta(days=today.weekday())
            next_mon = today_monday + timedelta(days=7)
            next_sun = today_monday + timedelta(days=13)
            previous_from = today_monday - timedelta(days=7)
            previous_to = today_monday - timedelta(days=1)
            current_from = today_monday
            current_to = today - timedelta(days=1)
            year_from = next_mon - timedelta(weeks=52)
            year_to = next_sun - timedelta(weeks=52)
            pids = [r.product_id for r in fc_order]
            year_receipts = _query_year_ago_receipts(conn, pids, year_from, year_to)
            previous_sales = _query_sales_period(conn, pids, previous_from, previous_to)
            current_sales = _query_sales_period(conn, pids, current_from, current_to)

            pdf.add_page()
            pk.cover(pdf, f"ПРОГНОЗ ЗАКУПКИ  ·  {sn}")
            pk.section_header(
                pdf,
                f"Горизонт: {next_mon:%d.%m}-{next_sun:%d.%m.%Y}  ·  Новый расчёт",
            )
            pk.callout(
                pdf,
                "КАК СЧИТАЕТСЯ К ЗАКАЗУ:\n"
                "1. Заказы клиентов = неотгруженное количество из заказов со статусом «Под заказ» и проектом «Ближайшая поставка».\n"
                "2. Статистический прогноз = средние продажи БАЗЫ за последние 28 календарных дней x 7 дней прогноза.\n"
                "3. Статистическая добавка = max(0, статистический прогноз - заказы клиентов).\n"
                "4. Ожидаемый спрос = заказы клиентов + статистическая добавка.\n"
                "5. Доступно = физический остаток - резерв. Резерв второй раз не вычитается.\n"
                "6. До округления = max(0, ожидаемый спрос - доступно).\n"
                "7. К заказу = «до округления», округлённое вверх до размера упаковки.\n"
                "Подтверждённые будущие поступления пока не загружены и в расчёте равны 0.",
                kind="info",
            )
            pk.callout(
                pdf,
                f"Приёмка год назад: {year_from:%d.%m.%Y}-{year_to:%d.%m.%Y} "
                f"(БАЗА + розница Воровского). Прошлая неделя: "
                f"{previous_from:%d.%m.%Y}-{previous_to:%d.%m.%Y}. Эта неделя: "
                f"{current_from:%d.%m.%Y}-{current_to:%d.%m.%Y}. Остаток: {snap_day:%d.%m.%Y}.",
                kind="info",
            )
            pk.section_header(pdf, "История приёмок и продаж")
            pk.table(
                pdf,
                headers=["Группа", "Название", "Приёмка г/н",
                         "Прошлая нед.", "Эта нед."],
                rows=[[
                    forecast_group_name(product_meta.get(r.product_id, ("", ""))[1])[:14],
                    r.product_name[:50],
                    _qty(year_receipts.get(r.product_id, 0)),
                    _qty(previous_sales.get(r.product_id, 0)),
                    _qty(current_sales.get(r.product_id, 0)),
                ] for r in fc_order],
                col_widths=[24, 80, 28, 28, 28],
                aligns=["L", "L", "R", "R", "R"],
                font_size=7.5,
                max_rows=500,
            )

            pdf.add_page()
            pk.cover(pdf, "ИЗ ЧЕГО СЛОЖИЛСЯ СПРОС")
            pk.callout(
                pdf,
                "Ожидаемый спрос = заказы клиентов + статистическая добавка. "
                "Добавка нужна, только если статистический прогноз выше уже оформленных заказов.",
                kind="info",
            )
            pk.section_header(pdf, "Расчёт спроса по каждой позиции")
            pk.table(
                pdf,
                headers=["Группа", "Название", "Заказы",
                         "Стат. прогноз", "Стат. добавка", "Ожид. спрос"],
                rows=[[
                    forecast_group_name(product_meta.get(r.product_id, ("", ""))[1])[:14],
                    r.product_name[:46],
                    _qty(r.known_order_demand),
                    _qty(r.statistical_demand),
                    _qty(r.statistical_residual),
                    _qty(r.expected_demand),
                ] for r in fc_order],
                col_widths=[20, 74, 24, 23, 23, 24],
                aligns=["L", "L", "R", "R", "R", "R"],
                font_size=6.8,
                max_rows=500,
            )

            pdf.add_page()
            pk.cover(pdf, "КАК ПОЛУЧЕНО «К ЗАКАЗУ»")
            pk.callout(
                pdf,
                "Доступно = физический остаток - резерв. "
                "До округления = max(0, ожидаемый спрос - доступно). "
                "К заказу = «до округления», округлённое вверх до упаковки.",
                kind="info",
            )
            pk.section_header(pdf, "Остаток, резерв и итоговый заказ")
            pk.table(
                pdf,
                headers=["Группа", "Название", "Физ.", "Резерв", "Доступно",
                         "Ожид. спрос", "До округл.", "Упак.", "К заказу"],
                rows=[[
                    forecast_group_name(product_meta.get(r.product_id, ("", ""))[1])[:14],
                    r.product_name[:30],
                    _qty(r.stock_all),
                    _qty(r.reserve_qty),
                    _qty(r.available_stock),
                    _qty(r.expected_demand),
                    _qty(r.raw_order_qty),
                    _qty(r.pack_size),
                    _qty(r.recommended_order_qty or 0),
                ] for r in fc_order],
                col_widths=[20, 44, 16, 16, 18, 20, 18, 14, 22],
                aligns=["L", "L", "R", "R", "R", "R", "R", "R", "R"],
                font_size=6.5,
                max_rows=500,
            )
        else:
            pk.callout(pdf, f"К заказу по складу «{sn}» нет позиций.", kind="ok")

    return bytes(pdf.output())
