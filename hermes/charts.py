"""Графики через matplotlib — PNG bytes в брендовом стиле ЦБД.

Каждая функция возвращает bytes (PNG) или None если matplotlib не установлен
или данных нет.

Палитра: SAGE=#8A9A7B (прибыль/акцент), INK=#1A1A1A (выручка),
         TERRA=#C77B58 (списания), CREAM=#FDFBF7 (фон), GRID=#E3E1DA (сетка).
"""
from __future__ import annotations

import io
from datetime import date

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

# ── Палитра ЦБД ──────────────────────────────────────────────────────────────
_SAGE   = "#8A9A7B"
_SAGE_L = "#BDC6B3"
_INK    = "#1A1A1A"
_CREAM  = "#FDFBF7"
_TERRA  = "#C77B58"
_GRID   = "#E3E1DA"

_W_IN = 11.0     # ширина фигуры (дюймы)
_H_IN = 5.2      # высота фигуры (дюймы) — соотношение из make_charts.py
_DPI  = 150


def _rub(kop) -> float:
    return (kop or 0) / 100


def _rub_fmt(v: float, _=None) -> str:
    """Форматтер оси: '1 500k' / '500'."""
    if abs(v) >= 1_000:
        return f"{int(v / 1000)}k"
    return str(int(v))


def _base_rcparams() -> dict:
    return {
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.edgecolor": _GRID,
        "axes.linewidth": 1,
        "figure.facecolor": _CREAM,
        "axes.facecolor": _CREAM,
        "axes.grid": True,
        "grid.color": _GRID,
        "grid.linewidth": 0.8,
    }


def _new_fig():
    """Фигура с одной осью Y (CREAM-фон, GRID-сетка)."""
    with plt.rc_context(_base_rcparams()):
        fig, ax = plt.subplots(figsize=(_W_IN, _H_IN), dpi=_DPI)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.set_major_formatter(FuncFormatter(_rub_fmt))
    ax.tick_params(labelsize=10)
    return fig, ax


def _new_fig_twin():
    """Фигура с двумя осями Y: левая — выручка (INK), правая — прибыль (SAGE)."""
    with plt.rc_context(_base_rcparams()):
        fig, ax = plt.subplots(figsize=(_W_IN, _H_IN), dpi=_DPI)
    ax.spines["top"].set_visible(False)
    ax.yaxis.set_major_formatter(FuncFormatter(_rub_fmt))
    ax.tick_params(labelsize=10)

    ax2 = ax.twinx()
    ax2.spines["top"].set_visible(False)
    ax2.set_facecolor(_CREAM)
    ax2.yaxis.set_major_formatter(FuncFormatter(_rub_fmt))
    ax2.tick_params(labelsize=10, colors=_SAGE)
    ax2.set_ylabel("Прибыль", fontsize=11, color=_SAGE)
    ax2.grid(False)
    return fig, ax, ax2


