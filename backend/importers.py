"""CSV importers for CollegeOne reports.

Idempotent — re-running with updated CSV files updates existing rows by
collegeone_family_id / collegeone_student_no / (student, item, period).
"""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from db import connect

# CollegeOne reports have 9 metadata rows before the actual header
FAMILIES_HEADER_ROW = 10
ITEMS_HEADER_ROW = 10

MONTH_COLUMNS = ["Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
                 "Jan", "Feb", "Mar", "Apr", "May"]

# Map academic-year month label → YYYY-MM. School year runs Aug-May with
# Aug-Dec being the first semester (calendar year N) and Jan-May the second
# (calendar year N+1). The header item name (e.g. "MENSUALIDAD 2025-2026")
# carries the starting calendar year.
def month_to_period(month_label: str, school_year_start: int) -> str:
    """('Aug', 2025) → '2025-08'; ('Feb', 2025) → '2026-02'."""
    month_num = {m: i + 1 for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )}[month_label]
    # months Jan-Jul belong to the SECOND calendar year of the school year
    year = school_year_start if month_num >= 8 else school_year_start + 1
    return f"{year}-{month_num:02d}"


def _clean(s: str | None) -> str:
    return (s or "").strip()


def _normalize_phone(raw: str) -> str:
    """787-XXX-XXXX format. Handles 7878074, 787-528-8074, 7877029797, etc."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"{digits[1:4]}-{digits[4:7]}-{digits[7:11]}"
    return digits  # unusual length — keep as-is for manual review


def _parse_price(s: str) -> float:
    """'$295.00' → 295.0. '-' or '' → 0.0"""
    s = _clean(s)
    if not s or s == "-":
        return 0.0
    return float(re.sub(r"[^0-9.\-]", "", s))


def _extract_school_year(item_name: str) -> int | None:
    """'MENSUALIDAD 2025-2026 associated with Grade' → 2025."""
    m = re.search(r"(\d{4})-(\d{4})", item_name or "")
    return int(m.group(1)) if m else None


# ---- Families importer ------------------------------------------------------

def import_families(csv_path: Path | str) -> dict:
    """Import families_report.csv. Returns stats dict."""
    csv_path = Path(csv_path)
    families_seen = {}  # collegeone_family_id → row data (for dedupe)
    student_rows = []

    last_family_id = None  # CollegeOne leaves family fields blank for siblings
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader, start=1):
            if i <= FAMILIES_HEADER_ROW or len(row) < 12:
                continue
            family_id = _clean(row[0])
            student_no = _clean(row[4])
            if not student_no:
                continue  # blank row
            if family_id and not family_id.isdigit():
                continue  # skip "Total Students : 84" footer
            if not family_id:
                # Sibling row — inherit family from previous row
                if not last_family_id:
                    continue
                family_id = last_family_id
            else:
                last_family_id = family_id

            parent_name = _clean(row[1])
            email = _clean(row[2]).lower()
            phone = _normalize_phone(row[3])
            student_name = _clean(row[5])
            grade = _clean(row[6])
            group = _clean(row[7])
            teacher = _clean(row[8])
            gender = _clean(row[9])
            dob = _clean(row[10])

            # First row wins for family-level fields
            if family_id not in families_seen:
                families_seen[family_id] = {
                    "collegeone_family_id": family_id,
                    "name": parent_name,
                    "primary_contact": parent_name,
                    "phone": phone,
                    "whatsapp": phone,  # same per Ivan
                    "email": email,
                }
            student_rows.append({
                "collegeone_student_no": student_no,
                "collegeone_family_id": family_id,
                "name": student_name,
                "grade": grade,
                "student_group": group,
                "teacher": teacher,
                "gender": gender,
                "dob": dob,
            })

    families_added = 0
    families_updated = 0
    students_added = 0
    students_updated = 0
    now = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        # UPSERT families
        for fam in families_seen.values():
            cur = conn.execute(
                "SELECT id FROM families WHERE collegeone_family_id = ?",
                (fam["collegeone_family_id"],),
            ).fetchone()
            if cur:
                conn.execute(
                    "UPDATE families SET name=?, primary_contact=?, phone=?, "
                    "whatsapp=?, email=?, updated_at=? WHERE id=?",
                    (fam["name"], fam["primary_contact"], fam["phone"],
                     fam["whatsapp"], fam["email"], now, cur["id"]),
                )
                families_updated += 1
            else:
                conn.execute(
                    "INSERT INTO families (collegeone_family_id, name, "
                    "primary_contact, phone, whatsapp, email) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (fam["collegeone_family_id"], fam["name"],
                     fam["primary_contact"], fam["phone"],
                     fam["whatsapp"], fam["email"]),
                )
                families_added += 1

        # Lookup family DB IDs for student inserts
        fam_id_map = dict(conn.execute(
            "SELECT collegeone_family_id, id FROM families"
        ).fetchall())

        for st in student_rows:
            fam_db_id = fam_id_map.get(st["collegeone_family_id"])
            if not fam_db_id:
                continue
            cur = conn.execute(
                "SELECT id FROM students WHERE collegeone_student_no = ?",
                (st["collegeone_student_no"],),
            ).fetchone()
            if cur:
                conn.execute(
                    "UPDATE students SET family_id=?, name=?, grade=?, "
                    "student_group=?, teacher=?, gender=?, dob=?, updated_at=? "
                    "WHERE id=?",
                    (fam_db_id, st["name"], st["grade"], st["student_group"],
                     st["teacher"], st["gender"], st["dob"], now, cur["id"]),
                )
                students_updated += 1
            else:
                conn.execute(
                    "INSERT INTO students (collegeone_student_no, family_id, "
                    "name, grade, student_group, teacher, gender, dob) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (st["collegeone_student_no"], fam_db_id, st["name"],
                     st["grade"], st["student_group"], st["teacher"],
                     st["gender"], st["dob"]),
                )
                students_added += 1

    return {
        "families_added": families_added,
        "families_updated": families_updated,
        "students_added": students_added,
        "students_updated": students_updated,
        "total_families": len(families_seen),
        "total_student_rows": len(student_rows),
    }


# ---- Items balance importer -------------------------------------------------

def import_items_balance(csv_path: Path | str) -> dict:
    """Import items_balance_report.csv. One row per (student, item, period).

    The CSV has month columns (Jun..May) showing $paid per month. We pivot
    into one tuition_charges row per (student, item, month-with-activity).
    Items with no payments yet (all '-') still get one row at the period of
    the report (current month) so the assistant knows the bill exists.
    """
    csv_path = Path(csv_path)
    rows_added = 0
    rows_updated = 0
    skipped_no_student = 0
    now = datetime.now(timezone.utc).isoformat()
    current_period = datetime.now(timezone.utc).strftime("%Y-%m")

    with connect() as conn, csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        student_id_map = dict(conn.execute(
            "SELECT collegeone_student_no, id FROM students"
        ).fetchall())

        for i, row in enumerate(reader, start=1):
            if i <= ITEMS_HEADER_ROW or len(row) < 21:
                continue
            student_no = _clean(row[0])
            invoice_id = _clean(row[1])
            item_name = _clean(row[5])
            item_price = _parse_price(row[6])
            invoice_status = _clean(row[7])
            if not student_no or not item_name:
                continue

            student_db_id = student_id_map.get(student_no)
            if not student_db_id:
                skipped_no_student += 1
                continue

            school_year = _extract_school_year(item_name)
            # Month columns start at index 9 (Jun) and run 12 wide
            month_amounts = {}
            for idx, month in enumerate(MONTH_COLUMNS):
                cell = _clean(row[9 + idx])
                amt = _parse_price(cell)
                if amt > 0 and school_year:
                    period = month_to_period(month, school_year)
                    month_amounts[period] = amt

            if not month_amounts:
                # No payments yet — record a single placeholder row at current period
                month_amounts[current_period] = 0.0

            for period, amount_paid in month_amounts.items():
                paid_at = now if amount_paid > 0 else None
                existing = conn.execute(
                    "SELECT id FROM tuition_charges WHERE student_id=? "
                    "AND item_name=? AND period=?",
                    (student_db_id, item_name, period),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE tuition_charges SET item_price=?, "
                        "amount_paid=?, invoice_status=?, paid_at=?, "
                        "collegeone_invoice_id=?, updated_at=? WHERE id=?",
                        (item_price, amount_paid, invoice_status, paid_at,
                         invoice_id, now, existing["id"]),
                    )
                    rows_updated += 1
                else:
                    conn.execute(
                        "INSERT INTO tuition_charges (student_id, "
                        "collegeone_invoice_id, item_name, item_price, period, "
                        "amount_paid, invoice_status, paid_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (student_db_id, invoice_id, item_name, item_price,
                         period, amount_paid, invoice_status, paid_at),
                    )
                    rows_added += 1

    return {
        "charges_added": rows_added,
        "charges_updated": rows_updated,
        "skipped_no_student": skipped_no_student,
    }


# ---- Debtors Detailed Report ------------------------------------------------

# Column indices inside the invoice data row of the Debtors report
DEBTORS_INV_COLS = {
    "invoice": 0, "year": 1, "item": 2, "item_total": 3, "tax": 4,
    "type": 5, "date": 6, "due_date": 7, "overdue": 8, "qty": 9,
}

_INV_ID_RE = re.compile(r"^\d{8,12}$")
_OVERDUE_DAYS_RE = re.compile(r"(\d+)")


def _split_multiline(cell: str) -> list[str]:
    """Debtors invoice cells stack multiple items separated by \\n. Strip and drop empties."""
    return [line.strip() for line in (cell or "").split("\n") if line.strip()]


def import_debtors_report(csv_path: Path | str) -> dict:
    """Import debtors_report.csv into debt_snapshots (one row per overdue item).

    Snapshots are dated. Re-running on the same day replaces today's snapshot;
    different days accumulate so we can track debt over time.
    """
    csv_path = Path(csv_path)
    snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    current_year: str | None = None
    current_student_no: str | None = None
    last_invoice_id: str | None = None
    snapshots: list[dict] = []
    skipped_no_student = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or all(not _clean(c) for c in row):
                continue
            c0 = _clean(row[0])

            # School year header (e.g. "2025-2026")
            if re.match(r"^\d{4}-\d{4}$", c0):
                current_year = c0
                continue

            # Per-invoice totals row: col[2] starts with "Subtotal", col[7]/col[9]
            # carry "Payments Applied" / "Balance Due". Backfill onto last invoice.
            c2 = _clean(row[2]) if len(row) > 2 else ""
            if c2.startswith("Subtotal") and last_invoice_id:
                subtotal = _parse_price(c2.replace("Subtotal", ""))
                payments_applied = _parse_price(
                    _clean(row[7]).replace("Payments Applied", "") if len(row) > 7 else ""
                )
                balance_due = _parse_price(
                    _clean(row[9]).replace("Balance Due", "") if len(row) > 9 else ""
                )
                for s in snapshots:
                    if s["invoice_id"] == last_invoice_id and s["snapshot_date"] == snapshot_date:
                        s["invoice_subtotal"] = subtotal
                        s["invoice_payments_applied"] = payments_applied
                        s["invoice_balance_due"] = balance_due
                continue

            # Student header rows / section totals — skip
            if c0 in ("Student Name", "Invoice#") or c0.startswith("Total"):
                continue

            # Student info row: Student No. is in col[2] as an 8-12 digit string
            if len(row) > 2 and _INV_ID_RE.match(_clean(row[2])):
                current_student_no = _clean(row[2])
                continue

            # Invoice data row: col[0] is a numeric invoice id
            if _INV_ID_RE.match(c0) and current_student_no:
                invoice_id = c0
                last_invoice_id = invoice_id
                items = _split_multiline(row[DEBTORS_INV_COLS["item"]])
                totals = _split_multiline(row[DEBTORS_INV_COLS["item_total"]])
                taxes = _split_multiline(row[DEBTORS_INV_COLS["tax"]])
                types = _split_multiline(row[DEBTORS_INV_COLS["type"]])
                dates = _split_multiline(row[DEBTORS_INV_COLS["date"]])
                dues = _split_multiline(row[DEBTORS_INV_COLS["due_date"]])
                overdues = _split_multiline(row[DEBTORS_INV_COLS["overdue"]])

                for i, item_name in enumerate(items):
                    overdue_days = None
                    if i < len(overdues):
                        m = _OVERDUE_DAYS_RE.search(overdues[i])
                        if m:
                            overdue_days = int(m.group(1))
                    snapshots.append({
                        "snapshot_date": snapshot_date,
                        "student_no": current_student_no,
                        "invoice_id": invoice_id,
                        "item_name": item_name,
                        "item_amount": _parse_price(totals[i] if i < len(totals) else ""),
                        "tax": _parse_price(taxes[i] if i < len(taxes) else ""),
                        "item_type": types[i] if i < len(types) else None,
                        "invoice_date": dates[i] if i < len(dates) else None,
                        "due_date": dues[i] if i < len(dues) else None,
                        "overdue_days": overdue_days,
                        "school_year": current_year,
                        "invoice_subtotal": None,
                        "invoice_payments_applied": None,
                        "invoice_balance_due": None,
                    })

    rows_added = 0
    with connect() as conn:
        student_id_map = dict(conn.execute(
            "SELECT collegeone_student_no, id FROM students"
        ).fetchall())
        student_to_family = dict(conn.execute(
            "SELECT id, family_id FROM students"
        ).fetchall())

        # Replace today's snapshots (re-run idempotency)
        conn.execute("DELETE FROM debt_snapshots WHERE snapshot_date = ?",
                     (snapshot_date,))

        for s in snapshots:
            sid = student_id_map.get(s["student_no"])
            if not sid:
                skipped_no_student += 1
                continue
            fid = student_to_family.get(sid)
            conn.execute(
                "INSERT INTO debt_snapshots (snapshot_date, student_id, "
                "family_id, invoice_id, item_name, item_amount, tax, item_type, "
                "invoice_date, due_date, overdue_days, school_year, "
                "invoice_subtotal, invoice_payments_applied, invoice_balance_due) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["snapshot_date"], sid, fid, s["invoice_id"], s["item_name"],
                 s["item_amount"], s["tax"], s["item_type"], s["invoice_date"],
                 s["due_date"], s["overdue_days"], s["school_year"],
                 s.get("invoice_subtotal"), s.get("invoice_payments_applied"),
                 s.get("invoice_balance_due")),
            )
            rows_added += 1

    return {
        "snapshot_date": snapshot_date,
        "snapshots_added": rows_added,
        "snapshots_parsed": len(snapshots),
        "skipped_no_student": skipped_no_student,
    }


# ---- Payments Received Report -----------------------------------------------

_STUDENT_NO_RE = re.compile(r"(\d{5,8})\s+[^\(]+\(Student\)")
_PAYMENT_ID_RE = re.compile(r"^\d{5,}$")


def _parse_customer_cell(cell: str) -> tuple[str, str | None]:
    """'Aviles Estrella, Desiree:\\n41401 Rodriguez Aviles, Vanelope(Student)/'
       → ('Aviles Estrella, Desiree', '41401')

    Many parents have multiple students; we take the first as the primary link
    since CollegeOne charges payments at the family level, not per child.
    """
    cell = (cell or "").strip()
    customer = cell.split("\n", 1)[0].rstrip(":").strip()
    m = _STUDENT_NO_RE.search(cell)
    student_no = m.group(1) if m else None
    return customer, student_no


def import_payments_received(csv_path: Path | str) -> dict:
    """Import payments_received_report.csv into payments_received table.

    Idempotent — UPSERTs by collegeone_payment_id.
    """
    csv_path = Path(csv_path)
    current_section: str | None = None  # 'Regular Invoices' / 'Recurring Invoices'
    payments: list[dict] = []

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or all(not _clean(c) for c in row):
                continue
            c0 = _clean(row[0])

            if c0 in ("Regular Invoices", "Recurring Invoices"):
                current_section = c0
                continue
            # Skip headers, totals, school year, payment-method group headers
            if c0 in ("Payment ID", "Bank Account", "Cash", "Check", "Card",
                      "Total", "Total ") or c0.startswith("Total") or \
                    c0.endswith("Total") or re.match(r"^\d{4}-\d{4}$", c0):
                continue

            if not _PAYMENT_ID_RE.match(c0) or len(row) < 10:
                continue

            # Old CSV had a "Year" column at [1] making customer at [3].
            # New CSV dropped that column so customer is at [2].
            # Detect by checking if row[1] looks like a school-year string.
            has_year_col = bool(re.match(r"^\d{4}-\d{4}", _clean(row[1])))
            col_offset = 1 if has_year_col else 0

            customer, student_no = _parse_customer_cell(row[2 + col_offset])
            payments.append({
                "collegeone_payment_id": c0,
                "payment_date": _clean(row[1 + col_offset]),
                "customer_name": customer,
                "student_no": student_no,
                "payment_method": _clean(row[3 + col_offset]),
                "invoice_id": _clean(row[4 + col_offset]),
                "transaction_id": _clean(row[5 + col_offset]) or None,
                "status": _clean(row[6 + col_offset]),
                "payout_id": _clean(row[7 + col_offset]) or None,
                "payout_date": _clean(row[8 + col_offset]) or None,
                "amount": _parse_price(row[9 + col_offset]),
                "section": current_section,
            })

    added = 0
    updated = 0
    skipped = 0
    with connect() as conn:
        student_id_map = dict(conn.execute(
            "SELECT collegeone_student_no, id FROM students"
        ).fetchall())
        student_to_family = dict(conn.execute(
            "SELECT id, family_id FROM students"
        ).fetchall())

        for p in payments:
            sid = student_id_map.get(p["student_no"]) if p["student_no"] else None
            fid = student_to_family.get(sid) if sid else None
            existing = conn.execute(
                "SELECT id FROM payments_received WHERE collegeone_payment_id=?",
                (p["collegeone_payment_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE payments_received SET payment_date=?, "
                    "customer_name=?, student_id=?, family_id=?, "
                    "payment_method=?, invoice_id=?, transaction_id=?, "
                    "status=?, amount=?, payout_id=?, payout_date=?, section=? "
                    "WHERE id=?",
                    (p["payment_date"], p["customer_name"], sid, fid,
                     p["payment_method"], p["invoice_id"], p["transaction_id"],
                     p["status"], p["amount"], p["payout_id"], p["payout_date"],
                     p["section"], existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO payments_received (collegeone_payment_id, "
                    "payment_date, customer_name, student_id, family_id, "
                    "payment_method, invoice_id, transaction_id, status, amount, "
                    "payout_id, payout_date, section) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p["collegeone_payment_id"], p["payment_date"],
                     p["customer_name"], sid, fid, p["payment_method"],
                     p["invoice_id"], p["transaction_id"], p["status"],
                     p["amount"], p["payout_id"], p["payout_date"], p["section"]),
                )
                added += 1

    return {
        "payments_added": added,
        "payments_updated": updated,
        "payments_parsed": len(payments),
        "skipped_no_student_link": skipped,
    }


# ---- Payment ↔ student/family name linker ----------------------------------

def _normalize_name(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace. For fuzzy comparison."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _flip_lastname_first(customer: str) -> str:
    """'Aviles Estrella, Desiree' → 'Desiree Aviles Estrella'. Leaves single-name as-is."""
    if "," in customer:
        last, first = customer.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return customer


def _best_token_match(target: str, candidates: list[tuple[int, str]]) -> tuple[int, float] | None:
    """Return (id, score) of best candidate by token overlap. Score = matched_tokens / max_tokens.

    Threshold 0.75 keeps false matches rare given short name token sets.
    """
    t_tokens = set(_normalize_name(target).split())
    if not t_tokens:
        return None
    best: tuple[int, float] | None = None
    for cid, name in candidates:
        c_tokens = set(_normalize_name(name).split())
        if not c_tokens:
            continue
        overlap = len(t_tokens & c_tokens)
        score = overlap / max(len(t_tokens), len(c_tokens))
        if score >= 0.75 and (best is None or score > best[1]):
            best = (cid, score)
    return best


def link_payments_by_name() -> dict:
    """Backfill family_id / student_id on payments_received rows that lack them.

    Strategy per row:
      1. Flip 'Lastname, Firstname' format.
      2. Normalized-exact match against families.name → set family_id.
      3. Normalized-exact match against students.name → set student_id (+ family).
      4. Token-overlap fuzzy match (>=0.75) against families, then students.
      5. If a family has exactly one student, also set student_id for clarity.
    """
    linked_family = 0
    linked_student = 0
    unmatched: list[dict] = []
    with connect() as conn:
        families = conn.execute("SELECT id, name FROM families").fetchall()
        students = conn.execute("SELECT id, name, family_id FROM students").fetchall()
        family_by_norm = {_normalize_name(f["name"]): f["id"] for f in families}
        student_by_norm = {_normalize_name(s["name"]): (s["id"], s["family_id"]) for s in students}
        family_list = [(f["id"], f["name"]) for f in families]
        student_list = [(s["id"], s["name"]) for s in students]
        student_to_family = {s["id"]: s["family_id"] for s in students}
        family_single_student = {}  # family_id → student_id when family has exactly 1 student
        from collections import defaultdict
        kids_per_family: dict[int, list[int]] = defaultdict(list)
        for s in students:
            kids_per_family[s["family_id"]].append(s["id"])
        for fid, kids in kids_per_family.items():
            if len(kids) == 1:
                family_single_student[fid] = kids[0]

        rows = conn.execute(
            "SELECT id, customer_name FROM payments_received "
            "WHERE family_id IS NULL OR student_id IS NULL"
        ).fetchall()

        for r in rows:
            cn = r["customer_name"]
            flipped = _flip_lastname_first(cn)
            norm = _normalize_name(flipped)
            fid: int | None = family_by_norm.get(norm)
            sid: int | None = None
            score: float | None = None
            method = ""

            if fid is not None:
                method = "family-exact"
            else:
                # Try student exact
                hit = student_by_norm.get(norm)
                if hit is not None:
                    sid, fid = hit
                    method = "student-exact"
                else:
                    # Fuzzy family
                    fam = _best_token_match(flipped, family_list)
                    if fam is not None:
                        fid, score = fam
                        method = f"family-fuzzy:{score:.2f}"
                    else:
                        stu = _best_token_match(flipped, student_list)
                        if stu is not None:
                            sid, score = stu
                            fid = student_to_family.get(sid)
                            method = f"student-fuzzy:{score:.2f}"

            if fid is None:
                unmatched.append({"id": r["id"], "customer_name": cn, "flipped": flipped})
                continue

            # If family resolved but student didn't, and family has exactly one kid, link them too
            if sid is None and fid in family_single_student:
                sid = family_single_student[fid]

            conn.execute(
                "UPDATE payments_received SET family_id=?, student_id=? WHERE id=?",
                (fid, sid, r["id"]),
            )
            linked_family += 1
            if sid:
                linked_student += 1

    return {
        "rows_processed": len(rows) if 'rows' in dir() else 0,
        "linked_to_family": linked_family,
        "linked_to_student": linked_student,
        "unmatched": unmatched,
    }


# ---- CLI entrypoint ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    from db import init_db

    init_db()
    samples = Path(__file__).parent.parent / "data" / "samples"

    print("== Importing families ==")
    stats = import_families(samples / "families_report.csv")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\n== Importing items balance ==")
    stats = import_items_balance(samples / "items_balance_report.csv")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\nDone.")
