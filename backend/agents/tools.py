"""Tool definitions exposed to Claude.

Read-only tools execute immediately and return data.
Action tools go through `propose_action` → approval queue → executor.
"""
from typing import Any

import approvals
from db import connect
from integrations import google_workspace as gw, quickbooks

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
        "name": "list_current_debtors",
        "description": (
            "Return who currently owes money per CollegeOne's official Debtors Detailed Report "
            "(latest scrape). Use this for 'who owes money right now' / 'quién debe ahora'. "
            "More authoritative than list_overdue_tuition because it reflects the live CollegeOne state, "
            "not the last items-balance CSV import."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "recent_payments",
        "description": (
            "List payments received from CollegeOne. Filter by date range and/or method (Cash, "
            "Bank Account, Check, Credit Card). Use for 'what came in this week / month' / 'cuánto "
            "hemos cobrado en efectivo'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "YYYY-MM-DD inclusive lower bound."},
                "until": {"type": "string", "description": "YYYY-MM-DD inclusive upper bound."},
                "method": {"type": "string", "description": "Cash | Bank Account | Check | Credit Card"},
            },
            "required": [],
        },
    },
    {
        "name": "qb_status",
        "description": "Check whether QuickBooks is connected and ready.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "qb_balance_summary",
        "description": (
            "QuickBooks BOOK BALANCE summary: sum of QB ledger transactions per "
            "account type — NOT the live bank balance. When reporting to Ivan, "
            "label it explicitly as 'saldo en libros (QB)' or 'book balance'. "
            "If he asks 'cuánto tengo en el banco' / 'what's in the bank', "
            "always clarify: 'En QB tienes X — este es el saldo en libros, no "
            "el saldo real del banco. Para el real, mira tu app del banco.' "
            "Returns: bank_total, accounts_receivable, accounts_payable, "
            "credit_cards, and per-type breakdown — all from the QB ledger."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "qb_list_accounts",
        "description": (
            "List QuickBooks chart of accounts (id, name, type, current balance). "
            "Use to look up account names before proposing categorizations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    },
    {
        "name": "qb_recent_transactions",
        "description": (
            "Recent QuickBooks purchases/expenses (last N days). Returns date, "
            "amount, payee, account categorization, memo. Use for 'what did I "
            "spend on X' / 'show me last week's expenses'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30},
                "max_results": {"type": "integer", "default": 100},
            },
            "required": [],
        },
    },
    {
        "name": "qb_open_invoices",
        "description": (
            "Outstanding invoices in QuickBooks (Balance > 0). Returns customer, "
            "total, balance, due date. Use for 'what invoices are still open in QB' "
            "(separate from CollegeOne debtors — these are anything QB tracks)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "qb_bookkeeper_run",
        "description": (
            "Apply payee categorization rules: scan recent QB transactions in "
            "generic/wrong accounts and queue qb.categorize_txn approvals ONLY "
            "for payees that have an existing rule. Does NOT guess. Payees "
            "with no rule are ignored — use `qb_list_unruled_payees` to see "
            "them and `qb_set_payee_rule` to teach the system. Use when Ivan "
            "says 'aplica las reglas' or 'corre el bookkeeper'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 30},
                "since": {"type": "string", "description": "YYYY-MM-DD lower bound"},
            },
            "required": [],
        },
    },
    {
        "name": "qb_list_unruled_payees",
        "description": (
            "Show payees that appear in recent generic-categorized QB "
            "transactions but don't have a categorization rule yet. Sorted by "
            "frequency. Use to identify the highest-leverage payees Ivan "
            "should set rules for next. Returns payee, txn_count, "
            "total_amount, currently_in account."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 200},
                "since": {"type": "string", "description": "YYYY-MM-DD lower bound"},
            },
            "required": [],
        },
    },
    {
        "name": "qb_list_payee_rules",
        "description": "Show all currently-defined payee categorization rules.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "qb_set_payee_rule",
        "description": (
            "Set a categorization rule for a payee. mode='always' auto-queues "
            "qb.categorize_txn → the chosen account (Ivan still approves each "
            "individually). mode='ask' queues with no account; Ivan picks per "
            "transaction at approval time. mode='skip' ignores this payee "
            "entirely. For 'always' you must supply qb_account_id and "
            "qb_account_name — look these up via qb_list_accounts first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payee": {"type": "string"},
                "mode": {"type": "string", "enum": ["always", "ask", "skip"]},
                "qb_account_id": {"type": "string"},
                "qb_account_name": {"type": "string"},
            },
            "required": ["payee", "mode"],
        },
    },
    {
        "name": "qb_delete_payee_rule",
        "description": "Remove a payee rule so the payee goes back to being ignored.",
        "input_schema": {
            "type": "object",
            "properties": {"payee": {"type": "string"}},
            "required": ["payee"],
        },
    },
    {
        "name": "qb_profit_and_loss",
        "description": (
            "P&L report from QuickBooks. Returns income, expenses, net income for "
            "the date range. Defaults to year-to-date if no dates. Use for 'how "
            "much did we make this month' / 'P&L de este semestre'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "end": {"type": "string", "description": "YYYY-MM-DD inclusive"},
            },
            "required": [],
        },
    },
    {
        "name": "schedule_event",
        "description": (
            "Create a Google Calendar event IMMEDIATELY in Ivan's admi.simplicity@gmail.com "
            "calendar (no approval queue — runs right away, syncs to iPhone via Google "
            "Calendar app). Use for meetings, parent calls, school events, deadlines. Times "
            "must be ISO 8601 in his local Puerto Rico time (no timezone suffix needed). "
            "After this runs you can truthfully tell Ivan the event was created and include "
            "the link from the response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601, e.g. '2026-05-30T14:30'"},
                "duration_minutes": {"type": "integer", "default": 60},
                "notes": {"type": "string"},
                "location": {"type": "string"},
                "calendar_id": {"type": "string", "description": "Calendar ID. Defaults to 'primary'."},
            },
            "required": ["title", "start"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Create a Google Tasks item IMMEDIATELY (no approval queue — runs right away). "
            "Appears in Gmail/Calendar sidebar and the Google Tasks app on iPhone. Use for "
            "follow-ups, to-dos, tasks with due dates (calling parents, filing paperwork, "
            "etc.). Google Tasks only honors the date portion of 'due', not the time. "
            "After this runs you can truthfully tell Ivan the task was created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": "string", "description": "Optional ISO 8601 due date (e.g. '2026-05-30')."},
                "notes": {"type": "string"},
                "tasklist_id": {"type": "string", "description": "Defaults to '@default'."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Compose an email DRAFT in Gmail (admi.simplicity@gmail.com) for Ivan's approval. "
            "The draft is created in Gmail but NEVER auto-sent — Ivan reviews and clicks Send "
            "himself. After approval the bot returns a link to open the draft directly. Use "
            "for messages to parents, vendors, payment confirmations, etc. Body should be "
            "plain text (no HTML). Be polite and professional, default to Spanish unless "
            "context says otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text email body."},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
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
    if name == "list_current_debtors":
        return _list_current_debtors()
    if name == "recent_payments":
        return _recent_payments(args.get("since"), args.get("until"), args.get("method"))
    if name == "qb_status":
        return {"connected": quickbooks.is_connected(),
                "environment": quickbooks._env()}
    if name == "qb_balance_summary":
        return quickbooks.balance_summary()
    if name == "qb_list_accounts":
        return quickbooks.list_accounts(active_only=args.get("active_only", True))
    if name == "qb_recent_transactions":
        return quickbooks.list_recent_transactions(
            days=args.get("days", 30),
            max_results=args.get("max_results", 100),
        )
    if name == "qb_open_invoices":
        return quickbooks.list_open_invoices()
    if name == "qb_profit_and_loss":
        return quickbooks.profit_and_loss(start=args.get("start"), end=args.get("end"))
    if name == "qb_bookkeeper_run":
        import bookkeeper
        return bookkeeper.propose_categorizations(
            limit=args.get("limit", 30), since=args.get("since"),
        )
    if name == "qb_list_unruled_payees":
        import bookkeeper
        return bookkeeper.list_unruled_payees(
            limit=args.get("limit", 200), since=args.get("since"),
        )
    if name == "qb_list_payee_rules":
        import bookkeeper
        return {"rules": bookkeeper.list_rules()}
    if name == "qb_set_payee_rule":
        import bookkeeper
        return bookkeeper.set_rule(
            payee=args["payee"], mode=args["mode"],
            qb_account_id=args.get("qb_account_id"),
            qb_account_name=args.get("qb_account_name", ""),
        )
    if name == "qb_delete_payee_rule":
        import bookkeeper
        return {"deleted": bookkeeper.delete_rule(args["payee"])}
    if name == "schedule_event":
        # Auto-execute — calendar events are reversible (just delete in Calendar)
        return gw.add_event(
            title=args["title"],
            start=args["start"],
            duration_minutes=args.get("duration_minutes", 60),
            notes=args.get("notes", ""),
            calendar_id=args.get("calendar_id", gw.DEFAULT_CALENDAR_ID),
            location=args.get("location", ""),
        )
    if name == "create_reminder":
        # Auto-execute — tasks are reversible (just check off / delete)
        return gw.add_task(
            title=args["title"],
            due=args.get("due"),
            notes=args.get("notes", ""),
            tasklist_id=args.get("tasklist_id", gw.DEFAULT_TASKLIST_ID),
        )
    if name == "draft_email":
        qid = approvals.enqueue(
            agent="messaging",
            action_type="email.draft",
            summary=f"Email draft to {args['to']}: {args['subject']}",
            payload=args,
        )
        return {"queued": True, "queue_id": qid, "status": "pending_approval"}
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


