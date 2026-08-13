"""
Нейтральный data layer для прогнозирования.
Строит DayRecord (факты одного дня) и ForecastFeatures (агрегаты периода).
НЕ выполняет прогнозирование.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Sequence

# ── Типы ──────────────────────────────────────────────────────────────

# Что наблюдаемые факты говорят о наличии товара в этот день.
# Не прогнозное предположение — только интерпретация имеющихся данных.
AvailabilityEvidence = Literal[
    "POSITIVE_STOCK_OBSERVED",  # снимок есть, stock_qty > 0, день не аномальный
    "NO_SNAPSHOT_ROW",          # строки в stock_snapshot нет (ETL не пишет stock_qty ≤ 0)
    "SNAPSHOT_UNRELIABLE",      # частичный ETL (SNAPSHOT_VOLUME_ANOMALY)
    "CONFLICTING_EVIDENCE",     # продажи > 0 при отсутствующем снимке или другие противоречия
]

# Флаги качества данных. Строки, не числовой score.
QualityFlag = Literal[
    "SNAPSHOT_VOLUME_ANOMALY",   # частичный ETL в этот день
    "BALANCE_MISMATCH",          # V2 balance не закрывается
    "NO_SNAPSHOT",               # нет строки в stock_snapshot
    "SALES_WITHOUT_SNAPSHOT",    # продажи есть, снимка нет — противоречие
]


# ── DayRecord ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DayRecord:
    """Факты одного дня для одного product_id × store_id.
    Не содержит агрегатов периода и прогнозных предположений.
    """

    date: date
    product_id: str
    product_name: str
    store_id: str
    store_name: str

    # Продажи за день (из sales_by_product_day)
    sales_qty: float

    # Остатки из stock_snapshot (None если строки нет)
    stock_qty: float | None
    available_qty: float | None
    reserve_qty: float | None

    # Движения с момент.date() == этот день (из *_doc/*_item)
    supply_qty: float
    move_in_qty: float
    move_out_qty: float
    loss_qty: float
    enter_qty: float

    # Качество снимка
    snapshot_present: bool
    snapshot_volume_anomaly: bool  # частичный ETL в этот день

    # V2 balance-check: этот день как T1, ближайший предыдущий снимок как T0.
    # None если нет пары снимков для сравнения.
    balance_ok: bool | None
    balance_abs_error: float | None

    # Набор строковых флагов качества
    data_quality_flags: tuple[str, ...]

    # Чем объясняется наблюдаемая доступность — факт, не предположение
    availability_evidence: AvailabilityEvidence


# ── ForecastFeatures ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ForecastFeatures:
    """Агрегаты периода для одного product_id × store_id.
    Входные данные для прогнозной модели. Отдельная структура от DayRecord.
    """

    product_id: str
    store_id: str
    date_from: date
    date_to: date

    # Количество дней по типу availability_evidence
    calendar_days: int
    positive_stock_days: int
    no_snapshot_days: int
    unreliable_snapshot_days: int
    conflicting_evidence_days: int

    # Продажи только по дням POSITIVE_STOCK_OBSERVED
    confirmed_sales_total: float   # сумма продаж в эти дни
    confirmed_sales_days: int      # дней с ненулевыми продажами из подтверждённых
    all_period_sales_total: float  # сумма продаж за весь период

    # Статистики (None если меньше 2 подтверждённых дней)
    median_daily_sales: float | None
    mean_daily_sales: float | None

    # Итоги движений за период
    supply_qty_total: float
    enter_qty_total: float

    # Качество
    data_quality_flags: tuple[str, ...]
    balance_ok_ratio: float | None  # доля дней с balance_ok=True, None если нет данных


# ── Загрузка данных ─────────────────────────────────────────────────

def _q(conn, sql: str, params=()) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def detect_snapshot_anomaly_dates(conn) -> set[date]:
    """Находит дни с аномально низким покрытием снимков (< 70% от медианы).

    Алгоритм:
    1. Считает кол-во SKU per store per day
    2. Медиана кол-ва по каждому складу
    3. День считается аномальным для склада если < 70% медианы
    4. Если >50% складов аномальны в этот день — день аномален глобально
    """
    rows = _q(conn, """
        SELECT day, store_id, COUNT(DISTINCT product_id) AS n_sku
        FROM stock_snapshot WHERE is_srezka = TRUE
        GROUP BY day, store_id
    """)
    if not rows:
        return set()

    by_store: dict[str, list] = defaultdict(list)
    counts: dict[tuple, int] = {}
    for day, sid, n in rows:
        by_store[sid].append(n)
        counts[(day, sid)] = n

    store_medians = {sid: statistics.median(vals) for sid, vals in by_store.items()}
    all_days = sorted({k[0] for k in counts})
    stores = list(store_medians)

    anomalies: set[date] = set()
    for d in all_days:
        anomalous_stores = sum(
            1 for sid in stores
            if counts.get((d, sid), 0) < 0.70 * store_medians[sid]
        )
        if anomalous_stores > len(stores) * 0.5:
            anomalies.add(d)
    return anomalies


def _load_snaps(conn, pids: list[str], d_from: date, d_to: date) -> dict:
    """Возвращает {(pid, sid, day): {stock, avail, rsrv, ts}}"""
    rows = _q(conn, """
        SELECT product_id, store_id, day, stock_qty, available_qty, reserve_qty, synced_at
        FROM stock_snapshot
        WHERE product_id = ANY(%s) AND is_srezka = TRUE AND day BETWEEN %s AND %s
    """, [pids, d_from, d_to])
    return {
        (r[0], r[1], r[2]): {
            "stock": float(r[3] or 0),
            "avail": float(r[4]) if r[4] is not None else None,
            "rsrv":  float(r[5]) if r[5] is not None else None,
            "ts":    r[6],
        }
        for r in rows
    }


def _load_sales(conn, pids: list[str], d_from: date, d_to: date) -> defaultdict:
    """Возвращает defaultdict{(pid, sid, day): qty}"""
    rows = _q(conn, """
        SELECT assortment_id, store_id, day, sell_qty
        FROM sales_by_product_day
        WHERE assortment_id = ANY(%s) AND day BETWEEN %s AND %s
    """, [pids, d_from, d_to])
    res: defaultdict = defaultdict(float)
    for r in rows:
        res[(r[0], r[1], r[2])] = float(r[3] or 0)
    return res


def _load_movements_by_day(conn, pids: list[str], d_from: date, d_to: date) -> dict:
    """Движения, сгруппированные по calendar day (moment.date()).

    Возвращает {(pid, sid, day): {supply, enter, loss, move_in, move_out}}
    Используется для отображения фактов дня в DayRecord.
    """
    result: dict = defaultdict(lambda: defaultdict(float))

    def _fetch(sql: str, key: str) -> None:
        for r in _q(conn, sql, [pids, d_from, d_to]):
            if r[2]:  # moment не None
                d = r[2].date()
                result[(r[0], r[1], d)][key] += float(r[3] or 0)

    _fetch("""
        SELECT si.product_id, sd.store_id, sd.moment, SUM(si.qty)
        FROM supply_item si JOIN supply_doc sd ON sd.doc_id = si.doc_id
        WHERE si.product_id = ANY(%s) AND sd.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "supply")
    _fetch("""
        SELECT ei.product_id, ed.store_id, ed.moment, SUM(ei.qty)
        FROM enter_item ei JOIN enter_doc ed ON ed.doc_id = ei.doc_id
        WHERE ei.product_id = ANY(%s) AND ed.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "enter")
    _fetch("""
        SELECT li.product_id, ld.store_id, ld.moment, SUM(li.qty)
        FROM loss_item li JOIN loss_doc ld ON ld.doc_id = li.doc_id
        WHERE li.product_id = ANY(%s) AND ld.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "loss")
    _fetch("""
        SELECT mi.product_id, md.store_to_id, md.moment, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id = mi.doc_id
        WHERE mi.product_id = ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "move_in")
    _fetch("""
        SELECT mi.product_id, md.store_from_id, md.moment, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id = mi.doc_id
        WHERE mi.product_id = ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "move_out")
    return result


