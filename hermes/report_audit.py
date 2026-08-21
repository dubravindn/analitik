"""Аудит важных изменений документов из МойСклад.

Приёмки, платежи, расходные ордера и торговые документы остаются в общем
контроле изменений и удалений. Документы списания намеренно исключены.

Для заказов покупателей и отгрузок используется настоящий журнал ``/audit``.
Показываются только изменение даты документа и снижение фактической цены позиции
ниже действующей цены «Наличка». Количество, резерв, статус и добавление/удаление
позиций намеренно скрыты.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import time

from .moysklad import MoyskladClient

_DOC_TYPES = {
    "retaildemand": "Розничные продажи",
    "salesreturn":  "Возвраты покупателей",
    "supply":       "Приёмки",
    "paymentin":    "Входящие платежи",
    "paymentout":   "Исходящие платежи",
    "cashin":       "Приходные ордера",
    "cashout":      "Расходные ордера",
}

_CUSTOMER_ORDER = "customerorder"
_DEMAND = "demand"
_CUSTOMER_ORDER_LABEL = "Заказ покупателя"
_DEMAND_LABEL = "Отгрузка"
_AUDIT_ENTITY_LABELS = {
    _CUSTOMER_ORDER: _CUSTOMER_ORDER_LABEL,
    _DEMAND: _DEMAND_LABEL,
}
_MAX_AUDIT_EVENTS = 100
_MAX_ORDER_EVENTS = _MAX_AUDIT_EVENTS  # совместимость со старыми тестами
_MAX_POSITION_CHANGES = 12
_AUDIT_PAGE_SIZE = 100
# Берём последние 100 записей КАЖДОГО дня и типа. Этого достаточно, чтобы
# события начала недели не вытеснялись концом периода, и не создаёт сотни
# параллельных запросов, на которые МойСклад отвечает 429.
_MAX_AUDIT_SUMMARIES_PER_DAY = 100
_AUDIT_REQUEST_PAUSE_SECONDS = 0.15
# В служебных карточках встречается sentinel 99 999 999 999 ₽.
# Цена товара выше миллиона рублей не считается валидной «Наличкой».
_MAX_CASH_PRICE_RUB = 1_000_000

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


def _money_rub(value: float) -> str:
    """Короткая сумма в рублях без потери копеек."""
    if abs(value - round(value)) < 0.005:
        return f"{round(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _effective_line_price(value: dict) -> float | None:
    """Фактическая цена единицы с учётом скидки строки документа."""
    try:
        price = float(value.get("price"))
        discount = float(value.get("discount") or 0)
    except (TypeError, ValueError):
        return None
    return round(price * (1 - discount / 100), 2)


def _cash_price_rub(
    client: MoyskladClient, value: dict, cache: dict[str, float | None],
) -> float | None:
    """Текущая цена «Наличка» из карточки товара, в рублях."""
    assortment = value.get("assortment") or {}
    href = ((assortment.get("meta") or {}).get("href") or "").split("?")[0]
    if not href:
        return None
    if href in cache:
        return cache[href]

    marker = "/api/remap/1.2"
    path = href.split(marker, 1)[-1] if marker in href else ""
    if not path.startswith("/"):
        cache[href] = None
        return None
    try:
        card = client._get(path, {})
        value_kop = next(
            (
                float(price.get("value"))
                for price in card.get("salePrices", []) or []
                if (price.get("priceType") or {}).get("name") == "Наличка"
                and price.get("value") is not None
            ),
            None,
        )
        result = round(value_kop / 100, 2) if value_kop and value_kop > 0 else None
        if result is not None and result > _MAX_CASH_PRICE_RUB:
            result = None
    except Exception:
        result = None
    cache[href] = result
    return result


def _position_diff_lines(
    changes: list[dict], client: MoyskladClient | None = None,
    cash_cache: dict[str, float | None] | None = None,
) -> list[str]:
    """Показать только реальное снижение цены ниже «Налички»."""
    if client is None:
        return []
    cache = cash_cache if cash_cache is not None else {}
    lines: list[str] = []
    for item in changes:
        old = item.get("oldValue")
        new = item.get("newValue")
        # Добавление и удаление позиций намеренно не показываем.
        if not old or not new:
            continue
        old_name = _position_name(old)
        new_name = _position_name(new)
        if old_name != new_name:
            continue

        old_price = _effective_line_price(old)
        new_price = _effective_line_price(new)
        if old_price is None or new_price is None or new_price >= old_price:
            continue
        cash_price = _cash_price_rub(client, new, cache)
        if cash_price is None or new_price >= cash_price:
            continue

        discount_note = ""
        if float(new.get("discount") or 0) > 0:
            discount_note = f" после скидки {float(new['discount']):g}%"
        lines.append(
            f"«{new_name}»: цена{discount_note} снижена "
            f"{_money_rub(old_price)} → {_money_rub(new_price)} ₽; "
            f"«Наличка» {_money_rub(cash_price)} ₽ "
            f"(ниже на {_money_rub(cash_price - new_price)} ₽)"
        )
        if len(lines) >= _MAX_POSITION_CHANGES:
            break
    return lines


def _diff_lines(
    diff: dict, *, entity_type: str = _CUSTOMER_ORDER,
    client: MoyskladClient | None = None,
    cash_cache: dict[str, float | None] | None = None,
) -> list[str]:
    """Даты + снижение цены ниже «Налички»; остальной шум скрыт."""
    lines: list[str] = []
    date_fields = ["moment"]
    if entity_type == _CUSTOMER_ORDER:
        date_fields.append("deliveryPlannedMoment")
    for field in date_fields:
        change = diff.get(field)
        if not isinstance(change, dict):
            continue
        old = change.get("oldValue")
        new = change.get("newValue")
        if old == new:
            continue
        if field == "moment":
            label = "Дата отгрузки" if entity_type == _DEMAND else "Дата заказа"
        else:
            label = "Плановая дата доставки"
        lines.append(
            f"{label}: {_audit_value(old, field)} → {_audit_value(new, field)}"
        )
    positions = diff.get("positions")
    if isinstance(positions, list):
        lines.extend(_position_diff_lines(positions, client, cash_cache))
    return lines


def _load_sales_document_audit(
    client: MoyskladClient, d_from: date, d_to: date,
    entity_types: tuple[str, ...] = (_CUSTOMER_ORDER, _DEMAND),
) -> tuple[list[dict], int, bool]:
    """Получить важные update-события заказов и отгрузок.

    Период фильтруется по ``audit.moment`` (времени изменения), а не по дате
    самого документа. Поэтому изменённая сегодня отгрузка прошлой датой
    обязательно попадёт в сегодняшний отчёт.
    """
    result: list[dict] = []
    seen: set[tuple] = set()
    cash_cache: dict[str, float | None] = {}
    truncated = False

    # Не берём одни «последние 100» на весь период: насыщенный поздний день
    # иначе вытесняет важную правку из начала недели. Читаем каждый день постранично.
    summaries_by_id: dict[str, dict] = {}
    day = d_from
    while day <= d_to:
        for entity_type in entity_types:
            audit_filter = (
                f"moment>={day.isoformat()} 00:00:00;"
                f"moment<={day.isoformat()} 23:59:59;"
                f"entityType={entity_type};eventType=update"
            )
            offset = 0
            total_size = 0
            while offset < _MAX_AUDIT_SUMMARIES_PER_DAY:
                page = client._get("/audit", {
                    "filter": audit_filter,
                    "limit": _AUDIT_PAGE_SIZE,
                    "offset": offset,
                    "order": "moment,desc",
                })
                batch = page.get("rows", [])
                total_size = int(page.get("meta", {}).get("size", len(batch)) or 0)
                for summary in batch:
                    summary_id = str(summary.get("id") or "")
                    if summary_id:
                        summaries_by_id[summary_id] = summary
                offset += len(batch)
                if not batch or offset >= total_size:
                    break
            if offset < total_size:
                truncated = True
        day += timedelta(days=1)

    # Раскрытие audit/{id}/events — сетевой N+1. Читаем последовательно с
    # ограничением частоты: параллельные запросы стабильно получают HTTP 429.
    event_pages: dict[str, list[dict]] = {}

    def _fetch_events(summary_id: str) -> tuple[str, list[dict]]:
        rows = client._get(
            f"/audit/{summary_id}/events", {"limit": 100},
        ).get("rows", [])
        # Лимит API общий для аккаунта; выдерживаем паузу даже в одном потоке.
        time.sleep(_AUDIT_REQUEST_PAUSE_SECONDS)
        return summary_id, rows

    for summary_id in summaries_by_id:
        try:
            fetched_id, rows = _fetch_events(summary_id)
            event_pages[fetched_id] = rows
        except Exception:
            truncated = True

    allowed_entity_types = set(entity_types)
    for summary_id, summary in summaries_by_id.items():
        for event in event_pages.get(summary_id, []):
            entity_type = event.get("entityType")
            if entity_type not in allowed_entity_types:
                continue
            if (event.get("eventType") or summary.get("eventType")) != "update":
                continue
            diff = event.get("diff") or {}
            details = _diff_lines(
                diff, entity_type=entity_type,
                client=client, cash_cache=cash_cache,
            )
            if not details:
                continue
            dedup_key = (
                entity_type,
                event.get("moment") or summary.get("moment") or "",
                event.get("uid") or summary.get("uid") or "—",
                event.get("name") or "—",
                tuple(details),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            result.append({
                "event_type": "update",
                "entity_type": entity_type,
                "moment": event.get("moment") or summary.get("moment") or "",
                "uid": event.get("uid") or summary.get("uid") or "—",
                "number": event.get("name") or "—",
                "diff": diff,
                "details": details,
            })

    result.sort(key=lambda row: str(row.get("moment") or ""), reverse=True)
    meaningful_total = len(result)
    if meaningful_total > _MAX_AUDIT_EVENTS:
        truncated = True
    return result[:_MAX_AUDIT_EVENTS], meaningful_total, truncated


def _load_customer_order_audit(
    client: MoyskladClient, d_from: date, d_to: date,
) -> tuple[list[dict], int, bool]:
    """Совместимая обёртка: только заказы покупателей."""
    return _load_sales_document_audit(
        client, d_from, d_to, entity_types=(_CUSTOMER_ORDER,),
    )


def _render_sales_document_audit(
    rows: list[dict], total: int, truncated: bool = False,
) -> list[str]:
    if not rows:
        return []
    count_label = f"не менее {total}" if truncated else str(total)
    lines = [f"🧾 ЗАКАЗЫ И ОТГРУЗКИ — ВАЖНЫЕ ИЗМЕНЕНИЯ ({count_label}):"]

    prepared: list[tuple[dict, str, list[str], tuple]] = []
    groups: dict[tuple, list[tuple[dict, str, list[str], tuple]]] = {}
    for row in rows:
        event_dt = _dt(str(row.get("moment") or ""))
        event_label = event_dt.strftime("%d.%m.%Y %H:%M") if event_dt else "—"
        entity_type = row.get("entity_type") or _CUSTOMER_ORDER
        details = row.get("details") or _diff_lines(
            row.get("diff") or {}, entity_type=entity_type,
        )
        key = (
            entity_type, event_label, row.get("uid") or "—",
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
            entity_type = row.get("entity_type") or _CUSTOMER_ORDER
            plural = "отгрузок" if entity_type == _DEMAND else "заказов"
            lines.append(
                f"  • Массовое изменение: {len(same)} {plural} · изменены "
                f"{event_label} · кто: {row.get('uid') or '—'}"
            )
            numbers = [str(item[0].get("number") or "—") for item in same]
            for start in range(0, len(numbers), 15):
                prefix = "Номера: " if start == 0 else "        "
                lines.append(f"      – {prefix}{', '.join(numbers[start:start + 15])}")
            lines.extend(f"      – {detail}" for detail in details)
            continue

        entity_type = row.get("entity_type") or _CUSTOMER_ORDER
        document_label = _AUDIT_ENTITY_LABELS.get(entity_type, "Документ")
        lines.append(
            f"  • {document_label} № {row.get('number') or '—'} · "
            f"изменён {event_label} · кто: {row.get('uid') or '—'}"
        )
        lines.extend(f"      – {detail}" for detail in details)
    if truncated:
        lines.append(
            f"  … показаны последние {_MAX_AUDIT_EVENTS} значимых событий"
        )
    return lines


def _render_customer_order_audit(
    rows: list[dict], total: int, truncated: bool = False,
) -> list[str]:
    """Совместимая обёртка для старых вызовов."""
    return _render_sales_document_audit(rows, total, truncated)


def build_audit_report(client: MoyskladClient, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines = [f"🔍 Изменения документов за {period_str}", ""]

    if client is None:
        lines.append("⚠️ МойСклад недоступен — данные об изменениях не получены.")
        return "\n".join(lines)

    sales_audit: list[dict] = []
    sales_audit_total = 0
    sales_audit_truncated = False
    sales_audit_error = ""
    try:
        sales_audit, sales_audit_total, sales_audit_truncated = (
            _load_sales_document_audit(client, d_from, d_to)
        )
    except Exception as exc:
        # Шумный updated-fallback для заказов/отгрузок не используем:
        # без diff нельзя доказать ни изменение даты, ни низкую цену.
        sales_audit_error = type(exc).__name__

    # Общий контроль документов. Списаний в _DOC_TYPES нет: endpoint loss не
    # запрашивается и документ не может попасть в отчёт.
    del_flt = (
        f"deletedMoment>={d_from.isoformat()} 00:00:00;"
        f"deletedMoment<={d_to.isoformat()} 23:59:59"
    )
    deleted: list[tuple] = []
    for doc_type, label in _DOC_TYPES.items():
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
        for row in rows:
            deleted_dt = _dt(str(row.get("deletedMoment") or row.get("moment", "")))
            deleted.append((
                deleted_dt.isoformat() if deleted_dt else "",
                deleted_dt.strftime("%d.%m.%Y") if deleted_dt else "—",
                label, _number(row), _location(row), row.get("sum"), _owner(row),
            ))

    doc_flt = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )
    modified: list[tuple] = []
    for doc_type, label in _DOC_TYPES.items():
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
        for row in rows:
            moment_dt = _dt(str(row.get("moment", "")))
            updated_dt = _dt(str(row.get("updated", "")))
            if moment_dt and updated_dt and (updated_dt - moment_dt) >= _EDIT_GAP:
                modified.append((
                    moment_dt.strftime("%d.%m.%Y"),
                    updated_dt.strftime("%d.%m.%Y"),
                    label, _number(row), _location(row), row.get("sum"), _owner(row),
                ))

    if not sales_audit and not deleted and not modified:
        if sales_audit_error:
            lines.append(
                "⚠️ Детализация заказов и отгрузок временно недоступна "
                f"({sales_audit_error})."
            )
            return "\n".join(lines)
        lines.append("✅ За период важных изменений и удалений документов нет.")
        return "\n".join(lines)

    if sales_audit:
        lines.extend(_render_sales_document_audit(
            sales_audit, sales_audit_total, sales_audit_truncated,
        ))
        lines.append("")
    elif sales_audit_error:
        lines.append(
            "⚠️ Детализация заказов и отгрузок временно недоступна "
            f"({sales_audit_error})."
        )
        lines.append("")

    if deleted:
        deleted.sort(key=lambda item: item[0])
        lines.append(f"🗑 УДАЛЁННЫЕ ({len(deleted)}):")
        for _sort, day, label, number, location, sum_kop, who in deleted:
            lines.append(
                f"  • {label} № {number} · {day} · {location} · "
                f"{_rub(sum_kop)} · {who}"
            )
        lines.append("")

    if modified:
        modified.sort(key=lambda item: item[0])
        lines.append(f"✏️ ИЗМЕНЁННЫЕ ({len(modified)}):")
        for moment_day, updated_day, label, number, location, sum_kop, who in modified:
            lines.append(
                f"  • {label} № {number} · {moment_day} · {location} · "
                f"{_rub(sum_kop)} · {who} (изм. {updated_day})"
            )

    return "\n".join(lines).rstrip()