def _list_current_debtors() -> dict:
    """From debt_snapshots — uses the most recent snapshot only.

    total_owed = sum of invoice_balance_due across DISTINCT invoices (the real
    outstanding balance, after subtracting partial payments).
    """
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(snapshot_date) AS d FROM debt_snapshots"
        ).fetchone()["d"]
        if not latest:
            return {"error": "no debt snapshots yet — run the CollegeOne scraper first"}
        rows = conn.execute(
            "SELECT d.invoice_id, d.item_name, d.item_amount, d.tax, "
            "d.invoice_date, d.due_date, d.overdue_days, d.school_year, "
            "d.invoice_subtotal, d.invoice_payments_applied, d.invoice_balance_due, "
            "s.id AS student_id, s.name AS student_name, s.grade, "
            "f.id AS family_id, f.name AS family_name, f.phone, f.whatsapp "
            "FROM debt_snapshots d "
            "LEFT JOIN students s ON s.id = d.student_id "
            "LEFT JOIN families f ON f.id = d.family_id "
            "WHERE d.snapshot_date = ? "
            "ORDER BY f.name, d.due_date",
            (latest,),
        ).fetchall()
        items = [dict(r) for r in rows]

        # Sum balance_due per DISTINCT invoice (denormalized across items)
        invoice_balances: dict[str, float] = {}
        for r in items:
            if r["invoice_balance_due"] is not None:
                invoice_balances[r["invoice_id"]] = r["invoice_balance_due"]
        total_owed = round(sum(invoice_balances.values()), 2)

        return {
            "snapshot_date": latest,
            "item_count": len(items),
            "invoice_count": len(invoice_balances),
            "family_count": len({r["family_id"] for r in items if r["family_id"]}),
            "total_owed": total_owed,
            "total_billed_gross": round(sum(r["item_amount"] for r in items), 2),
            "items": items,
        }