def _load_movements_ts(conn, pids: list[str], d_from: date, d_to: date) -> dict:
    """Движения с точными timestamp — для V2 balance check.

    Возвращает {(pid, sid): {key: [(ts, qty)]}}
    """
    result: dict = defaultdict(lambda: defaultdict(list))

    def _fetch(sql: str, key: str) -> None:
        for r in _q(conn, sql, [pids, d_from, d_to]):
            if r[2]:
                result[(r[0], r[1])][key].append((r[2], float(r[3] or 0)))

    _fetch("""
        SELECT si.product_id, sd.store_id, sd.moment, SUM(si.qty)
        FROM supply_item si JOIN supply_doc sd ON sd.doc_id = si.doc_id
        WHERE si.product_id = ANY(%s) AND sd.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "supply")
    _fetch("""
        SELECT ei.product_id, ed.store_id, ed.moment, SUM(ei.qty)
        FROM enter_item ei JOIN enter_doc ed ON ed.doc_id = ei.doc_id
        WHERE ei.product_id = ANY(%s) AND ed.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "enter")
    _fetch("""
        SELECT li.product_id, ld.store_id, ld.moment, SUM(li.qty)
        FROM loss_item li JOIN loss_doc ld ON ld.doc_id = li.doc_id
        WHERE li.product_id = ANY(%s) AND ld.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "loss")
    _fetch("""
        SELECT mi.product_id, md.store_to_id, md.moment, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id = mi.doc_id
        WHERE mi.product_id = ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "move_in")
    _fetch("""
        SELECT mi.product_id, md.store_from_id, md.moment, SUM(mi.qty)
        FROM move_item mi JOIN move_doc md ON md.doc_id = mi.doc_id
        WHERE mi.product_id = ANY(%s) AND md.day BETWEEN %s AND %s
        GROUP BY 1, 2, 3
    """, "move_out")
    return result


