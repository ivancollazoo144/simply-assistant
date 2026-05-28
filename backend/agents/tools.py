"""Tool definitions exposed to Claude.

Read-only tools execute immediately and return data.
Action tools go through `propose_action` → approval queue → executor.
"""
from typing import Any

import approvals
from db import connect
from integrations import quickbooks

# Spanish grade order (Kinder → Duodecimo). Used for grade-based sorting.
GRADE_ORDER = [
    "Kinder", "Primero", "Segundo", "Tercero", "Cuarto", "Quinto",
    "Sexto", "Septimo", "Octavo", "Noveno", "Decimo", "Undecimo", "Duodecimo",
]
GRADE_RANK = {g.lower(): i for i, g in enumerate(GRADE_ORDER)}


TOOLS = [
    {
        "name": "school_stats",
        "description": (
            "Return exact counts: total families, total students, students per grade. "
            "USE THIS FIRST whenever Ivan asks 'how many...' anything — never count yourself."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_families",
        "description": "List all families. Returns name, primary contact, phone, and student count per family.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_family",
        "description": (
            "Search families and students by name (fuzzy, case-insensitive). "
            "Matches against parent name OR student name. Returns each matching family with its "
            "students and recent tuition/payment status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Any part of a parent or student name"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_students_by_grade",
        "description": "List all students in a given grade (Spanish names: Kinder, Primero, Segundo, etc).",
        "input_schema": {
            "type": "object",
            "properties": {"grade": {"type": "string"}},
            "required": ["grade"],
        },
    },
    {
        "name": "list_overdue_tuition",
        "description": (
            "List all past-due or partially-paid tuition items. Optionally filter by period (YYYY-MM) "
            "or semester ('fall' = Aug-Dec, 'spring' = Jan-May)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "YYYY-MM, e.g. '2026-05'. Omit for all open."},
                "semester": {"type": "string", "enum": ["fall", "spring"]},
                "year": {"type": "integer", "description": "Required if semester is given. Calendar year of semester start."},
            },
            "required": [],
        },
    },
    {
        "name": "payment_summary",
        "description": (
            "Roll up total billed, paid, and outstanding by item type for a period or semester. "
            "Use this when Ivan asks 'how much have we collected in tuition this semester?' etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "semester": {"type": "string", "enum": ["fall", "spring"]},
                "year": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "family_balance",
        "description": "Show a single family's full payment status — every item, paid or owed, across the year.",
        "input_schema": {
            "type": "object",
            "properties": {"family_id": {"type": "integer", "description": "Internal DB id"}},
            "required": ["family_id"],
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
            "Propose an action that modifies external state (QB write, message draft, record edit). "
            "Creates an approval-queue entry — Ivan reviews before it executes. Use this for: "
            "creating QB invoices, categorizing transactions, drafting WhatsApp/email messages, "
            "any change to a family/student record beyond pure lookup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["bookkeeper", "payments", "records", "messaging"]},
                "action_type": {
                    "type": "string",
                    "description": "e.g. qb.create_invoice, qb.categorize_txn, message.whatsapp_draft, record.update_family",
                },
                "summary": {"type": "string", "description": "One-line human description Ivan will see in the queue."},
                "payload": {"type": "object", "description": "Structured data for the action."},
            },
            "required": ["agent", "action_type", "summary", "payload"],
        },
    },
]


def dispatch(name: str, args: dict) -> Any:
    if name == "school_stats":
        return _school_stats()
    if name == "list_families":
        return _list_families()
    if name == "find_family":
        return _find_family(args["query"])
    if name == "list_students_by_grade":
        return _list_students_by_grade(args["grade"])
    if name == "list_overdue_tuition":
        return _list_overdue(args.get("period"), args.get("semester"), args.get("year"))
    if name == "payment_summary":
        return _payment_summary(args.get("period"), args.get("semester"), args.get("year"))
    if name == "family_balance":
        return _family_balance(args["family_id"])
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


# ---- Helpers ----------------------------------------------------------------

def _semester_periods(semester: str, year: int) -> list[str]:
    """fall(year=2025) → ['2025-08'..'2025-12']; spring(year=2025) → ['2026-01'..'2026-05'].

    Convention: 'fall 2025' = Aug-Dec 2025 (start of school year 2025-26).
                'spring 2025' = Jan-May 2026 (end of school year 2025-26).
    Ivan thinks in school years; we follow Aug-Dec, Jan-May split.
    """
    if semester == "fall":
        return [f"{year}-{m:02d}" for m in range(8, 13)]
    return [f"{year + 1}-{m:02d}" for m in range(1, 6)]


def _resolve_periods(period: str | None, semester: str | None, year: int | None) -> list[str] | None:
    if period:
        return [period]
    if semester and year:
        return _semester_periods(semester, year)
    return None  # caller treats as "all"


# ---- Tool implementations ---------------------------------------------------

def _school_stats() -> dict:
    with connect() as conn:
        total_families = conn.execute("SELECT COUNT(*) AS n FROM families").fetchone()["n"]
        total_students = conn.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
        by_grade_rows = conn.execute(
            "SELECT grade, COUNT(*) AS n FROM students GROUP BY grade"
        ).fetchall()
        by_grade = {r["grade"] or "(no grade)": r["n"] for r in by_grade_rows}
        by_grade_sorted = dict(sorted(
            by_grade.items(),
            key=lambda kv: GRADE_RANK.get(kv[0].lower(), 999),
        ))
        siblings = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "SELECT family_id FROM students GROUP BY family_id HAVING COUNT(*) > 1"
            ") sub"
        ).fetchone()["n"]
        return {
            "total_families": total_families,
            "total_students": total_students,
            "families_with_multiple_students": siblings,
            "students_by_grade": by_grade_sorted,
        }


