"""Аудит изменений: удалённые и изменённые документы из МойСклад.

Владелец должен видеть, что за период кто-то удалил или переписал документ.
Формат — плоский список: тип документа · дата · склад · сумма · кто.
Склад: у части документов (списания, поставки, продажи) берётся из поля store,
у расходных/платёжных — из project. Если ни того, ни другого нет — «(без склада)».
«Кто» — ответственный сотрудник (owner) документа.
"""
from __future__ import annotations

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
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def build_audit_report(client: MoyskladClient, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines = [f"🔍 Изменения документов за {period_str}", ""]

    # ── Удалённые (deletedMoment в периоде) ─────────────────────────────────────
    del_flt = (
        f"deletedMoment>={d_from.isoformat()} 00:00:00;"
        f"deletedMoment<={d_to.isoformat()} 23:59:59"
    )
    deleted: list[tuple] = []   # (day, label, loc, sum_kop, who)
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
        for r in rows:
            d_dt = _dt(str(r.get("deletedMoment") or r.get("moment", "")))
            day = d_dt.strftime("%d.%m.%Y") if d_dt else "—"
            sort_key = d_dt.isoformat() if d_dt else ""
            deleted.append((sort_key, day, label, _location(r), r.get("sum"), _owner(r)))

    # ── Изменённые (updated позже moment на _EDIT_GAP) ──────────────────────────
    doc_flt = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )
    modified: list[tuple] = []   # (moment_day, updated_day, label, loc, sum_kop, who)
    for doc_type, label in _DOC_TYPES.items():
        try:
            resp = client._get(f"/entity/{doc_type}", {
                "filter": doc_flt,
                "limit": 100,
                "order": "moment,asc",
                "expand": "store,project,owner",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        for r in rows:
            m_dt = _dt(str(r.get("moment", "")))
            u_dt = _dt(str(r.get("updated", "")))
            if m_dt and u_dt and (u_dt - m_dt) >= _EDIT_GAP:
                modified.append((
                    m_dt.strftime("%d.%m.%Y"), u_dt.strftime("%d.%m.%Y"),
                    label, _location(r), r.get("sum"), _owner(r),
                ))

    if not deleted and not modified:
        lines.append("✅ За период изменённых и удалённых документов нет.")
        return "\n".join(lines)

    if deleted:
        deleted.sort(key=lambda x: x[0])
        lines.append(f"🗑 УДАЛЁННЫЕ ({len(deleted)}):")
        for _sort, day, label, loc, sum_kop, who in deleted:
            lines.append(f"  • {label} · {day} · {loc} · {_rub(sum_kop)} · {who}")
        lines.append("")

    if modified:
        modified.sort(key=lambda x: x[0])
        lines.append(f"✏️ ИЗМЕНЁННЫЕ ({len(modified)}):")
        for m_day, u_day, label, loc, sum_kop, who in modified:
            lines.append(
                f"  • {label} · {m_day} · {loc} · {_rub(sum_kop)} · {who} "
                f"(изм. {u_day})"
            )

    return "\n".join(lines).rstrip()
