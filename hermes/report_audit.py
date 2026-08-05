"""Аудит изменений: удалённые и изменённые документы из МойСклад.

Группировка по складу: у части документов (списания, поставки, продажи) склад
берётся из поля store, у расходных/платёжных — из project. Если ни того, ни
другого нет — «(без склада)».
"""
from __future__ import annotations

from datetime import date

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


def _location(doc: dict) -> str:
    """Склад документа: store.name → project.name → «(без склада)»."""
    store = doc.get("store")
    if isinstance(store, dict) and store.get("name"):
        return store["name"]
    project = doc.get("project")
    if isinstance(project, dict) and project.get("name"):
        return project["name"]
    return _NO_STORE


def build_audit_report(client: MoyskladClient, d_from: date, d_to: date) -> str:
    period_str = (
        d_from.strftime("%d.%m.%Y") if d_from == d_to
        else f"{d_from.strftime('%d.%m.%Y')} – {d_to.strftime('%d.%m.%Y')}"
    )
    lines = [f"🔍 Изменения документов за {period_str}", ""]

    # ── Удалённые ──────────────────────────────────────────────────────────────
    del_flt = (
        f"deletedMoment>={d_from.isoformat()} 00:00:00;"
        f"deletedMoment<={d_to.isoformat()} 23:59:59"
    )
    # loc → label → [(moment, name), ...]
    deleted: dict[str, dict[str, list]] = {}
    for doc_type, label in _DOC_TYPES.items():
        try:
            resp = client._get(f"/entity/{doc_type}/deleted", {
                "filter": del_flt,
                "limit": 100,
                "order": "deletedMoment,asc",
                "expand": "store,project",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        for r in rows:
            loc = _location(r)
            moment = str(r.get("moment", ""))[:10]
            name = r.get("name") or r.get("id", "—")
            deleted.setdefault(loc, {}).setdefault(label, []).append((moment, name))

    if deleted:
        lines.append("🗑 УДАЛЁННЫЕ ДОКУМЕНТЫ:")
        for loc in sorted(deleted, key=lambda x: (x == _NO_STORE, x)):
            lines.append(f"  📍 {loc}:")
            for label, items in deleted[loc].items():
                lines.append(f"      {label}: {len(items)} шт.")
                for moment, name in items:
                    lines.append(f"        • {moment}  {name}")
    else:
        lines.append("✅ Удалённых документов за период не найдено.")
    lines.append("")

    # ── Изменённые (updated > moment + 1 мин) ─────────────────────────────────
    doc_flt = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )
    # loc → label → [(moment_day, updated_day, name), ...]
    modified: dict[str, dict[str, list]] = {}
    for doc_type, label in _DOC_TYPES.items():
        try:
            resp = client._get(f"/entity/{doc_type}", {
                "filter": doc_flt,
                "limit": 100,
                "order": "moment,asc",
                "expand": "store,project",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        for r in rows:
            moment  = str(r.get("moment",  ""))[:19].replace("T", " ")
            updated = str(r.get("updated", ""))[:19].replace("T", " ")
            # Изменён если updated на 2+ минуты позже moment (автообновление ~0с)
            if updated > moment[:16] + "1":
                loc = _location(r)
                name = r.get("name") or r.get("id", "—")
                modified.setdefault(loc, {}).setdefault(label, []).append(
                    (moment[:10], updated[:10], name)
                )

    if modified:
        lines.append("✏️ ИЗМЕНЁННЫЕ ДОКУМЕНТЫ:")
        for loc in sorted(modified, key=lambda x: (x == _NO_STORE, x)):
            lines.append(f"  📍 {loc}:")
            for label, items in modified[loc].items():
                lines.append(f"      {label}: {len(items)} шт.")
                for moment_day, updated_day, name in items:
                    lines.append(f"        • {moment_day}  {name}  (изм. {updated_day})")
    else:
        lines.append("✅ Изменённых документов за период не найдено.")

    return "\n".join(lines)