def _save(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight",
                facecolor=_CREAM)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def chart_revenue_by_day(
    conn, d_from: date, d_to: date, store_name: str | None = None
) -> bytes | None:
    """Выручка — столбцами INK, прибыль — линией SAGE (двойная ось).

    Над каждым столбцом — сумма, над каждой точкой прибыли — маржа %.
    Последний день помечается как неполный, если продаж заметно меньше.
    """
    if not _MPL_OK:
        return None
    sf    = "AND store_name = %s" if store_name else ""
    sf_p  = "AND ssd.store_name = %s" if store_name else ""
    p_ssd = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT day, COALESCE(SUM(revenue_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY day ORDER BY day
        """, p_ssd)
        rev_rows = cur.fetchall()
    if not rev_rows:
        return None
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT spd.day, COALESCE(SUM(
                CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0
                THEN round(spd.sell_qty * pp.price_kop)
                ELSE round(spd.revenue_kop * 0.6) END
            ), 0)
            FROM sales_by_product_day spd
            JOIN sales_by_store_day ssd
                 ON ssd.store_id = spd.store_id AND ssd.day = spd.day
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = spd.assortment_id AND p.priced_from <= spd.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE spd.day BETWEEN %s AND %s {sf_p}
            GROUP BY spd.day
        """, p_ssd)
        cost_map = {{r[0]: int(r[1]) for r in cur.fetchall()}}
    rows = [(day, rev, rev - cost_map.get(day, round(rev * 0.6)))
            for day, rev in rev_rows]

    days   = [r[0] for r in rows]
    rev    = [_rub(r[1]) for r in rows]
    profit = [_rub(r[2]) for r in rows]
    x      = range(len(days))
    xlbls  = [d.strftime("%d.%m") for d in days]

    fig, ax, ax2 = _new_fig_twin()

    # Выручка — столбцы (INK)
    bars = ax.bar(x, rev, color=_INK, width=0.55, label="Выручка", zorder=3)
    ax.set_ylabel("Выручка", color=_INK, fontsize=11)

    # Прибыль — линия (SAGE)
    ax2.plot(x, profit, color=_SAGE, lw=3, marker="o", ms=7,
             label="Прибыль", zorder=4)

    # Маржа % над точками прибыли
    for i, (r, p) in enumerate(zip(rev, profit)):
        if r > 0:
            mg = round(p / r * 100)
            ax2.annotate(
                f"{mg}%", (i, p),
                textcoords="offset points", xytext=(0, 10),
                ha="center", fontsize=8, color=_SAGE, fontweight="bold",
            )

    # Пометить «неполный день» если последний день заметно меньше
    if len(rev) >= 2:
        avg_prev = sum(rev[:-1]) / len(rev[:-1]) if sum(rev[:-1]) else 1
        if rev[-1] < avg_prev * 0.5:
            ax.annotate(
                "неполный день", (len(rev) - 1, rev[-1]),
                textcoords="offset points", xytext=(0, 20),
                ha="center", fontsize=8, color=_TERRA, style="italic",
            )

    ax.set_xticks(list(x))
    ax.set_xticklabels(xlbls, fontsize=9)
    ax.set_title("Выручка и маржа по дням", fontsize=14, fontweight="bold",
                 color=_INK, pad=14)

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right",
              framealpha=0.95, facecolor=_CREAM, edgecolor=_GRID)

    return _save(fig)