def _wts(items: list[tuple], t0, t1) -> float:
    """Сумма qty для движений с t0 < ts <= t1."""
    return sum(qty for ts, qty in items if t0 < ts <= t1)


def _sales_window(sales: defaultdict, pid: str, sid: str, d_start: date, d_end: date) -> float:
    """Продажи в окне [d_start, d_end) — d_start включён, d_end исключён (V2)."""
    total = 0.0
    d = d_start
    while d < d_end:
        total += sales.get((pid, sid, d), 0)
        d += timedelta(days=1)
    return total


def _v2_balance(
    s0: dict, s1: dict,
    pid: str, sid: str,
    d_prev: date, d_curr: date,
    sales: defaultdict,
    mv_ts: dict,
) -> tuple[bool | None, float | None]:
    """V2 balance check между снимком d_prev и d_curr.
    Возвращает (balance_ok, abs_error). None если нет обоих снимков.
    """
    t0 = s0["ts"]; t1 = s1["ts"]
    q0 = s0["stock"]; q1 = s1["stock"]
    mv = mv_ts.get((pid, sid), {})
    sold   = _sales_window(sales, pid, sid, d_prev, d_curr)
    supply = _wts(mv.get("supply", []), t0, t1)
    enter  = _wts(mv.get("enter", []), t0, t1)
    loss   = _wts(mv.get("loss", []), t0, t1)
    mi     = _wts(mv.get("move_in", []), t0, t1)
    mo     = _wts(mv.get("move_out", []), t0, t1)
    pred   = q0 + supply + enter + mi - sold - mo - loss
    err    = abs(q1 - pred)
    return err <= 0.5, round(err, 1)


