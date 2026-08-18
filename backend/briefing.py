"""Morning brief assembly.

Pulls signal from QB, Google Calendar, debt snapshots, and approval queue
into a single Spanish digest message. Fails soft — any source that errors
is reported as "no disponible" rather than killing the whole brief.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import approvals
from db import connect
from integrations import google_workspace as gw
from integrations import quickbooks

TZ = ZoneInfo("America/Puerto_Rico")
DAY_NAMES_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTH_NAMES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _today_label() -> str:
    now = datetime.now(TZ)
    return f"{DAY_NAMES_ES[now.weekday()]} {now.day} de {MONTH_NAMES_ES[now.month - 1]}"


def _safe(label: str, fn):
    try:
        return fn()
    except Exception as e:
        return {"_error": f"{label}: {type(e).__name__}: {e}"[:200]}


# ---- Section builders -------------------------------------------------------

def _cash_section() -> dict:
    """Bank total now + payments collected yesterday + last 7 days."""
    bal = _safe("balance", quickbooks.balance_summary)
    if "_error" in bal:
        return bal
    today = datetime.now(TZ).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()
    with connect() as conn:
        y_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS t FROM payments_received "
            "WHERE payment_date = ?", (yesterday,),
        ).fetchone()["t"]
        wk_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS t FROM payments_received "
            "WHERE payment_date >= ?", (week_ago,),
        ).fetchone()["t"]
    return {
        "bank": bal["bank_total"],
        "ar": bal["accounts_receivable"],
        "collected_yesterday": round(y_total, 2),
        "collected_last_7": round(wk_total, 2),
    }


def _calendar_section() -> dict:
    events = _safe("calendar", gw.list_events_today)
    if isinstance(events, dict) and "_error" in events:
        return events
    return {"events": events}


def _tasks_section() -> dict:
    return _safe("tasks", gw.list_tasks_due_today_or_overdue)


def _debt_section() -> dict:
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(snapshot_date) AS d FROM debt_snapshots"
        ).fetchone()["d"]
        if not latest:
            return {"snapshot_date": None, "invoices": 0, "families": 0, "total": 0.0}
        rows = conn.execute(
            "SELECT d.invoice_id, d.invoice_balance_due, d.overdue_days, "
            "d.family_id, f.name AS family_name "
            "FROM debt_snapshots d LEFT JOIN families f ON f.id = d.family_id "
            "WHERE d.snapshot_date = ?", (latest,),
        ).fetchall()
        balances: dict[str, float] = {}
        family_ids: set[int] = set()
        most_overdue = []
        seen_inv: set[str] = set()
        for r in rows:
            inv_id = r["invoice_id"]
            if r["invoice_balance_due"] is not None and inv_id not in seen_inv:
                balances[inv_id] = r["invoice_balance_due"]
                seen_inv.add(inv_id)
                if r["family_id"]:
                    family_ids.add(r["family_id"])
                most_overdue.append({
                    "family": r["family_name"] or "(sin enlazar)",
                    "balance": r["invoice_balance_due"],
                    "days": r["overdue_days"] or 0,
                })
        most_overdue.sort(key=lambda x: x["days"], reverse=True)
        return {
            "snapshot_date": latest,
            "invoices": len(balances),
            "families": len(family_ids),
            "total": round(sum(balances.values()), 2),
            "top": most_overdue[:5],
        }


def _queue_section() -> dict:
    items = _safe("queue", approvals.list_pending)
    if isinstance(items, dict) and "_error" in items:
        return items
    return {"count": len(items), "items": items[:5]}


# ---- Public API -------------------------------------------------------------

def build_morning_brief() -> dict:
    return {
        "date_label": _today_label(),
        "calendar": _calendar_section(),
        "tasks": _tasks_section(),
        "debt": _debt_section(),
    }


def render_brief_text(brief: dict) -> str:
    """Render the brief as a Telegram-friendly plain-text Spanish message."""
    lines = [f"☀️ *Buenos días — {brief['date_label']}*", ""]

    # Calendar
    cal = brief["calendar"]
    if "_error" in cal:
        lines.append(f"📅 *Hoy:* no disponible ({cal['_error']})")
    elif not cal["events"]:
        lines.append("📅 *Hoy:* sin eventos")
    else:
        lines.append("📅 *Hoy*")
        for e in cal["events"]:
            if e["all_day"]:
                lines.append(f"   • {e['title']} (todo el día)")
            else:
                t = e["start"][11:16] if len(e["start"]) >= 16 else e["start"]
                lines.append(f"   • {t} — {e['title']}")
    lines.append("")

    # Tasks (Google Tasks — overdue + due today)
    t = brief["tasks"]
    if "_error" in t:
        lines.append(f"✅ *Tareas:* no disponible ({t['_error']})")
    elif not t.get("overdue") and not t.get("today"):
        lines.append("✅ *Tareas:* nada pendiente para hoy")
    else:
        lines.append("✅ *Tareas*")
        for x in t.get("overdue", []):
            lines.append(f"   ⚠️ {x['title']} (vencida {x['due']})")
        for x in t.get("today", []):
            lines.append(f"   • {x['title']}")
    lines.append("")

    # Debts
    d = brief["debt"]
    if d.get("snapshot_date") is None:
        lines.append("📋 *Deudas:* sin datos (corre el scraper)")
    else:
        lines.append(f"📋 *Deudas* (snapshot {d['snapshot_date']})")
        lines.append(f"   Total: ${d['total']:,.2f} en {d['invoices']} facturas, {d['families']} familias")
        if d.get("top"):
            lines.append("   Más atrasadas:")
            for t in d["top"]:
                lines.append(f"   • {t['family']}: ${t['balance']:,.2f} ({t['days']}d)")

    return "\n".join(lines)