def _recent_payments(since: str | None, until: str | None, method: str | None) -> dict:
    with connect() as conn:
        sql = (
            "SELECT p.collegeone_payment_id, p.payment_date, p.customer_name, "
            "p.payment_method, p.amount, p.status, p.invoice_id, p.section, "
            "f.id AS family_id, f.name AS family_name, "
            "s.id AS student_id, s.name AS student_name, s.grade "
            "FROM payments_received p "
            "LEFT JOIN families f ON f.id = p.family_id "
            "LEFT JOIN students s ON s.id = p.student_id "
            "WHERE 1=1"
        )
        params: list = []
        if since:
            sql += " AND p.payment_date >= ?"
            params.append(since)
        if until:
            sql += " AND p.payment_date <= ?"
            params.append(until)
        if method:
            sql += " AND p.payment_method = ?"
            params.append(method)
        sql += " ORDER BY p.payment_date DESC, p.collegeone_payment_id DESC"
        rows = conn.execute(sql, params).fetchall()
        items = [dict(r) for r in rows]

        by_method: dict[str, dict] = {}
        for r in items:
            m = r["payment_method"] or "(unknown)"
            slot = by_method.setdefault(m, {"count": 0, "total": 0.0})
            slot["count"] += 1
            slot["total"] += r["amount"]
        for slot in by_method.values():
            slot["total"] = round(slot["total"], 2)

        return {
            "filters": {"since": since, "until": until, "method": method},
            "count": len(items),
            "total": round(sum(r["amount"] for r in items), 2),
            "by_method": by_method,
            "payments": items,
        }


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
