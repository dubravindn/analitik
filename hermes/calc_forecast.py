"""
Логика прогноза закупки СРЕЗКА.

Цепочка: приёмка → продажи → списания → остаток → прогноз → заказ.
Все веса и пороги вынесены в константы — менять здесь, не в PDF-генераторе.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes.forecast_models import ForecastResult

# ── Веса спроса ──────────────────────────────────────────────────────────────
PREV_WEEK_WEIGHT  = 0.65
YEAR_AGO_WEIGHT   = 0.35

# ── Страховой запас (по sell-through прошлой партии) ─────────────────────────
SAFETY_HIGH   = 0.10   # sell-through ≥ 85%
SAFETY_MEDIUM = 0.05   # sell-through 60–84%
SAFETY_NONE   = 0.00   # sell-through < 60%

# ── Пороги sell-through ───────────────────────────────────────────────────────
ST_FULL    = 0.95   # Продано полностью
ST_GOOD    = 0.80   # Продано хорошо
ST_PARTIAL = 0.30   # Продано частично
ST_WEAK    = 0.01   # Слабая продажа

# ── Скидка ООО Поставщик ─────────────────────────────────────────────────────
SUPPLIER_DISCOUNT = 0.07

# ── Защита от завышения: max target = max(prev, year_ago) × 1.15 ─────────────
DEMAND_CAP_MULT = 1.15

# Sentinel: санитарный порог остатка (для фильтрации мусора)
_SENTINEL_QTY = 9999

# ── Feature flag ──────────────────────────────────────────────────────────────
# "old" — production bot (Telegram/PDF) использует старый движок; "new" — NEW engine.
# Переключать только после прохождения всех 8 критериев production-readiness.
FORECAST_ENGINE: str = "old"

# ── Парсинг размера упаковки из названия товара ───────────────────────────────
_PACK_RE = re.compile(r'(\d{1,3})\s*шт\.?', re.IGNORECASE)


def parse_pack_size(name: str) -> tuple[int, str]:
    """
    Извлекает размер упаковки из названия.
    Returns (pack_size, source) где source='name_parsed'|'default'.
    """
    m = _PACK_RE.search(name)
    if m:
        n = int(m.group(1))
        if 2 <= n <= 500:
            return n, "name_parsed"
    return 1, "default"


def round_up_to_pack(qty: float, pack_size: int) -> tuple[int, int]:
    """
    Округляет qty вверх до целого числа упаковок.
    Returns (order_units, order_packs).
    """
    if qty <= 0 or pack_size <= 0:
        return 0, 0
    packs = math.ceil(qty / pack_size)
    return packs * pack_size, packs


def sell_through_label(st: float, obs_complete: bool, received: float) -> str:
    if not obs_complete:
        return "Неполное окно"
    if received == 0:
        return "Нет приёмки"
    if st >= ST_FULL:
        return "Продано полностью"
    if st >= ST_GOOD:
        return "Продано хорошо"
    if st >= ST_PARTIAL:
        return "Продано частично"
    if st >= ST_WEAK:
        return "Слабая продажа"
    return "Не продано"


@dataclass
class ForecastRow:
    product_id:   str
    product_name: str
    folder_path:  str
    subgroup:     str

    # Упаковка
    pack_size:        int  = 1
    pack_size_source: str  = "default"  # 'name_parsed' | 'default'

    # Приёмки
    prev_received:     float = 0.0
    year_ago_received: float = 0.0

    # Продажи (нетто) за период
    prev_demand:     float = 0.0
    year_ago_demand: float = 0.0

    # Скорректированный спрос (с учётом дефицита)
    prev_adj_demand:     float = 0.0
    year_ago_adj_demand: float = 0.0

    # Sell-through (продано из партии за 7 дней / принято)
    prev_sell_through:     float = 0.0
    year_ago_sell_through: float = 0.0

    # Окно наблюдения завершено?
    prev_obs_complete:     bool = True
    year_ago_obs_complete: bool = True

    # Списания за прошлую неделю
    prev_written_off: float = 0.0

    # Дефицит (прошлая неделя)
    prev_days_in_stock:    int   = 7
    prev_days_total:       int   = 7
    prev_deficit_detected: bool  = False

    # Текущий остаток
    available_stock: float = 0.0
    stock_all:       float = 0.0
    reserve_qty:     float = 0.0

    # Прогноз
    base_demand:    float = 0.0
    safety_rate:    float = 0.0
    target_stock:   float = 0.0
    raw_order:      float = 0.0
    order_units:    int   = 0
    order_packs:    int   = 0

    # Стоимость (копейки)
    buy_price_kop:           float = 0.0
    is_supplier_discount:    bool  = False
    cost_before_discount_kop: float = 0.0
    discount_kop:            float = 0.0
    cost_after_discount_kop: float = 0.0

    # Квалификация
    confidence: str = "low"    # 'high' | 'medium' | 'low'
    reason:     str = ""
    category:   str = "manual" # 'order' | 'skip' | 'manual'

    # Вспомогательные флаги
    has_prev:     bool = False
    has_year_ago: bool = False

    @property
    def prev_sell_through_label(self) -> str:
        return sell_through_label(
            self.prev_sell_through, self.prev_obs_complete, self.prev_received
        )

    @property
    def year_ago_sell_through_label(self) -> str:
        return sell_through_label(
            self.year_ago_sell_through, self.year_ago_obs_complete, self.year_ago_received
        )


# ── Главная функция ───────────────────────────────────────────────────────────

def build_forecast(
    conn,
    target_from: date,
    target_to:   date,
    report_date: date | None = None,
) -> list[ForecastRow]:
    """
    Строит прогноз закупки для целевой недели.

    Периоды:
      prev_week:      target - 7 дней (ровно предыдущая неделя)
      year_ago_week:  target - 52 недели (сохраняется день недели)
    """
    if report_date is None:
        report_date = date.today()

    days = max((target_to - target_from).days + 1, 1)
    prev_from     = target_from - timedelta(days=days)
    prev_to       = target_to   - timedelta(days=days)
    ya_from       = target_from - timedelta(weeks=52)
    ya_to         = target_to   - timedelta(weeks=52)

    prev_obs_complete = report_date > prev_to + timedelta(days=7)
    ya_obs_complete   = report_date > ya_to   + timedelta(days=7)

    # ── 1. Справочник СРЕЗКА ─────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id, product_name, folder_path
            FROM product_dim
            WHERE is_srezka = TRUE
        """)
        srezka = {r[0]: (r[1], r[2] or "") for r in cur.fetchall()}
    if not srezka:
        return []
    pids = list(srezka)

    # ── 2. Приёмки от ООО Поставщик ──────────────────────────────────────────
    def _receipts(d_from: date, d_to: date) -> dict[str, float]:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT si.product_id, SUM(si.qty)
                FROM supply_item si
                JOIN supply_doc sd ON sd.doc_id = si.doc_id
                WHERE sd.day BETWEEN %s AND %s
                  AND sd.agent_name ILIKE '%%поставщик%%'
                  AND si.product_id = ANY(%s)
                GROUP BY si.product_id
            """, [d_from, d_to, pids])
            return {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    prev_receipts = _receipts(prev_from, prev_to)
    ya_receipts   = _receipts(ya_from,   ya_to)

    # ── 3. Продажи за период + 7 дней (для sell-through) ─────────────────────
    def _sales(d_from: date, d_to: date) -> dict[str, float]:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT assortment_id, SUM(sell_qty)
                FROM sales_by_product_day
                WHERE day BETWEEN %s AND %s
                  AND sell_qty > 0
                  AND assortment_id = ANY(%s)
                GROUP BY assortment_id
            """, [d_from, d_to, pids])
            return {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    prev_sales_period  = _sales(prev_from, prev_to)
    prev_sales_7d      = _sales(prev_from, prev_to + timedelta(days=7))
    ya_sales_period    = _sales(ya_from,   ya_to)
    ya_sales_7d        = _sales(ya_from,   ya_to + timedelta(days=7))

    # ── 4. Списания прошлой недели ────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT li.product_id, SUM(li.qty)
            FROM loss_item li
            JOIN loss_doc ld ON ld.doc_id = li.doc_id
            WHERE ld.day BETWEEN %s AND %s
              AND li.product_id = ANY(%s)
            GROUP BY li.product_id
        """, [prev_from, prev_to, pids])
        prev_losses: dict[str, float] = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

    # ── 5. Наличие товара по дням (для дефицита) ──────────────────────────────
    # total_days берём из Python-переменной days, чтобы sentinel-фильтр
    # не занижал знаменатель при экстраполяции продаж.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT product_id,
                   COUNT(DISTINCT day) FILTER (
                       WHERE available_qty > 0 AND available_qty < %s
                   ) AS days_in_stock
            FROM stock_snapshot
            WHERE day BETWEEN %s AND %s
              AND is_srezka = TRUE
            GROUP BY product_id
        """, [_SENTINEL_QTY, prev_from, prev_to])
        stock_days: dict[str, int] = {
            r[0]: int(r[1]) for r in cur.fetchall()
        }

    # ── 6. Текущий остаток (последний снимок ≤ target_from) ──────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(day) FROM stock_snapshot
            WHERE day <= %s AND is_srezka = TRUE
        """, [target_from])
        snap_day = cur.fetchone()[0]

    curr_stock: dict[str, tuple[float, float, float]] = {}  # pid → (stock, reserve, available)
    if snap_day:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT product_id,
                       SUM(stock_qty), SUM(reserve_qty), SUM(available_qty)
                FROM stock_snapshot
                WHERE day = %s AND is_srezka = TRUE AND available_qty < %s
                GROUP BY product_id
            """, [snap_day, _SENTINEL_QTY])
            for pid, stk, rsv, avl in cur.fetchall():
                curr_stock[pid] = (float(stk or 0), float(rsv or 0), float(avl or 0))

    # ── 7. Закупочные цены ────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (product_id)
                   product_id, buy_price_kop
            FROM product_price
            WHERE buy_price_kop > 0
              AND product_id = ANY(%s)
            ORDER BY product_id, day DESC
        """, [pids])
        prices: dict[str, float] = {r[0]: float(r[1]) for r in cur.fetchall()}

    # ── 8. ID товаров, по которым действует скидка ────────────────────────────
    from . import calc
    discount_pids: set[str] = set(calc.discount_product_ids(conn))

    # ── 9. Сборка ForecastRow ────────────────────────────────────────────────
    rows: list[ForecastRow] = []

    for pid, (pname, fpath) in srezka.items():
        pack_size, pack_src = parse_pack_size(pname)
        parts = (fpath or "").split("/")
        subgroup = parts[2] if len(parts) >= 3 else (parts[-1] if parts else "Другое")

        # Приёмки
        prev_recv = prev_receipts.get(pid, 0.0)
        ya_recv   = ya_receipts.get(pid, 0.0)

        # Продажи
        prev_dem = prev_sales_period.get(pid, 0.0)
        ya_dem   = ya_sales_period.get(pid, 0.0)

        # Sell-through (7 дней после начала периода)
        prev_st_qty = prev_sales_7d.get(pid, 0.0)
        ya_st_qty   = ya_sales_7d.get(pid, 0.0)
        prev_st  = (prev_st_qty / prev_recv) if prev_recv > 0 else 0.0
        ya_st    = (ya_st_qty  / ya_recv)   if ya_recv   > 0 else 0.0
        prev_st  = min(prev_st, 1.0)
        ya_st    = min(ya_st,   1.0)

        # Списания
        prev_wo = prev_losses.get(pid, 0.0)

        # Дефицит: если товар был доступен меньше чем весь период
        in_days = stock_days.get(pid, days)
        deficit = 0 < in_days < days and prev_dem > 0
        if deficit:
            prev_adj = prev_dem / in_days * days
        else:
            prev_adj = prev_dem

        # Текущий остаток
        stk_all, stk_rsv, stk_avl = curr_stock.get(pid, (0.0, 0.0, 0.0))

        # Флаги данных
        has_prev = prev_dem > 0 or prev_recv > 0
        has_ya   = ya_dem   > 0 or ya_recv   > 0

        # Базовый спрос (взвешенный)
        eff_prev = prev_adj
        eff_ya   = ya_dem
        if has_prev and has_ya:
            base = eff_prev * PREV_WEEK_WEIGHT + eff_ya * YEAR_AGO_WEIGHT
        elif has_prev:
            base = eff_prev
        elif has_ya:
            base = eff_ya
        else:
            base = 0.0

        # Защита от завышения
        max_base = max(eff_prev, eff_ya, 0.0) * DEMAND_CAP_MULT
        if max_base > 0:
            base = min(base, max_base)

        # Страховой запас (по sell-through прошлой партии)
        effective_st = prev_st if prev_obs_complete else ya_st
        if effective_st >= 0.85:
            safety = SAFETY_HIGH
        elif effective_st >= 0.60:
            safety = SAFETY_MEDIUM
        else:
            safety = SAFETY_NONE

        # Если sell-through < 60% и есть стоки — не добавлять запас
        if prev_recv > 0 and effective_st < 0.60 and stk_avl > 0:
            safety = SAFETY_NONE

        # target_stock и raw_order
        target = base * (1 + safety)
        raw = max(0.0, target - stk_avl)

        # Если продажи=0 и есть остаток — не заказывать (кроме сезонного)
        if prev_dem == 0 and ya_dem == 0:
            raw = 0.0

        # Округление до упаковки
        order_units, order_packs = round_up_to_pack(raw, pack_size)

        # Стоимость
        buy_kop = prices.get(pid, 0.0)
        is_disc = pid in discount_pids
        cost_before = buy_kop * order_units
        disc_kop = cost_before * SUPPLIER_DISCOUNT if is_disc else 0.0
        cost_after = cost_before - disc_kop

        # Достоверность
        if has_prev and has_ya and prev_obs_complete and pack_src == "name_parsed":
            conf = "high"
        elif has_prev or has_ya:
            conf = "medium"
        else:
            conf = "low"

        # Если pack_size=default и нет данных — всегда low
        if pack_src == "default" and not (has_prev or has_ya):
            conf = "low"

        # Причина рекомендации
        if not has_prev and not has_ya:
            reason = "Нет данных для прогноза"
        elif prev_dem == 0 and ya_dem == 0 and (prev_recv > 0 or ya_recv > 0):
            reason = "Не было продаж после приёмки"
        elif raw <= 0 and stk_avl > 0:
            reason = "Текущий остаток покрывает спрос"
        elif effective_st > 0 and effective_st < ST_PARTIAL and prev_recv > 0:
            reason = "Большой непроданный остаток"
        elif deficit:
            reason = "Продажи ограничены отсутствием товара"
        elif not has_ya:
            reason = "Нет данных год назад"
        elif not prev_obs_complete:
            reason = "Неполное окно наблюдения"
        elif has_prev and has_ya and effective_st >= ST_GOOD:
            if eff_prev > eff_ya * 1.2:
                reason = "Рост относительно прошлого года"
            else:
                reason = "Стабильный спрос в обоих периодах"
        elif order_units > raw and pack_size > 1:
            reason = "Заказ округлён до упаковки"
        else:
            reason = "Прогноз по двум периодам"

        # Категория
        if conf == "low":
            cat = "manual"
        elif order_units > 0:
            if not has_prev and not has_ya:
                cat = "manual"
            else:
                cat = "order"
        else:
            cat = "skip"

        # Особые случаи → ручная проверка
        if not prev_obs_complete and order_units > 0:
            cat = "manual"
        if effective_st < ST_PARTIAL and prev_recv > 0 and prev_dem == 0:
            cat = "skip"

        rows.append(ForecastRow(
            product_id=pid, product_name=pname, folder_path=fpath, subgroup=subgroup,
            pack_size=pack_size, pack_size_source=pack_src,
            prev_received=prev_recv, year_ago_received=ya_recv,
            prev_demand=prev_dem, year_ago_demand=ya_dem,
            prev_adj_demand=prev_adj, year_ago_adj_demand=eff_ya,
            prev_sell_through=prev_st, year_ago_sell_through=ya_st,
            prev_obs_complete=prev_obs_complete, year_ago_obs_complete=ya_obs_complete,
            prev_written_off=prev_wo,
            prev_days_in_stock=in_days, prev_days_total=days,
            prev_deficit_detected=deficit,
            available_stock=stk_avl, stock_all=stk_all, reserve_qty=stk_rsv,
            base_demand=base, safety_rate=safety,
            target_stock=target, raw_order=raw,
            order_units=order_units, order_packs=order_packs,
            buy_price_kop=buy_kop, is_supplier_discount=is_disc,
            cost_before_discount_kop=cost_before,
            discount_kop=disc_kop,
            cost_after_discount_kop=cost_after,
            confidence=conf, reason=reason, category=cat,
            has_prev=has_prev, has_year_ago=has_ya,
        ))

    return rows


# ── NEW engine: orchestration layer ──────────────────────────────────────────

@dataclass
class ForecastStoreConfig:
    """Конфигурация одного магазина для NEW engine."""
    store_id:   str
    channel:    str   # "BASE" | "RETAIL"
    store_href: str = ""   # требуется для BASE (загрузка CO из МойСклад)


def _get_snap_day(conn, cutoff_date: date) -> date | None:
    """Последний снимок остатков <= cutoff_date (по is_srezka=TRUE)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(day) FROM stock_snapshot
            WHERE day <= %s AND is_srezka = TRUE
        """, [cutoff_date])
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def build_forecast_new(
    conn,
    token:         str,
    store_configs: list[ForecastStoreConfig],
    cutoff_date:   date,
    horizon_from:  date,
    horizon_to:    date,
) -> list[ForecastResult]:
    """
    NEW engine — orchestration layer.

    Порядок:
      1. Общий snap_day из stock_snapshot (<= cutoff_date)
      2. Для каждого RETAIL-магазина: build_retail_forecast
         Для BASE-магазина: build_base_hybrid_forecast (4-компонентная модель)
      3. Для каждого результата:
         a. load_stock_snapshot(store_id, snap_day) → per-store остаток
         b. apply_replenishment_to_result → raw_order + rounded
         c. Флаги INCOMING_UNKNOWN (incoming_qty is None v1)
            + MANUAL_REVIEW + recommended_order_qty=None (если available_stock is None)

    Ключ изоляции: (product_id, store_id) — никогда не агрегировать раньше времени.
    Replenishment v1 (safety_stock=0):
      net_available = available + incoming + transfer_in − transfer_out − reserve
      raw_order     = max(0, expected_demand − net_available)
      rounded_order = ceil(raw_order / pack_size) × pack_size
    """
    from hermes.forecast_base import build_base_hybrid_forecast
    from hermes.forecast_retail import build_retail_forecast
    from hermes.forecast_data import load_stock_snapshot
    from hermes.forecast_replenishment import apply_replenishment_to_result
    from hermes.forecast_models import DataFlag

    snap_day = _get_snap_day(conn, cutoff_date)

    all_results: list[ForecastResult] = []

    for cfg in store_configs:
        # ── 1. Спрос ────────────────────────────────────────────────────────
        if cfg.channel == "BASE":
            results = build_base_hybrid_forecast(
                conn, token,
                store_id=cfg.store_id,
                store_href=cfg.store_href,
                cutoff_date=cutoff_date,
                horizon_from=horizon_from,
                horizon_to=horizon_to,
            )
        else:  # RETAIL
            results = build_retail_forecast(
                conn,
                store_id=cfg.store_id,
                cutoff_date=cutoff_date,
                horizon_from=horizon_from,
                horizon_to=horizon_to,
            )

        # ── 2. Остаток для данного магазина ─────────────────────────────────
        stock_by_pid = (
            load_stock_snapshot(conn, cfg.store_id, snap_day=snap_day)
            if snap_day else {}
        )

        # ── 3. Пополнение v1 + флаги ────────────────────────────────────────
        for r in results:
            snap = stock_by_pid.get(r.product_id)
            apply_replenishment_to_result(r, snap)

            # v1: поступления ещё не загружаются → INCOMING_UNKNOWN
            if r.incoming_qty is None:
                flags = r.data_quality_flags
                if DataFlag.INCOMING_UNKNOWN not in flags:
                    r.data_quality_flags = flags + (DataFlag.INCOMING_UNKNOWN,)

            # STOCK_UNKNOWN → ручная проверка, не выдавать конкретный заказ
            if r.available_stock is None:
                flags = r.data_quality_flags
                if DataFlag.MANUAL_REVIEW not in flags:
                    r.data_quality_flags = flags + (DataFlag.MANUAL_REVIEW,)
                r.recommended_order_qty = None  # сигнал для PDF/бота

        all_results.extend(results)

    return all_results


# ── Агрегаты для KPI ──────────────────────────────────────────────────────────

def summarize(rows: list[ForecastRow]) -> dict:
    order = [r for r in rows if r.category == "order"]
    skip  = [r for r in rows if r.category == "skip"]
    man   = [r for r in rows if r.category == "manual"]

    total_units = sum(r.order_units for r in order)
    total_packs = sum(r.order_packs for r in order)
    cost_before = sum(r.cost_before_discount_kop for r in order)
    discount    = sum(r.discount_kop             for r in order)
    cost_after  = sum(r.cost_after_discount_kop  for r in order)

    high   = sum(1 for r in order if r.confidence == "high")
    medium = sum(1 for r in order if r.confidence == "medium")
    low    = sum(1 for r in order if r.confidence == "low")

    return dict(
        n_order=len(order), n_skip=len(skip), n_manual=len(man),
        total_units=total_units, total_packs=total_packs,
        cost_before_kop=cost_before,
        discount_kop=discount,
        cost_after_kop=cost_after,
        confidence_high=high,
        confidence_medium=medium,
        confidence_low=low,
        n_deficit=sum(1 for r in rows if r.prev_deficit_detected),
        n_high_remainder=sum(1 for r in rows if r.prev_sell_through > 0 and r.prev_sell_through < 0.60 and r.prev_received > 0),
        n_incomplete_obs=sum(1 for r in rows if not r.prev_obs_complete),
        n_no_pack=sum(1 for r in rows if r.pack_size_source == "default" and r.pack_size == 1),
    )
