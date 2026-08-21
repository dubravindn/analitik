"""Аудит изменений: удалённые и изменённые документы из МойСклад.

Владелец должен видеть, что за период кто-то удалил или переписал документ.
Формат — плоский список: тип документа · дата · склад · сумма · кто.
Склад: у части документов (списания, поставки, продажи) берётся из поля store,
у расходных/платёжных — из project. Если ни того, ни другого нет — «(без склада)».
«Кто» — ответственный сотрудник (owner) документа.

Для заказов покупателей используется настоящий журнал ``/audit``: он отдаёт
пользователя, время события и ``diff`` со значениями «было → стало». Поэтому
для заказов показываем не косвенный признак ``updated``, а конкретные изменения
даты, статуса, склада, проекта, суммы и состава позиций.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .moysklad import MoyskladClient

_DOC_TYPES = {
    "retaildemand": "Розничные продажи",
    "salesreturn":  "Возвраты покупателей",
    "loss":         "Списания",
    "supply":       "Поставки",
    "cashout":      "Расходные ордера",
    "paymentout":   "Исходящие платежи",
}

_CUSTOMER_ORDER = "customerorder"
_CUSTOMER_ORDER_LABEL = "Заказ покупателя"
_MAX_ORDER_EVENTS = 100
_MAX_POSITION_CHANGES = 12

_FIELD_LABELS = {
    "moment": "Дата заказа",
    "deliveryPlannedMoment": "Плановая дата доставки",
    "state": "Статус",
    "store": "Склад",
    "project": "Проект",
    "agent": "Клиент",
    "organization": "Организация",
    "owner": "Владелец",
    "sum": "Сумма",
    "description": "Комментарий",
    "applicable": "Проведён",
    "shared": "Общий доступ",
    "rate": "Курс",
}

_POSITION_FIELDS = {
    "quantity": "количество",
    "reserve": "резерв",
    "price": "цена",
    "discount": "скидка",
    "vat": "НДС",
}

_NO_STORE = "(без склада)"
# updated считается «изменением», если он позже moment минимум на столько
# (МойСклад ставит updated ≈ moment при создании; правки — позже на минуты).
_EDIT_GAP = timedelta(minutes=2)


def _location(doc: dict) -> str:
    """Склад документа: store.name → project.name → «(без склада)»."""
    store = doc.get("store")
    if isinstance(store, dict) and store.get("name"):
        return store["name"]
    project = doc.get("project")
    if isinstance(project, dict) and project.get("name"):
        return project["name"]
    return _NO_STORE


def _owner(doc: dict) -> str:
    owner = doc.get("owner")
    if isinstance(owner, dict) and owner.get("name"):
        return owner["name"]
    return "—"


def _number(doc: dict) -> str:
    """Номер документа МойСклад; ID используем только как последний фолбэк."""
    value = doc.get("name") or doc.get("externalCode") or doc.get("id") or "—"
    return str(value)


def _rub(sum_kop) -> str:
    try:
        return f"{float(sum_kop) / 100:,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return "—"


def _dt(s: str) -> datetime | None:
    """Разобрать момент МойСклад «YYYY-MM-DD HH:MM:SS[.ms]» в datetime."""
    if not s:
        return None
    s = s[:19].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _audit_value(value, field: str = "") -> str:
    """Коротко представить значение из diff журнала МойСклад."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, dict):
        if value.get("name"):
            return str(value["name"])
        if field == "rate" and value.get("rate") is not None:
            return str(value["rate"])
        return str(value.get("id") or "—")
    if field == "sum" and isinstance(value, (int, float)):
        return f"{float(value):,.2f} ₽".replace(",", " ").replace(".00", "")
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    parsed = _dt(text)
    if parsed:
        return parsed.strftime("%d.%m.%Y %H:%M")
    return text.replace("\n", " ")[:160]


def _position_name(value: dict | None) -> str:
    value = value or {}
    assortment = value.get("assortment") or {}
    return str(assortment.get("name") or "позиция без названия")