# ── Основные функции ──────────────────────────────────────────────

def build_day_records(
    conn,
    date_from: date,
    date_to: date,
    *,
    product_ids: list[str] | None = None,
    store_ids: list[str] | None = None,
    anomaly_dates: set[date] | None = None,
) -> list[DayRecord]:
    """Строит список DayRecord для всех SKU×Магазин×День в периоде.

    Если product_ids=None — берёт все СРЕЗКА товары из product_dim.
    Если anomaly_dates=None — вычисляет автоматически через detect_snapshot_anomaly_dates.
    Данные загружаются с запасом в 1 день для balance check предыдущего дня.
    """
    if product_ids is None:
        rows = _q(conn, "SELECT product_id FROM product_dim WHERE is_srezka = TRUE")
        product_ids = [r[0] for r in rows]
    if not product_ids:
        return []

    if anomaly_dates is None:
        anomaly_dates = detect_snapshot_anomaly_dates(conn)

    # Загружаем с запасом -1 день для balance check
    load_from = date_from - timedelta(1)

    snaps    = _load_snaps(conn, product_ids, load_from, date_to)
    sales    = _load_sales(conn, product_ids, date_from, date_to)
    mv_day   = _load_movements_by_day(conn, product_ids, date_from, date_to)
    mv_ts    = _load_movements_ts(conn, product_ids, load_from, date_to)
    store_nm = {r[0]: r[1] for r in _q(conn,
        "SELECT DISTINCT ON (store_id) store_id, store_name FROM stock_snapshot")}
    prod_nm  = {r[0]: r[1] for r in _q(conn,
        "SELECT product_id, product_name FROM product_dim WHERE product_id = ANY(%s)",
        [product_ids])}

    # Все pid×sid комбинации, у которых есть хоть один снимок в периоде
    ps_pairs: set[tuple[str, str]] = {
        (pid, sid)
        for (pid, sid, d) in snaps
        if pid in set(product_ids) and date_from <= d <= date_to
    }
    # Дополнить парами из продаж (могут быть продажи без снимка)
    for (pid, sid, d) in sales:
        if date_from <= d <= date_to:
            ps_pairs.add((pid, sid))

    if store_ids:
        ps_pairs = {(p, s) for p, s in ps_pairs if s in set(store_ids)}

    records: list[DayRecord] = []
    n_days = (date_to - date_from).days + 1

    for pid, sid in sorted(ps_pairs):
        for i in range(n_days):
            d = date_from + timedelta(i)
            snap   = snaps.get((pid, sid, d))
            prev_d = d - timedelta(1)
            snap_p = snaps.get((pid, sid, prev_d))
            s_qty  = sales.get((pid, sid, d), 0)
            mv     = mv_day.get((pid, sid, d), {})

            # Признаки качества снимка
            is_anomaly = d in anomaly_dates
            snap_ok    = snap is not None

            # V2 balance check
            bal_ok  = None
            bal_err = None
            if snap_ok and snap_p is not None and not is_anomaly and prev_d not in anomaly_dates:
                bal_ok, bal_err = _v2_balance(
                    snap_p, snap, pid, sid, prev_d, d, sales, mv_ts
                )

            # Флаги качества
            flags: list[str] = []
            if is_anomaly:
                flags.append("SNAPSHOT_VOLUME_ANOMALY")
            if not snap_ok:
                flags.append("NO_SNAPSHOT")
                if s_qty > 0:
                    flags.append("SALES_WITHOUT_SNAPSHOT")
            elif bal_ok is False:
                flags.append("BALANCE_MISMATCH")

            # Availability evidence — только факты
            if is_anomaly:
                evidence: AvailabilityEvidence = "SNAPSHOT_UNRELIABLE"
            elif not snap_ok and s_qty > 0:
                evidence = "CONFLICTING_EVIDENCE"
            elif not snap_ok:
                evidence = "NO_SNAPSHOT_ROW"
            else:
                # snap присутствует; ETL не пишет stock_qty <= 0,
                # поэтому наличие строки уже означает положительный остаток
                evidence = "POSITIVE_STOCK_OBSERVED"

            records.append(DayRecord(
                date=d,
                product_id=pid,
                product_name=prod_nm.get(pid, pid),
                store_id=sid,
                store_name=store_nm.get(sid, sid),
                sales_qty=s_qty,
                stock_qty=snap["stock"] if snap else None,
                available_qty=snap.get("avail") if snap else None,
                reserve_qty=snap.get("rsrv") if snap else None,
                supply_qty=mv.get("supply", 0),
                move_in_qty=mv.get("move_in", 0),
                move_out_qty=mv.get("move_out", 0),
                loss_qty=mv.get("loss", 0),
                enter_qty=mv.get("enter", 0),
                snapshot_present=snap_ok,
                snapshot_volume_anomaly=is_anomaly,
                balance_ok=bal_ok,
                balance_abs_error=bal_err,
                data_quality_flags=tuple(flags),
                availability_evidence=evidence,
            ))

    return records


