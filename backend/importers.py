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