def _position_diff_lines(changes: list[dict]) -> list[str]:
    """Расшифровать изменения строк заказа, не раздувая PDF бесконечно."""
    lines: list[str] = []
    for item in changes[:_MAX_POSITION_CHANGES]:
        old = item.get("oldValue")
        new = item.get("newValue")
        if not old and new:
            lines.append(
                f"добавлена позиция «{_position_name(new)}» "
                f"({_audit_value(new.get('quantity'))} шт.)"
            )
            continue
        if old and not new:
            lines.append(
                f"удалена позиция «{_position_name(old)}» "
                f"({_audit_value(old.get('quantity'))} шт.)"
            )
            continue
        old = old or {}
        new = new or {}
        product = _position_name(new or old)
        parts = []
        old_name = _position_name(old)
        new_name = _position_name(new)
        if old_name != new_name:
            parts.append(f"товар {old_name} → {new_name}")
        for key, label in _POSITION_FIELDS.items():
            if old.get(key) != new.get(key):
                suffix = " ₽" if key == "price" else ("%" if key in {"discount", "vat"} else "")
                parts.append(
                    f"{label} {_audit_value(old.get(key))}{suffix} → "
                    f"{_audit_value(new.get(key))}{suffix}"
                )
        lines.append(f"«{product}»: " + (", ".join(parts) or "изменена позиция"))
    if len(changes) > _MAX_POSITION_CHANGES:
        lines.append(
            f"… ещё {len(changes) - _MAX_POSITION_CHANGES} изменений позиций"
        )
    return lines


def _diff_lines(diff: dict, *, for_delete: bool = False) -> list[str]:
    lines: list[str] = []
    delete_fields = {
        "moment", "deliveryPlannedMoment", "state", "store", "project",
        "agent", "sum", "positions",
    }
    for field, change in diff.items():
        if for_delete and field not in delete_fields:
            continue
        if field == "positions" and isinstance(change, list):
            lines.extend(_position_diff_lines(change))
            continue
        if field not in _FIELD_LABELS:
            # Технические поля (externalCode, group, vatIncluded и т.п.)
            # не помогают управленческому контролю и перегружают страницу.
            continue
        if not isinstance(change, dict):
            continue
        old = change.get("oldValue")
        new = change.get("newValue")
        if old == new:
            continue
        label = _FIELD_LABELS.get(field, field)
        lines.append(
            f"{label}: {_audit_value(old, field)} → {_audit_value(new, field)}"
        )
    return lines