def chart_stores_compare(
    conn, d_from: date, d_to: date
) -> bytes | None:
    """Выручка (INK) и прибыль (SAGE) по складам, маржа % над прибылью."""
    if not _MPL_OK:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT store_name, COALESCE(SUM(revenue_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s
            GROUP BY store_name
            ORDER BY SUM(revenue_kop) DESC
        """, [d_from, d_to])
        rev_rows = cur.fetchall()
    if not rev_rows:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ssd.store_name, COALESCE(SUM(
                CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0
                THEN round(spd.sell_qty * pp.price_kop)
                ELSE round(spd.revenue_kop * 0.6) END
            ), 0)
            FROM sales_by_product_day spd
            JOIN sales_by_store_day ssd
                 ON ssd.store_id = spd.store_id AND ssd.day = spd.day
            LEFT JOIN LATERAL (
                SELECT price_kop FROM purchase_price_asof p
                WHERE p.product_id = spd.assortment_id AND p.priced_from <= spd.day
                ORDER BY p.priced_from DESC LIMIT 1
            ) pp ON true
            WHERE spd.day BETWEEN %s AND %s
            GROUP BY ssd.store_name
        """, [d_from, d_to])
        cost_map = {r[0]: int(r[1]) for r in cur.fetchall()}
    rows = [(store, rev, rev - cost_map.get(store, round(rev * 0.6)))
            for store, rev in rev_rows]

    total_rev = sum(r[1] for r in rows) or 1
    rows = [r for r in rows if r[1] / total_rev >= 0.01]
    if not rows:
        return None

    stores = [r[0].replace(" ", "\n") for r in rows]
    rev    = [_rub(r[1]) for r in rows]
    profit = [_rub(r[2]) for r in rows]
    x = list(range(len(stores)))
    w = 0.38

    fig, ax = _new_fig()
    bars_r = ax.bar([i - w / 2 for i in x], rev,    width=w, color=_INK,  label="Выручка", zorder=3)
    bars_p = ax.bar([i + w / 2 for i in x], profit, width=w, color=_SAGE, label="Прибыль", zorder=3)

    for i, (r, p) in enumerate(zip(rev, profit)):
        if r > 0:
            rv_k = f"{int(r / 1000)}k" if r >= 1000 else str(int(r))
            ax.annotate(rv_k, (i - w / 2, r), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=8, color=_INK)
        if r > 0 and p >= 0:
            mg = round(p / r * 100)
            pk = f"{int(p / 1000)}k" if p >= 1000 else str(int(p))
            ax.annotate(pk, (i + w / 2, p), textcoords="offset points",
                        xytext=(0, 14), ha="center", fontsize=8, color=_SAGE)
            ax.annotate(f"{mg}%", (i + w / 2, p), textcoords="offset points",
                        xytext=(0, 4), ha="center", fontsize=9,
                        color=_SAGE, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(stores, fontsize=9)
    ax.set_title("Выручка, прибыль и маржа по складам", fontsize=14,
                 fontweight="bold", color=_INK, pad=14)
    ax.set_ylabel("Рублей", fontsize=11)
    ax.legend(loc="upper right", framealpha=0.95, facecolor=_CREAM, edgecolor=_GRID)
    for sp in ["top", "right"]:
        ax.spines[sp].set_visible(False)

    return _save(fig)


def chart_losses_vs_revenue(
    conn, d_from: date, d_to: date, store_name: str | None = None
) -> bytes | None:
    """Выручка (линия INK) + списания (столбцы TERRA) на двойной оси; % от выручки."""
    if not _MPL_OK:
        return None
    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT day, COALESCE(SUM(revenue_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY day ORDER BY day
        """, p)
        rev_map = {r[0]: _rub(r[1]) for r in cur.fetchall()}

    sf_l = "AND d.store_name = %s" if store_name else ""
    p_l  = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.day, COALESCE(SUM(i.total_kop), 0)
            FROM loss_doc d
            JOIN loss_item i ON i.doc_id = d.doc_id
            WHERE d.day BETWEEN %s AND %s {sf_l}
            GROUP BY d.day ORDER BY d.day
        """, p_l)
        loss_map = {r[0]: _rub(r[1]) for r in cur.fetchall()}

    all_days = sorted(set(list(rev_map) + list(loss_map)))
    if not all_days:
        return None
    rev_vals  = [rev_map.get(d, 0)  for d in all_days]
    loss_vals = [loss_map.get(d, 0) for d in all_days]
    x         = range(len(all_days))
    xlbls     = [d.strftime("%d.%m") for d in all_days]

    fig, ax = _new_fig()
    ax2 = ax.twinx()
    ax2.spines["top"].set_visible(False)
    ax2.yaxis.set_major_formatter(FuncFormatter(_rub_fmt))
    ax2.tick_params(labelsize=10, colors=_TERRA)
    ax2.set_ylabel("Списания", fontsize=11, color=_TERRA)
    ax2.grid(False)

    # Выручка — линия слева (INK)
    ax.plot(x, rev_vals, color=_INK, lw=3, marker="o", ms=6,
            label="Выручка", zorder=4)
    ax.set_ylabel("Выручка", color=_INK, fontsize=11)

    # Списания — столбцы справа (TERRA)
    ax2.bar(x, loss_vals, color=_TERRA, width=0.5, alpha=0.85,
            label="Списания", zorder=3)
    max_l = max(loss_vals) if any(v > 0 for v in loss_vals) else 1
    ax2.set_ylim(0, max_l * 2.2)

    # % от выручки над столбцами списаний
    for i, (r, lv) in enumerate(zip(rev_vals, loss_vals)):
        if r > 0 and lv > 0:
            pct = lv / r * 100
            ax2.annotate(
                f"{pct:.1f}%", (i, lv),
                textcoords="offset points", xytext=(0, 6),
                ha="center", fontsize=8, color=_TERRA, fontweight="bold",
            )

    ax.set_xticks(list(x))
    ax.set_xticklabels(xlbls, fontsize=9)
    ax.set_title("Выручка и списания по дням (% от выручки)", fontsize=14,
                 fontweight="bold", color=_INK, pad=14)

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper right",
              framealpha=0.95, facecolor=_CREAM, edgecolor=_GRID)

    return _save(fig)


def chart_forecast_summary(results, subgroups_by_pid: dict) -> "bytes | None":
    """
    4 панели на одном PNG — управленческий обзор прогноза закупки СРЕЗКИ.

    results          — list[ForecastResult]
    subgroups_by_pid — {product_id: subgroup_str}  (из OLD engine / product_dim)
    """
    if not _MPL_OK or not results:
        return None

    # ── агрегация ─────────────────────────────────────────────────────────────
    group_orders: dict[str, float] = {}
    store_data:   dict[str, dict]  = {}
    deficit_items: list[tuple[str, float]] = []
    n_order = n_zero = n_ok = 0

    for r in results:
        sg    = subgroups_by_pid.get(r.product_id, "Другое")
        order = r.recommended_order_qty or 0.0
        avail = r.available_stock or 0.0
        dem   = r.expected_demand or 0.0

        if order > 0:
            group_orders[sg] = group_orders.get(sg, 0.0) + order
            n_order += 1
        elif avail <= 0:
            n_zero += 1
        else:
            n_ok += 1

        sname = r.store_name or r.store_id
        if sname not in store_data:
            store_data[sname] = {"demand": 0.0, "stock": 0.0, "order": 0.0}
        store_data[sname]["demand"] += dem
        store_data[sname]["stock"]  += max(avail, 0.0)
        store_data[sname]["order"]  += order

        deficit = dem - max(avail, 0.0)
        if deficit > 0 and order > 0:
            deficit_items.append((r.product_name, deficit))

    top_groups  = sorted(group_orders.items(), key=lambda x: x[1], reverse=True)[:10]
    top_deficit = sorted(deficit_items,        key=lambda x: x[1], reverse=True)[:10]

    # ── фигура 2×2 ────────────────────────────────────────────────────────────
    with plt.rc_context(_base_rcparams()):
        fig, axes = plt.subplots(2, 2, figsize=(14, 10), dpi=_DPI)
    fig.patch.set_facecolor(_CREAM)
    fig.suptitle("Прогноз закупки СРЕЗКИ — управленческий обзор",
                 fontsize=13, fontweight="bold", color=_INK, y=0.99)

    # ── панель 1: К заказу по группам ─────────────────────────────────────────
    ax1 = axes[0, 0]
    ax1.set_facecolor(_CREAM)
    if top_groups:
        names, vals = zip(*reversed(top_groups))
        names = [n[:28] for n in names]
        bars = ax1.barh(names, vals, color=_SAGE, edgecolor="none", height=0.6)
        mx = max(vals)
        for bar, v in zip(bars, vals):
            ax1.text(v + mx * 0.02, bar.get_y() + bar.get_height() / 2,
                     f"{int(v)}", va="center", fontsize=8, color=_INK)
        ax1.set_xlim(0, mx * 1.18)
        ax1.set_xlabel("шт.", fontsize=9)
        ax1.xaxis.grid(True, color=_GRID, linewidth=0.6)
        ax1.yaxis.grid(False)
        ax1.tick_params(axis="y", labelsize=8)
    ax1.set_title("К заказу по группам (топ-10)", fontsize=11, fontweight="bold", pad=8)
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)

    # ── панель 2: по магазинам ─────────────────────────────────────────────────
    ax2 = axes[0, 1]
    ax2.set_facecolor(_CREAM)
    if store_data:
        stores = list(store_data.keys())
        short  = [s[:18] for s in stores]
        xs     = range(len(stores))
        w      = 0.26
        ax2.bar([i - w for i in xs], [store_data[s]["demand"] for s in stores],
                width=w, label="Спрос", color=_INK, alpha=0.65)
        ax2.bar(list(xs), [store_data[s]["stock"] for s in stores],
                width=w, label="Остаток", color=_SAGE_L)
        ax2.bar([i + w for i in xs], [store_data[s]["order"] for s in stores],
                width=w, label="К заказу", color=_SAGE)
        ax2.set_xticks(list(xs))
        ax2.set_xticklabels(short, rotation=18, ha="right", fontsize=8)
        ax2.set_ylabel("шт.", fontsize=9)
        ax2.yaxis.grid(True, color=_GRID, linewidth=0.6)
        ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("По магазинам", fontsize=11, fontweight="bold", pad=8)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)

    # ── панель 3: статус ассортимента ─────────────────────────────────────────
    ax3 = axes[1, 0]
    ax3.set_facecolor(_CREAM)
    labels_p = ["К заказу", "Нулевой остаток", "Достаточно"]
    sizes_p  = [n_order, n_zero, n_ok]
    colors_p = [_SAGE, _TERRA, _SAGE_L]
    non_zero = [(l, s, c) for l, s, c in zip(labels_p, sizes_p, colors_p) if s > 0]
    if non_zero:
        lp, sp, cp = zip(*non_zero)
        _, _, autotexts = ax3.pie(
            sp, labels=lp, colors=cp, autopct="%1.0f%%",
            startangle=90, textprops={"fontsize": 9},
            wedgeprops={"edgecolor": _CREAM, "linewidth": 1.5},
        )
        for at in autotexts:
            at.set_fontsize(8)
        ax3.text(0, -1.35, f"Всего SKU: {n_order + n_zero + n_ok}",
                 ha="center", fontsize=9, color=_INK)
    ax3.set_title("Статус ассортимента", fontsize=11, fontweight="bold", pad=8)

    # ── панель 4: топ дефицит ─────────────────────────────────────────────────
    ax4 = axes[1, 1]
    ax4.set_facecolor(_CREAM)
    if top_deficit:
        dnames, dvals = zip(*reversed(top_deficit))
        dnames = [n[:32] for n in dnames]
        bars = ax4.barh(dnames, dvals, color=_TERRA, edgecolor="none", height=0.6)
        mx = max(dvals)
        for bar, v in zip(bars, dvals):
            ax4.text(v + mx * 0.02, bar.get_y() + bar.get_height() / 2,
                     f"{int(v)}", va="center", fontsize=8, color=_INK)
        ax4.set_xlim(0, mx * 1.18)
        ax4.set_xlabel("шт. (спрос − остаток)", fontsize=9)
        ax4.xaxis.grid(True, color=_GRID, linewidth=0.6)
        ax4.yaxis.grid(False)
        ax4.tick_params(axis="y", labelsize=8)
    ax4.set_title("Топ-10 дефицит", fontsize=11, fontweight="bold", pad=8)
    ax4.spines["top"].set_visible(False)
    ax4.spines["right"].set_visible(False)

    return _save(fig)