def build_forecast_features(
    records: list[DayRecord],
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ForecastFeatures]:
    """Строит ForecastFeatures по списку DayRecord (агрегация за период).

    Группирует по product_id × store_id.
    date_from/date_to берутся из records если не указаны явно.
    """
    if not records:
        return []

    by_ps: dict[tuple[str, str], list[DayRecord]] = defaultdict(list)
    for r in records:
        by_ps[(r.product_id, r.store_id)].append(r)

    result: list[ForecastFeatures] = []
    for (pid, sid), recs in by_ps.items():
        recs_sorted = sorted(recs, key=lambda x: x.date)
        d_from = date_from or recs_sorted[0].date
        d_to   = date_to   or recs_sorted[-1].date

        pos    = [r for r in recs if r.availability_evidence == "POSITIVE_STOCK_OBSERVED"]
        no_sn  = [r for r in recs if r.availability_evidence == "NO_SNAPSHOT_ROW"]
        unrel  = [r for r in recs if r.availability_evidence == "SNAPSHOT_UNRELIABLE"]
        confl  = [r for r in recs if r.availability_evidence == "CONFLICTING_EVIDENCE"]

        conf_sales = [r.sales_qty for r in pos]
        med = statistics.median(conf_sales) if len(conf_sales) >= 2 else None
        mn  = (sum(conf_sales) / len(conf_sales)) if conf_sales else None

        bal_checks = [r for r in recs if r.balance_ok is not None]
        bal_ratio  = (
            sum(1 for r in bal_checks if r.balance_ok) / len(bal_checks)
            if bal_checks else None
        )

        all_flags: set[str] = set()
        for r in recs:
            all_flags.update(r.data_quality_flags)
        if len(pos) < 3:
            all_flags.add("LIMITED_HISTORY")

        result.append(ForecastFeatures(
            product_id=pid,
            store_id=sid,
            date_from=d_from,
            date_to=d_to,
            calendar_days=(d_to - d_from).days + 1,
            positive_stock_days=len(pos),
            no_snapshot_days=len(no_sn),
            unreliable_snapshot_days=len(unrel),
            conflicting_evidence_days=len(confl),
            confirmed_sales_total=sum(conf_sales),
            confirmed_sales_days=sum(1 for s in conf_sales if s > 0),
            all_period_sales_total=sum(r.sales_qty for r in recs),
            median_daily_sales=med,
            mean_daily_sales=round(mn, 4) if mn is not None else None,
            supply_qty_total=sum(r.supply_qty for r in recs),
            enter_qty_total=sum(r.enter_qty for r in recs),
            data_quality_flags=tuple(sorted(all_flags)),
            balance_ok_ratio=round(bal_ratio, 3) if bal_ratio is not None else None,
        ))

    return result
