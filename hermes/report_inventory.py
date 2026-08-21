"""Инвентаризации по точкам: списание, оприходование и проверяемая оценка.

Обычная порча на рознице не считается инвентаризацией. Корректировка
определяется по паре документов ``enter`` + технический ``loss`` в тот же день;
для БАЗЫ все списания остаются учётными корректировками по принятому правилу.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import calc, config


INVENTORY_STORES = tuple(
    getattr(config, "INVENTORY_STORES", ()) or (
        "База Воровского 107/1",
        "Розница Воровского 107/1",
        "Киров, Ленина 102А",
        "Слободской, Советская 64",
    )
)
_MIN_TECHNICAL_LOSS_POSITIONS = 20
_SUSPICIOUS_POSITION_QTY = 100_000


def _rub(kop: float) -> str:
    return f"{float(kop) / 100:,.0f}".replace(",", " ")


def _qty(value: float) -> str:
    value = float(value or 0)
    return (f"{value:,.1f}" if value % 1 else f"{int(value):,}").replace(",", " ")


def _doc_rows(conn, d_from: date, d_to: date) -> list[dict[str, Any]]:
    discount_ids = list(calc.discount_product_ids(conn)) or ["__none__"]
    rows: list[dict[str, Any]] = []
    for kind, doc_table, item_table in (
        ("loss", "loss_doc", "loss_item"),
        ("enter", "enter_doc", "enter_item"),
    ):
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT d.doc_id, d.day, d.moment, d.store_name,
                       COALESCE(d.description, ''), COALESCE(d.project_name, ''),
                       COUNT(i.position_id), COALESCE(SUM(i.qty), 0),
                       COALESCE(SUM(i.total_kop), 0),
                       COALESCE(SUM(
                           CASE WHEN pp.price_kop IS NOT NULL AND pp.price_kop > 0
                                THEN round(i.qty * pp.price_kop *
                                     CASE WHEN i.product_id = ANY(%s)
                                          THEN %s::numeric ELSE 1.0 END)
                                ELSE i.total_kop END
                       ), 0),
                       COUNT(*) FILTER (
                           WHERE (pp.price_kop IS NULL OR pp.price_kop <= 0)
                             AND COALESCE(i.total_kop, 0) <= 0
                       ),
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object('product', i.product_name, 'qty', i.qty)
                               ORDER BY i.qty DESC
                           ) FILTER (WHERE i.qty >= %s),
                           '[]'::jsonb
                       )
                FROM {doc_table} d
                JOIN {item_table} i ON i.doc_id = d.doc_id
                LEFT JOIN LATERAL (
                    SELECT price_kop FROM purchase_price_asof p
                    WHERE p.product_id = i.product_id AND p.priced_from <= d.day
                    ORDER BY p.priced_from DESC LIMIT 1
                ) pp ON true
                WHERE d.day BETWEEN %s AND %s AND d.store_name = ANY(%s)
                GROUP BY d.doc_id, d.day, d.moment, d.store_name,
                         d.description, d.project_name
                ORDER BY d.day, d.store_name, d.moment
            """, (
                discount_ids, config.SUPPLIER_DISCOUNT_MULTIPLIER, _SUSPICIOUS_POSITION_QTY,
                d_from, d_to, list(INVENTORY_STORES),
            ))
            for row in cur.fetchall():
                rows.append({
                    "kind": kind, "doc_id": row[0], "day": row[1],
                    "moment": row[2], "store": row[3], "description": row[4],
                    "project": row[5], "positions": int(row[6] or 0),
                    "qty": float(row[7] or 0), "raw_kop": int(row[8] or 0),
                    "value_kop": int(row[9] or 0), "missing_prices": int(row[10] or 0),
                    "quantity_anomalies": [
                        {"product": item.get("product") or "—", "qty": float(item.get("qty") or 0)}
                        for item in (row[11] or [])
                    ],
                })
    return rows


def _is_inventory_loss(row: dict[str, Any], enter_days: set[tuple[str, date]]) -> bool:
    if row["store"] in set(config.ADJUSTMENT_STORES or ()):
        return True
    # Розничная инвентаризация выгружается отдельным большим техническим
    # списанием с нулевой стоимостью позиции. Ежедневную порчу так не помечаем.
    technical = row["raw_kop"] == 0 and row["positions"] >= _MIN_TECHNICAL_LOSS_POSITIONS
    return technical or (
        (row["store"], row["day"]) in enter_days
        and row["raw_kop"] == 0
        and row["positions"] >= 10
    )


def load_inventory_sessions(conn, d_from: date, d_to: date) -> list[dict[str, Any]]:
    rows = _doc_rows(conn, d_from, d_to)
    enter_days = {
        (row["store"], row["day"]) for row in rows if row["kind"] == "enter"
    }
    selected = [
        row for row in rows
        if row["kind"] == "enter" or _is_inventory_loss(row, enter_days)
    ]
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        grouped[(row["store"], row["day"])].append(row)

    sessions: list[dict[str, Any]] = []
    for (store, day), docs in grouped.items():
        loss = [row for row in docs if row["kind"] == "loss"]
        enter = [row for row in docs if row["kind"] == "enter"]
        loss_kop = sum(row["value_kop"] for row in loss)
        enter_kop = sum(row["value_kop"] for row in enter)
        descriptions = sorted({
            row["description"].strip() for row in docs if row["description"].strip()
        })
        sessions.append({
            "store": store, "day": day, "documents": docs,
            "loss_docs": loss, "enter_docs": enter,
            "loss_kop": loss_kop, "enter_kop": enter_kop,
            "net_kop": enter_kop - loss_kop,
            "loss_qty": sum(row["qty"] for row in loss),
            "enter_qty": sum(row["qty"] for row in enter),
            "missing_prices": sum(row["missing_prices"] for row in docs),
            "descriptions": descriptions,
            "quantity_anomalies": [
                {
                    **item, "document_type": row["kind"],
                    "document_id": row["doc_id"],
                }
                for row in docs for item in row["quantity_anomalies"]
            ],
        })
    return sorted(sessions, key=lambda row: (row["day"], row["store"]), reverse=True)