def _load_customer_order_audit(
    client: MoyskladClient, d_from: date, d_to: date,
) -> tuple[list[dict], int, bool]:
    """Получить update/delete заказов и раскрыть их auditevent.diff."""
    base_filter = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59;"
        f"entityType={_CUSTOMER_ORDER}"
    )
    summaries: list[dict] = []
    total_matching = 0
    # eventType разрешено указывать в фильтре лишь один раз, поэтому update и
    # delete читаем двумя короткими запросами. Так не выгружаем многочисленные
    # create/print-события и не замедляем PDF.
    for audit_event_type in ("update", "delete"):
        page = client._get("/audit", {
            "filter": f"{base_filter};eventType={audit_event_type}",
            "limit": _MAX_ORDER_EVENTS,
            "offset": 0,
            "order": "moment,desc",
        })
        batch = page.get("rows", [])
        total_matching += int(page.get("meta", {}).get("size", len(batch)) or 0)
        summaries.extend(batch[:_MAX_ORDER_EVENTS])

    summaries.sort(key=lambda row: str(row.get("moment") or ""), reverse=True)
    summaries = summaries[:_MAX_ORDER_EVENTS]

    result: list[dict] = []
    seen: set[tuple] = set()
    for summary in summaries:
        event_page = client._get(
            f"/audit/{summary['id']}/events", {"limit": 100},
        )
        for event in event_page.get("rows", []):
            if event.get("entityType") != _CUSTOMER_ORDER:
                continue
            event_type = event.get("eventType") or summary.get("eventType")
            if event_type not in {"update", "delete"}:
                continue
            diff = event.get("diff") or {}
            if event_type == "update" and not _diff_lines(diff):
                # МойСклад иногда регистрирует update без доступного diff.
                # Такой факт нельзя объяснить владельцу — не показываем шум.
                continue
            dedup_key = (
                event_type,
                event.get("moment") or summary.get("moment") or "",
                event.get("uid") or summary.get("uid") or "—",
                event.get("name") or "—",
                json.dumps(diff, ensure_ascii=False, sort_keys=True),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            result.append({
                "event_type": event_type,
                "moment": event.get("moment") or summary.get("moment") or "",
                "uid": event.get("uid") or summary.get("uid") or "—",
                "number": event.get("name") or "—",
                "diff": diff,
            })
    result.sort(key=lambda row: str(row.get("moment") or ""), reverse=True)
    meaningful_total = len(result)
    truncated = total_matching > len(summaries) or meaningful_total > _MAX_ORDER_EVENTS
    return result[:_MAX_ORDER_EVENTS], meaningful_total, truncated


def _render_customer_order_audit(
    rows: list[dict], total: int, truncated: bool = False,
) -> list[str]:
    if not rows:
        return []
    count_label = f"не менее {total}" if truncated else str(total)
    lines = [f"🧾 ЗАКАЗЫ ПОКУПАТЕЛЕЙ — ИСТОРИЯ ({count_label}):"]

    prepared: list[tuple[dict, str, list[str], tuple]] = []
    groups: dict[tuple, list[tuple[dict, str, list[str], tuple]]] = {}
    for row in rows:
        event_dt = _dt(str(row.get("moment") or ""))
        event_label = event_dt.strftime("%d.%m.%Y %H:%M") if event_dt else "—"
        details = _diff_lines(
            row.get("diff") or {},
            for_delete=row.get("event_type") == "delete",
        )
        key = (
            row.get("event_type"), event_label, row.get("uid") or "—",
            tuple(details),
        )
        item = (row, event_label, details, key)
        prepared.append(item)
        groups.setdefault(key, []).append(item)

    rendered_groups: set[tuple] = set()
    for row, event_label, details, key in prepared:
        same = groups[key]
        if len(same) >= 3:
            if key in rendered_groups:
                continue
            rendered_groups.add(key)
            action = "удалены" if row.get("event_type") == "delete" else "изменены"
            lines.append(
                f"  • Массовое изменение: {len(same)} заказов · {action} "
                f"{event_label} · кто: {row.get('uid') or '—'}"
            )
            numbers = [str(item[0].get("number") or "—") for item in same]
            for start in range(0, len(numbers), 15):
                prefix = "Номера: " if start == 0 else "        "
                lines.append(f"      – {prefix}{', '.join(numbers[start:start + 15])}")
            lines.extend(f"      – {detail}" for detail in details)
            continue

        action = "удалён" if row.get("event_type") == "delete" else "изменён"
        lines.append(
            f"  • {_CUSTOMER_ORDER_LABEL} № {row.get('number') or '—'} · "
            f"{action} {event_label} · кто: {row.get('uid') or '—'}"
        )
        if details:
            lines.extend(f"      – {detail}" for detail in details)
        elif action == "удалён":
            lines.append("      – документ удалён")
        else:
            lines.append("      – МойСклад не передал расшифровку полей")
    if truncated:
        lines.append(
            f"  … показаны последние {_MAX_ORDER_EVENTS} значимых событий"
        )
    return lines


def build_audit_report(client: MoyskladClient, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines = [f"🔍 Изменения документов за {period_str}", ""]

    if client is None:
        lines.append("⚠️ МойСклад недоступен — данные об изменениях не получены.")
        return "\n".join(lines)

    order_audit: list[dict] = []
    order_audit_total = 0
    order_audit_truncated = False
    order_audit_error = ""
    try:
        order_audit, order_audit_total, order_audit_truncated = _load_customer_order_audit(
            client, d_from, d_to,
        )
    except Exception as exc:
        # Старый updated-механизм ниже остаётся безопасным fallback: заказы
        # попадут хотя бы фактом изменения, даже если /audit временно недоступен.
        order_audit_error = type(exc).__name__

    doc_types = dict(_DOC_TYPES)
    if order_audit_error:
        doc_types[_CUSTOMER_ORDER] = "Заказы покупателей"

    # ── Удалённые (deletedMoment в периоде) ─────────────────────────────────────
    del_flt = (
        f"deletedMoment>={d_from.isoformat()} 00:00:00;"
        f"deletedMoment<={d_to.isoformat()} 23:59:59"
    )
    deleted: list[tuple] = []   # (sort, day, label, number, loc, sum_kop, who)
    for doc_type, label in doc_types.items():
        try:
            resp = client._get(f"/entity/{doc_type}/deleted", {
                "filter": del_flt,
                "limit": 100,
                "order": "deletedMoment,asc",
                "expand": "store,project,owner",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        for r in rows:
            d_dt = _dt(str(r.get("deletedMoment") or r.get("moment", "")))
            day = d_dt.strftime("%d.%m.%Y") if d_dt else "—"
            sort_key = d_dt.isoformat() if d_dt else ""
            deleted.append((
                sort_key, day, label, _number(r), _location(r),
                r.get("sum"), _owner(r),
            ))

    # ── Изменённые (updated позже moment на _EDIT_GAP) ──────────────────────────
    doc_flt = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )
    modified: list[tuple] = []   # (moment_day, updated_day, label, number, loc, sum_kop, who)
    for doc_type, label in doc_types.items():
        try:
            rows: list[dict] = []
            offset = 0
            while True:
                resp = client._get(f"/entity/{doc_type}", {
                    "filter": doc_flt,
                    "limit": 100,
                    "offset": offset,
                    "order": "moment,asc",
                    "expand": "store,project,owner",
                })
                batch = resp.get("rows", [])
                rows.extend(batch)
                size = resp.get("meta", {}).get("size", 0)
                offset += len(batch)
                if not batch or offset >= size:
                    break
        except Exception:
            continue
        for r in rows:
            m_dt = _dt(str(r.get("moment", "")))
            u_dt = _dt(str(r.get("updated", "")))
            if m_dt and u_dt and (u_dt - m_dt) >= _EDIT_GAP:
                modified.append((
                    m_dt.strftime("%d.%m.%Y"), u_dt.strftime("%d.%m.%Y"),
                    label, _number(r), _location(r), r.get("sum"), _owner(r),
                ))

    if not deleted and not modified and not order_audit:
        if order_audit_error:
            lines.append(
                "⚠️ Детализация заказов покупателей временно недоступна; "
                f"использован резервный контроль updated ({order_audit_error})."
            )
            return "\n".join(lines)
        lines.append("✅ За период изменённых и удалённых документов нет.")
        return "\n".join(lines)

    if order_audit:
        lines.extend(_render_customer_order_audit(
            order_audit, order_audit_total, order_audit_truncated,
        ))
        lines.append("")
    elif order_audit_error:
        lines.append(
            "⚠️ Детализация заказов покупателей временно недоступна; "
            f"использован резервный контроль updated ({order_audit_error})."
        )
        lines.append("")

    if deleted:
        deleted.sort(key=lambda x: x[0])
        lines.append(f"🗑 УДАЛЁННЫЕ ({len(deleted)}):")
        for _sort, day, label, number, loc, sum_kop, who in deleted:
            lines.append(
                f"  • {label} № {number} · {day} · {loc} · {_rub(sum_kop)} · {who}"
            )
        lines.append("")

    if modified:
        modified.sort(key=lambda x: x[0])
        lines.append(f"✏️ ИЗМЕНЁННЫЕ ({len(modified)}):")
        for m_day, u_day, label, number, loc, sum_kop, who in modified:
            lines.append(
                f"  • {label} № {number} · {m_day} · {loc} · {_rub(sum_kop)} · {who} "
                f"(изм. {u_day})"
            )

    return "\n".join(lines).rstrip()
