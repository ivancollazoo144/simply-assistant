"""Tool definitions exposed to Claude. Tools are split into:
- read-only: execute immediately
- action: produce an approval-queue entry, do NOT execute
"""
import json
from typing import Any

import approvals
from db import connect
from integrations import quickbooks

TOOLS = [
    {
        "name": "list_families",
        "description": "List all families in the school. Returns name, contact, # of students. Read-only.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_family",
        "description": "Look up a family by name (fuzzy match). Returns family record + students + recent tuition status.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_overdue_tuition",
        "description": "List families with overdue tuition for a given period or all open periods.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "description": "YYYY-MM, or omit for all open"}},
            "required": [],
        },
    },
    {
        "name": "qb_status",
        "description": "Check whether QuickBooks is connected and ready.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "propose_action",
        "description": (
            "Propose an action that will modify external state (QB write, message draft, etc). "
            "Creates an approval-queue entry — Ivan reviews before it executes. "
            "Use this for: creating QB invoices, categorizing transactions, drafting WhatsApp/email messages, "
            "any change to a family/student record beyond pure lookup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["bookkeeper", "payments", "records", "messaging"],
                },
                "action_type": {
                    "type": "string",
                    "description": "e.g. qb.create_invoice, qb.categorize_txn, message.whatsapp_draft, record.update_family",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line human description Ivan will see in the queue.",
                },
                "payload": {
                    "type": "object",
                    "description": "Structured data for the action (recipient, amount, category, message body, etc).",
                },
            },
            "required": ["agent", "action_type", "summary", "payload"],
        },
    },
]


def dispatch(name: str, args: dict) -> Any:
    if name == "list_families":
        return _list_families()
    if name == "find_family":
        return _find_family(args["name"])
    if name == "list_overdue_tuition":
        return _list_overdue(args.get("period"))
    if name == "qb_status":
        return {"connected": quickbooks.is_connected()}
    if name == "propose_action":
        qid = approvals.enqueue(
            agent=args["agent"],
            action_type=args["action_type"],
            summary=args["summary"],
            payload=args["payload"],
        )
        return {"queued": True, "queue_id": qid, "status": "pending_approval"}
    return {"error": f"unknown tool: {name}"}


def _list_families() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.id, f.name, f.phone, COUNT(s.id) AS student_count "
            "FROM families f LEFT JOIN students s ON s.family_id = f.id "
            "GROUP BY f.id ORDER BY f.name"
        ).fetchall()
        return [dict(r) for r in rows]


def _find_family(name: str) -> dict:
    pattern = f"%{name}%"
    with connect() as conn:
        fam = conn.execute(
            "SELECT * FROM families WHERE name LIKE ? LIMIT 5", (pattern,)
        ).fetchall()
        results = []
        for f in fam:
            students = conn.execute(
                "SELECT id, name, grade FROM students WHERE family_id = ?", (f["id"],)
            ).fetchall()
            tuition = conn.execute(
                "SELECT period, amount, paid_at FROM tuition_charges "
                "WHERE family_id = ? ORDER BY period DESC LIMIT 6",
                (f["id"],),
            ).fetchall()
            results.append({
                "family": dict(f),
                "students": [dict(s) for s in students],
                "recent_tuition": [dict(t) for t in tuition],
            })
        return {"matches": results}


def _list_overdue(period: str | None) -> list[dict]:
    with connect() as conn:
        if period:
            rows = conn.execute(
                "SELECT t.*, f.name AS family_name FROM tuition_charges t "
                "JOIN families f ON f.id = t.family_id "
                "WHERE t.period = ? AND t.paid_at IS NULL",
                (period,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.*, f.name AS family_name FROM tuition_charges t "
                "JOIN families f ON f.id = t.family_id "
                "WHERE t.paid_at IS NULL ORDER BY t.due_date"
            ).fetchall()
        return [dict(r) for r in rows]
