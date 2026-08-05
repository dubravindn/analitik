"""Аудит изменений: удалённые и изменённые документы из МойСклад."""
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
    any_deleted = False
    for doc_type, label in _DOC_TYPES.items():
        try:
            resp = client._get(f"/entity/{doc_type}/deleted", {
                "filter": del_flt,
                "limit": 100,
                "order": "deletedMoment,asc",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        if not rows:
            continue
        if not any_deleted:
            lines.append("🗑 УДАЛЁННЫЕ ДОКУМЕНТЫ:")
            any_deleted = True
        lines.append(f"  {label}: {len(rows)} шт.")
        for r in rows:
            moment = str(r.get("moment", ""))[:10]
            name   = r.get("name") or r.get("id", "—")
            lines.append(f"    • {moment}  {name}")

    if not any_deleted:
        lines.append("✅ Удалённых документов за период не найдено.")
    lines.append("")

    # ── Изменённые (updated > moment + 1 мин) ─────────────────────────────────
    doc_flt = (
        f"moment>={d_from.isoformat()} 00:00:00;"
        f"moment<={d_to.isoformat()} 23:59:59"
    )
    any_modified = False
    for doc_type, label in _DOC_TYPES.items():
        try:
            resp = client._get(f"/entity/{doc_type}", {
                "filter": doc_flt,
                "limit": 100,
                "order": "moment,asc",
            })
            rows = resp.get("rows", [])
        except Exception:
            continue
        modified = []
        for r in rows:
            moment  = str(r.get("moment",  ""))[:19].replace("T", " ")
            updated = str(r.get("updated", ""))[:19].replace("T", " ")
            # Изменён если updated на 2+ минуты позже moment (автообновление ~0с)
            if updated > moment[:16] + "1":
                modified.append({
                    "name":    r.get("name") or r.get("id", "—"),
                    "moment":  moment[:10],
                    "updated": updated[:10],
                })
        if not modified:
            continue
        if not any_modified:
            lines.append("✏️ ИЗМЕНЁННЫЕ ДОКУМЕНТЫ:")
            any_modified = True
        lines.append(f"  {label}: {len(modified)} шт.")
        for m in modified:
            lines.append(
                f"    • {m['moment']}  {m['name']}  (изм. {m['updated']})"
            )

    if not any_modified:
        lines.append("✅ Изменённых документов за период не найдено.")

    return "\n".join(lines)