def _list_families() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.id, f.collegeone_family_id, f.name, f.phone, f.email, "
            "COUNT(s.id) AS student_count "
            "FROM families f LEFT JOIN students s ON s.family_id = f.id "
            "GROUP BY f.id ORDER BY f.name"
        ).fetchall()
        return [dict(r) for r in rows]


def _find_family(query: str) -> dict:
    pattern = f"%{query}%"
    with connect() as conn:
        # Match against family name OR any student name in that family
        fam_rows = conn.execute(
            "SELECT DISTINCT f.* FROM families f "
            "LEFT JOIN students s ON s.family_id = f.id "
            "WHERE f.name LIKE ? OR s.name LIKE ? LIMIT 10",
            (pattern, pattern),
        ).fetchall()

        matches = []
        for f in fam_rows:
            students = conn.execute(
                "SELECT id, name, grade, collegeone_student_no FROM students WHERE family_id = ?",
                (f["id"],),
            ).fetchall()
            student_ids = [s["id"] for s in students]
            recent = []
            if student_ids:
                placeholders = ",".join("?" * len(student_ids))
                recent = conn.execute(
                    f"SELECT s.name AS student_name, t.item_name, t.period, "
                    f"t.item_price, t.amount_paid, t.invoice_status "
                    f"FROM tuition_charges t JOIN students s ON s.id = t.student_id "
                    f"WHERE t.student_id IN ({placeholders}) "
                    f"ORDER BY t.period DESC LIMIT 12",
                    student_ids,
                ).fetchall()
            matches.append({
                "family": dict(f),
                "students": [dict(s) for s in students],
                "recent_charges": [dict(r) for r in recent],
            })
        return {"matches": matches, "count": len(matches)}


def _list_students_by_grade(grade: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.grade, f.name AS family_name, f.phone "
            "FROM students s JOIN families f ON f.id = s.family_id "
            "WHERE LOWER(s.grade) = LOWER(?) ORDER BY s.name",
            (grade,),
        ).fetchall()
        return [dict(r) for r in rows]