def chart_forecast_history(history_rows: list) -> "bytes | None":
    """
    Динамика прогноза за последние N запусков.

    history_rows — [(horizon_from, total_order, total_demand, total_stock), ...]
                   хронологически (старые → новые).
    """
    if not _MPL_OK or len(history_rows) < 2:
        return None

    dates   = [r[0] for r in history_rows]
    orders  = [float(r[1] or 0) for r in history_rows]
    demands = [float(r[2] or 0) for r in history_rows]
    stocks  = [float(r[3] or 0) for r in history_rows]
    labels  = [d.strftime("%d.%m") if hasattr(d, "strftime") else str(d) for d in dates]

    with plt.rc_context(_base_rcparams()):
        fig, ax = plt.subplots(figsize=(_W_IN, _H_IN), dpi=_DPI)
    ax.set_facecolor(_CREAM)
    fig.patch.set_facecolor(_CREAM)

    xs = range(len(labels))
    ax.plot(xs, demands, "o-", color=_INK,    label="Спрос",    linewidth=2,   markersize=5)
    ax.plot(xs, orders,  "s-", color=_SAGE,   label="К заказу", linewidth=2,   markersize=5)
    ax.plot(xs, stocks,  "^-", color=_SAGE_L, label="Остаток",  linewidth=1.5, markersize=4,
            linestyle="--")

    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, rotation=15, ha="right", fontsize=9)
    ax.set_ylabel("шт.", fontsize=10)
    ax.set_title("Динамика прогноза (по неделям)", fontsize=12, fontweight="bold")
    ax.legend(fontsize=9, loc="upper left")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, color=_GRID, linewidth=0.8)

    return _save(fig)