def _document_name(client, kind: str, doc_id: str) -> str:
    if client is not None:
        try:
            card = client._get(f"/entity/{kind}/{doc_id}", {})
            return str(card.get("name") or card.get("externalCode") or doc_id[:8])
        except Exception:
            pass
    return doc_id[:8]


def _assessment(session: dict[str, Any]) -> tuple[str, str]:
    loss = session["loss_kop"]
    enter = session["enter_kop"]
    missing = session["missing_prices"]
    if session.get("quantity_anomalies"):
        anomalies = session["quantity_anomalies"][:3]
        anomaly_text = "; ".join(
            f"«{item['product']}» — {_qty(item['qty'])} ед." for item in anomalies
        )
        result = (
            f"Критическая аномалия ввода: {anomaly_text} Денежный итог инвентаризации считать "
            "недостоверным до исправления документа."
        )
    elif missing:
        result = (
            f"Стоимость {missing} поз. не определена; денежный итог неполный. "
            "Сначала заполнить закупочные цены."
        )
    elif loss > enter:
        result = (
            f"Недостача по учёту: фактически товара меньше на {_rub(loss - enter)} ₽."
        )
    elif enter > loss:
        result = (
            f"Излишек по учёту: фактически товара больше на {_rub(enter - loss)} ₽."
        )
    else:
        result = "Денежная корректировка сбалансирована."

    if session["loss_docs"] and session["enter_docs"]:
        check = (
            "Есть корректировки в обе стороны: проверить пересортицу, единицы измерения "
            "и несвоевременные приёмки/перемещения. Это гипотезы, а не установленная причина."
        )
    elif session["loss_docs"]:
        check = (
            "Только списание: сверить недостачу, ранее не проведённую порчу и перемещения. "
            "Причина без первичных документов не установлена."
        )
    else:
        check = (
            "Только оприходование: проверить неучтённую приёмку или ошибку прошлого остатка. "
            "Причина без первичных документов не установлена."
        )
    return result, check


def build_inventory_report(
    conn, d_from: date, d_to: date, client=None,
) -> str:
    """Текст для отдельной страницы утверждённого PDF «Отчёт за период»."""
    sessions = load_inventory_sessions(conn, d_from, d_to)
    period = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from:%d.%m.%Y}–{d_to:%d.%m.%Y}"
    )
    lines = [
        f"📋 Инвентаризации по точкам · {period}",
        "Списание = фактически меньше учётного остатка; оприходование = фактически больше.",
        "Итог = оприходовано − списано. Это корректировка учёта, не продажа и не обычная порча.",
        "",
    ]
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        by_store[session["store"]].append(session)

    for store in INVENTORY_STORES:
        store_sessions = by_store.get(store, [])
        lines.append(f"── {store} ──")
        if not store_sessions:
            lines.append("За выбранный период инвентаризация не найдена.")
            lines.append("")
            continue
        for session in store_sessions:
            lines.append(f"▸ {session['day']:%d.%m.%Y}")
            for kind, label in (("loss", "Списание"), ("enter", "Оприходование")):
                docs = session[f"{kind}_docs"]
                if not docs:
                    lines.append(f"{label}: документа нет")
                    continue
                for doc in docs:
                    number = _document_name(client, kind, doc["doc_id"])
                    lines.append(
                        f"{label} № {number}: {doc['positions']} поз. · "
                        f"{_qty(doc['qty'])} ед. · {_rub(doc['value_kop'])} ₽"
                    )
            sign = "+" if session["net_kop"] > 0 else ("−" if session["net_kop"] < 0 else "")
            lines.append(f"Итог корректировки: {sign}{_rub(abs(session['net_kop']))} ₽")
            result, check = _assessment(session)
            lines.append(f"Что не так: {result}")
            if session["descriptions"]:
                lines.append(f"Указанная причина: {'; '.join(session['descriptions'])}")
            else:
                lines.append("Указанная причина: в документах МойСклад не заполнена.")
            lines.append(f"Автоматическая оценка: {check}")
            lines.append("")
    lines.append(
        "ИИ получает эти же цифры как отдельные факты и даёт мнение только с их подтверждением."
    )
    return "\n".join(lines)


def inventory_fact_rows(conn, d_from: date, d_to: date) -> list[dict[str, Any]]:
    """Структурированные строки для payload ИИ без повторного толкования формул."""
    sessions = load_inventory_sessions(conn, d_from, d_to)
    rows: list[dict[str, Any]] = []
    for session in sessions:
        result, check = _assessment(session)
        rows.append({**session, "assessment": result, "check": check})

    # Еженедельная дисциплина: проверяем последние 7 дней на конец отчёта.
    weekly_from = d_to - timedelta(days=6)
    weekly = sessions if d_from <= weekly_from else load_inventory_sessions(conn, weekly_from, d_to)
    latest = {store: None for store in INVENTORY_STORES}
    for session in weekly:
        if session["store"] in latest and (
            latest[session["store"]] is None or session["day"] > latest[session["store"]]
        ):
            latest[session["store"]] = session["day"]
    for store, day in latest.items():
        rows.append({"store": store, "weekly_latest": day, "weekly_from": weekly_from, "weekly_to": d_to})
    return rows
