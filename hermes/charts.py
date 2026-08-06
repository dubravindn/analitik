"""Графики через matplotlib — PNG bytes (1080×720 px, шрифт ≥14pt, суммы в ₽).

Каждая функция возвращает bytes (PNG) или None если matplotlib не установлен
или данных нет.
"""
from __future__ import annotations

import io
from datetime import date

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

_W_IN = 1080 / 96   # 11.25 дюйма
_H_IN = 720  / 96   # 7.5 дюйма
_DPI  = 96
_FS   = 14           # базовый размер шрифта

_COL_REV  = "#4A7BCC"
_COL_PROF = "#5CB85C"
_COL_LOSS = "#E05252"
_GRID_CLR = "#EBEBEB"


def _rub(kop) -> float:
    return (kop or 0) / 100


def _fmt_rub(v: float, _pos=None) -> str:
    """Форматтер оси Y: '1 500 000 ₽'."""
    return f"{int(v):,}".replace(",", " ") + " ₽"


def _new_fig():
    fig, ax = plt.subplots(figsize=(_W_IN, _H_IN), dpi=_DPI)
    ax.grid(axis="y", color=_GRID_CLR, linewidth=0.8, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=_FS - 2)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(_fmt_rub))
    return fig, ax


def _new_fig_twin():
    """Фигура с двумя осями Y: левая — выручка, правая — прибыль."""
    fig, ax = plt.subplots(figsize=(_W_IN, _H_IN), dpi=_DPI)
    ax.grid(axis="y", color=_GRID_CLR, linewidth=0.8, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.tick_params(labelsize=_FS - 2)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(_fmt_rub))
    ax2 = ax.twinx()
    ax2.spines["top"].set_visible(False)
    ax2.tick_params(labelsize=_FS - 2, colors=_COL_PROF)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(_fmt_rub))
    ax2.set_ylabel("Прибыль (прав. ось)", fontsize=_FS - 1, color=_COL_PROF)
    return fig, ax, ax2


def _save(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def chart_revenue_by_day(
    conn, d_from: date, d_to: date, store_name: str | None = None
) -> bytes | None:
    """Линейный: выручка (лев. ось) + прибыль (прав. ось) по дням.

    N8: двойная ось Y — прибыль не прижата к нулю на фоне выручки.
    Точки с отрицательной прибылью подсвечены красным маркером.
    """
    if not _MPL_OK:
        return None
    sf = "AND store_name = %s" if store_name else ""
    p  = [d_from, d_to] + ([store_name] if store_name else [])
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT day,
                   COALESCE(SUM(revenue_kop), 0),
                   COALESCE(SUM(revenue_kop - cost_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s {sf}
            GROUP BY day ORDER BY day
        """, p)
        rows = cur.fetchall()
    if not rows:
        return None

    days   = [r[0] for r in rows]
    rev    = [_rub(r[1]) for r in rows]
    profit = [_rub(r[2]) for r in rows]

    fig, ax, ax2 = _new_fig_twin()
    ax.plot(days, rev, marker="o", ms=4, lw=2.5, color=_COL_REV,
            label="Выручка", zorder=3)
    ax2.plot(days, profit, marker="s", ms=4, lw=2, color=_COL_PROF,
             linestyle="--", label="Прибыль", zorder=3)

    # Подсветить дни с отрицательной прибылью
    neg_days   = [d for d, p in zip(days, profit) if p < 0]
    neg_profit = [p for p in profit if p < 0]
    if neg_days:
        ax2.scatter(neg_days, neg_profit, color=_COL_LOSS, s=60, zorder=4,
                    label="Убыточный день")

    ax.set_title("Выручка и прибыль от продаж по дням", fontsize=_FS + 2, pad=10)
    ax.set_xlabel("Дата", fontsize=_FS)
    ax.set_ylabel("Выручка (лев. ось)", fontsize=_FS)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=40)

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, fontsize=_FS - 1, loc="upper left")
    return _save(fig)


def chart_stores_compare(
    conn, d_from: date, d_to: date
) -> bytes | None:
    """Столбчатый: выручка и прибыль по каждому складу за период.

    N9: склады с долей выручки <1% скрыты (шум). Подписи значений над столбцами.
    """
    if not _MPL_OK:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT store_name,
                   COALESCE(SUM(revenue_kop), 0),
                   COALESCE(SUM(revenue_kop - cost_kop), 0)
            FROM sales_by_store_day
            WHERE day BETWEEN %s AND %s
            GROUP BY store_name
            ORDER BY SUM(revenue_kop) DESC
        """, [d_from, d_to])
        rows = cur.fetchall()
    if not rows:
        return None

    total_rev = sum(r[1] for r in rows) or 1
    # Отсекаем склады с долей < 1%
    rows = [r for r in rows if r[1] / total_rev >= 0.01]
    if not rows:
        return None

    stores = [r[0] for r in rows]
    rev    = [_rub(r[1]) for r in rows]
    profit = [_rub(r[2]) for r in rows]

    x = list(range(len(stores)))
    w = 0.38
    fig, ax = _new_fig()
    bars_r = ax.bar([i - w / 2 for i in x], rev,    width=w, color=_COL_REV,  label="Выручка", zorder=2)
    bars_p = ax.bar([i + w / 2 for i in x], profit, width=w, color=_COL_PROF, label="Прибыль", zorder=2)

    # Подписи значений над столбцами (N9)
    def _label_bars(bars):
        for bar in bars:
            h = bar.get_height()
            if abs(h) < 1:
                continue
            label = f"{int(h / 1000):,}k".replace(",", " ") if abs(h) >= 1000 else str(int(h))
            ax.text(bar.get_x() + bar.get_width() / 2, max(h, 0) + ax.get_ylim()[1] * 0.01,
                    label, ha="center", va="bottom", fontsize=_FS - 4, rotation=0)

    _label_bars(bars_r)
    _label_bars(bars_p)

    ax.set_xticks(x)
    ax.set_xticklabels(stores, fontsize=_FS - 2, rotation=30, ha="right")
    ax.set_title("Выручка и прибыль по складам", fontsize=_FS + 2, pad=10)
    ax.set_ylabel("Рублей", fontsize=_FS)
    ax.legend(fontsize=_FS - 1)
    return _save(fig)


def chart_losses_vs_revenue(
    conn, d_from: date, d_to: date, store_name: str | None = None
) -> bytes | None:
    """Комбо: выручка (линия) + списания (столбцы) по дням."""
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

    fig, ax = _new_fig()
    ax.bar(all_days, loss_vals, color=_COL_LOSS, alpha=0.75, label="Списания", zorder=2)
    ax.plot(all_days, rev_vals, marker="o", ms=3, lw=2, color=_COL_REV, label="Выручка", zorder=3)
    ax.set_title("Выручка и списания по дням", fontsize=_FS + 2, pad=10)
    ax.set_xlabel("Дата", fontsize=_FS)
    ax.set_ylabel("Рублей", fontsize=_FS)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=40)
    ax.legend(fontsize=_FS - 1)
    return _save(fig)