def _list_overdue(period: str | None, semester: str | None, year: int | None) -> dict:
    periods = _resolve_periods(period, semester, year)
    with connect() as conn:
        sql = (
            "SELECT t.id, t.item_name, t.item_price, t.amount_paid, t.period, "
            "t.invoice_status, s.name AS student_name, s.grade, "
            "f.id AS family_id, f.name AS family_name, f.phone, f.whatsapp "
            "FROM tuition_charges t "
            "JOIN students s ON s.id = t.student_id "
            "JOIN families f ON f.id = s.family_id "
            "WHERE t.invoice_status IN ('Past Due', 'Partially Paid')"
        )
        params: list = []
        if periods:
            placeholders = ",".join("?" * len(periods))
            sql += f" AND t.period IN ({placeholders})"
            params.extend(periods)
        sql += " ORDER BY f.name, t.period"
        rows = conn.execute(sql, params).fetchall()

        items = [dict(r) for r in rows]
        total_owed = sum(r["item_price"] - r["amount_paid"] for r in items)
        unique_families = len({r["family_id"] for r in items})
        return {
            "item_count": len(items),
            "family_count": unique_families,
            "total_owed": round(total_owed, 2),
            "items": items,
        }


def _payment_summary(period: str | None, semester: str | None, year: int | None) -> dict:
    periods = _resolve_periods(period, semester, year)
    with connect() as conn:
        sql = (
            "SELECT item_name, "
            "COUNT(*) AS charge_count, "
            "SUM(item_price) AS total_billed, "
            "SUM(amount_paid) AS total_paid "
            "FROM tuition_charges WHERE invoice_status != 'cancelled'"
        )
        params: list = []
        if periods:
            placeholders = ",".join("?" * len(periods))
            sql += f" AND period IN ({placeholders})"
            params.extend(periods)
        sql += " GROUP BY item_name ORDER BY total_billed DESC"
        rows = conn.execute(sql, params).fetchall()

        by_item = [
            {
                "item": r["item_name"],
                "count": r["charge_count"],
                "billed": round(r["total_billed"] or 0, 2),
                "paid": round(r["total_paid"] or 0, 2),
                "outstanding": round((r["total_billed"] or 0) - (r["total_paid"] or 0), 2),
            }
            for r in rows
        ]
        totals = {
            "billed": round(sum(r["billed"] for r in by_item), 2),
            "paid": round(sum(r["paid"] for r in by_item), 2),
            "outstanding": round(sum(r["outstanding"] for r in by_item), 2),
        }
        return {"scope": {"period": period, "semester": semester, "year": year},
                "by_item": by_item, "totals": totals}


def _family_balance(family_id: int) -> dict:
    with connect() as conn:
        fam = conn.execute("SELECT * FROM families WHERE id = ?", (family_id,)).fetchone()
        if not fam:
            return {"error": "family not found"}
        students = conn.execute(
            "SELECT id, name, grade FROM students WHERE family_id = ?", (family_id,)
        ).fetchall()
        student_ids = [s["id"] for s in students]
        charges = []
        if student_ids:
            placeholders = ",".join("?" * len(student_ids))
            charges = conn.execute(
                f"SELECT s.name AS student_name, t.item_name, t.period, "
                f"t.item_price, t.amount_paid, t.invoice_status "
                f"FROM tuition_charges t JOIN students s ON s.id = t.student_id "
                f"WHERE t.student_id IN ({placeholders}) "
                f"ORDER BY s.name, t.period",
                student_ids,
            ).fetchall()
        return {
            "family": dict(fam),
            "students": [dict(s) for s in students],
            "charges": [dict(c) for c in charges],
            "total_outstanding": round(
                sum((c["item_price"] - c["amount_paid"]) for c in charges
                    if c["invoice_status"] in ("Past Due", "Partially Paid")), 2),
        }