def chart_period_summary(
    curr: dict,
    prev: dict,
    label_curr: str = "Текущий",
    label_prev: str = "Предыдущий",
) -> bytes | None:
    """Горизонтальный chart сравнения двух периодов.

    curr / prev — словари из _pdf_summary (ключи: rev, profit, op_expenses,
    loss, loss_spoil, result). Все значения в копейках.
    Читабелен на телефоне: горизонтальные бары, 6 метрик.
    """
    if not _MPL_OK:
        return None

    with _MPL_LOCK:
        import matplotlib.pyplot as plt
        import numpy as np

        plt.rcParams.update(_BASE_RCPARAMS)

        def _kk(kop) -> float:
            return float(kop or 0) / 100 / 1000  # тыс. ₽

        profit_before_c = _kk(curr["profit"]) - _kk(curr["op_expenses"])
        profit_before_p = _kk(prev["profit"]) - _kk(prev["op_expenses"])

        metrics = [
            ("Выручка",               _kk(curr["rev"]),      _kk(prev["rev"])),
            ("Валовая прибыль",        _kk(curr["profit"]),   _kk(prev["profit"])),
            ("Прибыль до списаний",    profit_before_c,       profit_before_p),
            ("Прибыль после списаний", _kk(curr["result"]),   _kk(prev["result"])),
            ("Списания",               _kk(curr["loss"]),     _kk(prev["loss"])),
            ("Расходы",                _kk(curr["op_expenses"]), _kk(prev["op_expenses"])),
        ]

        labels = [m[0] for m in metrics]
        vals_c = [m[1] for m in metrics]
        vals_p = [m[2] for m in metrics]

        fig, ax = plt.subplots(figsize=(10, 6), dpi=_DPI)
        fig.patch.set_facecolor(_BASE_RCPARAMS["figure.facecolor"])
        ax.set_facecolor(_BASE_RCPARAMS["axes.facecolor"])

        x = np.arange(len(labels))
        w = 0.36

        bars_c = ax.barh(x + w / 2, vals_c, w, color=_SAGE,   label=label_curr, zorder=3)
        bars_p = ax.barh(x - w / 2, vals_p, w, color=_SAGE_L, label=label_prev, zorder=3)

        ax.set_yticks(x)
        ax.set_yticklabels(labels, fontsize=11)
        ax.set_xlabel("тыс. ₽", fontsize=9, color=_INK)
        ax.grid(axis="x", color=_GRID, linewidth=0.5, zorder=0)
        ax.legend(fontsize=9, loc="lower right")
        ax.set_title("Сравнение периодов", fontsize=14, fontweight="bold",
                     color=_INK, pad=12)
        ax.invert_yaxis()

        # подписи бар
        max_val = max((abs(v) for v in vals_c + vals_p if v), default=1)
        offset  = max_val * 0.015 + 0.3
        for bar, val in zip(list(bars_c) + list(bars_p),
                            vals_c + vals_p):
            if abs(val) < 0.3:
                continue
            clr = _SAGE if bar in bars_c else _SAGE_L
            ax.text(
                val + (offset if val >= 0 else -offset),
                bar.get_y() + bar.get_height() / 2,
                f"{val:.0f}к",
                va="center",
                ha="left" if val >= 0 else "right",
                fontsize=8,
                color=clr,
            )

        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout(pad=1.2)
        return _save(fig)
