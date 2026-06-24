"""Matrícula invoicing: read the enrollment payments CSV, create one invoice per
student in QuickBooks, and apply the payment they've already made.

CSV shape (from the Google Sheet export):
    Nombre del Estudiante, Total Pagado, , Nombre del Estudiante, Saldo Restante

Invoice total per student = Total Pagado + Saldo Restante (the full enrollment fee).
The payment we record = Total Pagado (skipped when it's $0).

Workflow is dry-run by default. It matches every CSV name to a QB customer and
prints exactly what it WOULD do, plus a list of anyone it could not match. Pass
--execute (with --item-id) to actually write to QuickBooks.

A run log at data/matricula_run.json records which students already got an
invoice/payment, so re-running is safe and won't double-charge.

Usage (inside the container):
    python -m matricula_payments --csv /app/data/matriculas.csv
    python -m matricula_payments --csv /app/data/matriculas.csv \
        --execute --item-id 23 --deposit-account-id 35 --date 2026-06-10
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from integrations import quickbooks as qb

RUN_LOG = Path(__file__).parent.parent / "data" / "matricula_run.json"

# Match-confidence thresholds
EXACT = "exact"        # identical normalized token sets
SUBSET = "subset"      # one name's tokens contained in the other's
FUZZY = "fuzzy"        # best SequenceMatcher ratio above FUZZY_MIN
FUZZY_MIN = 0.82


# ---- parsing ----------------------------------------------------------------

def _money(raw: str) -> float:
    """'$1,430' -> 1430.0 ; '$0' -> 0.0 ; '' -> 0.0"""
    s = (raw or "").replace("$", "").replace(",", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_csv(path: Path) -> list[dict]:
    """Return [{name, paid, remaining, total}] skipping headers, totals, and $0/$0."""
    students: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            if name.lower() in ("nombre del estudiante", "total"):
                continue
            paid = _money(row[1] if len(row) > 1 else "")
            remaining = _money(row[4] if len(row) > 4 else "")
            total = round(paid + remaining, 2)
            if total <= 0:
                continue  # nothing owed, nothing to invoice
            students.append({
                "name": name,
                "paid": paid,
                "remaining": remaining,
                "total": total,
            })
    return students


def parse_sheets_csv(path: Path) -> list[dict]:
    """Parse the Google Sheets matricula tracker CSV (v2 format).

    Column layout:
        0: row#  1: name  2: grupo  3: costo  4: deposito
        5: abono1  6: abono2  7: abono3  8: saldo_restante

    Returns [{name, paid, remaining, total}] for rows with paid > 0 and cost > 0.
    paid = deposito + abono1 + abono2 + abono3.
    """
    students: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 9:
                continue
            name = row[1].strip()
            if not name or name in ("Nombre del Estudiante", "A", "B"):
                continue
            cost = _money(row[3])
            if cost <= 0:
                continue
            paid = round(sum(_money(row[i]) for i in range(4, 8)), 2)
            remaining = _money(row[8])
            if paid <= 0:
                continue
            students.append({
                "name": name,
                "paid": paid,
                "remaining": remaining,
                "total": cost,
            })
    return students


def parse_items_balance_csv(path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Parse a CollegeOne Items Balance Report CSV.

    Returns (paid_list, partial_list, past_due_list) where each element is:
        {name, paid, remaining, total, status, collegeone_invoice_id}

    - paid: Invoice Status == "Paid"  →  paid = item_price, remaining = 0
    - partial: "Partially Paid"       →  paid = 0 (exact unknown; needs manual review)
    - past_due: "Past Due"            →  paid = 0, remaining = item_price
    Cancelled rows and duplicates (keep first non-cancelled) are dropped.
    """
    paid_list: list[dict] = []
    partial_list: list[dict] = []
    past_due_list: list[dict] = []
    seen: set[str] = {}  # type: ignore[assignment]  # name_key → status
    seen = {}

    in_data = False
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0].strip() == "Student No.":
                in_data = True
                continue
            if not in_data:
                continue
            if len(row) < 9:
                continue
            cell0 = row[0].strip()
            if not cell0 or cell0 in ("Payments Received", "Pending Payments"):
                continue

            raw_name = row[2].strip().rstrip("*").strip()
            item_price = _money(row[6])
            status = row[7].strip().lower()

            if not raw_name or item_price <= 0:
                continue
            if status == "cancelled":
                continue

            name_key = _norm(raw_name)
            if name_key in seen:
                continue
            seen[name_key] = status

            entry = {
                "name": raw_name,
                "total": item_price,
                "collegeone_invoice_id": row[1].strip(),
                "status": status,
            }
            if status == "paid":
                entry.update({"paid": item_price, "remaining": 0.0})
                paid_list.append(entry)
            elif status == "partially paid":
                entry.update({"paid": 0.0, "remaining": item_price})
                partial_list.append(entry)
            else:
                entry.update({"paid": 0.0, "remaining": item_price})
                past_due_list.append(entry)

    return paid_list, partial_list, past_due_list


# ---- name matching ----------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def match_customer(name: str, customers: list[dict]) -> dict:
    """Best customer match for a CSV student name.

    Returns {customer, confidence, score, candidates} where customer may be None.
    candidates lists near-misses so a human can sanity-check.
    """
    src = _tokens(name)
    if not src:
        return {"customer": None, "confidence": None, "score": 0.0, "candidates": []}

    exact, subset, scored = [], [], []
    for c in customers:
        cust_tokens = _tokens(c["name"] or "")
        if not cust_tokens:
            continue
        if src == cust_tokens:
            exact.append(c)
        elif src <= cust_tokens or cust_tokens <= src:
            subset.append(c)
        ratio = SequenceMatcher(None, _norm(name), _norm(c["name"] or "")).ratio()
        scored.append((ratio, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [{"name": c["name"], "id": c["id"], "score": round(r, 3)}
                  for r, c in scored[:5]]

    if len(exact) == 1:
        return {"customer": exact[0], "confidence": EXACT, "score": 1.0,
                "candidates": candidates}
    if not exact and len(subset) == 1:
        return {"customer": subset[0], "confidence": SUBSET,
                "score": scored[0][0] if scored else 0.0, "candidates": candidates}
    if not exact and not subset and scored and scored[0][0] >= FUZZY_MIN:
        return {"customer": scored[0][1], "confidence": FUZZY,
                "score": round(scored[0][0], 3), "candidates": candidates}
    # ambiguous (multiple exact/subset) or nothing close enough
    return {"customer": None, "confidence": None,
            "score": round(scored[0][0], 3) if scored else 0.0,
            "candidates": candidates}


# ---- run log (idempotency) --------------------------------------------------

def _load_log() -> dict:
    if RUN_LOG.exists():
        return json.loads(RUN_LOG.read_text())
    return {}


def _save_log(log: dict) -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False))


# ---- planning + execution ---------------------------------------------------

def build_plan(students: list[dict], customers: list[dict]) -> dict:
    matched, needs_review, not_found = [], [], []
    for s in students:
        m = match_customer(s["name"], customers)
        entry = {**s, **{"match_confidence": m["confidence"],
                         "match_score": m["score"], "candidates": m["candidates"]}}
        if m["customer"] and m["confidence"] == EXACT:
            entry["customer_id"] = m["customer"]["id"]
            entry["customer_name"] = m["customer"]["name"]
            matched.append(entry)
        elif m["customer"]:
            entry["customer_id"] = m["customer"]["id"]
            entry["customer_name"] = m["customer"]["name"]
            needs_review.append(entry)
        else:
            not_found.append(entry)
    return {"matched": matched, "needs_review": needs_review, "not_found": not_found}


def execute(plan_entries: list[dict], item_id: str, txn_date: str | None,
            deposit_account_id: str | None, payment_method_id: str | None,
            description: str) -> list[dict]:
    log = _load_log()
    results = []
    for e in plan_entries:
        key = e["customer_id"]
        prior = log.get(key, {})
        res = {"name": e["name"], "customer_id": key, "customer_name": e["customer_name"]}
        try:
            inv_id = prior.get("invoice_id")
            if inv_id:
                res["invoice_id"] = inv_id
                res["invoice"] = "already-created (skipped)"
            else:
                inv = qb.create_invoice(e["customer_id"], item_id, e["total"],
                                        txn_date=txn_date, description=description)
                inv_id = inv["id"]
                res["invoice_id"] = inv_id
                res["invoice"] = f"created total ${e['total']:.2f}"
                prior["invoice_id"] = inv_id

            if e["paid"] > 0:
                if prior.get("payment_id"):
                    res["payment_id"] = prior["payment_id"]
                    res["payment"] = "already-applied (skipped)"
                else:
                    pay = qb.create_payment(
                        e["customer_id"], inv_id, e["paid"], txn_date=txn_date,
                        deposit_account_id=deposit_account_id,
                        payment_method_id=payment_method_id,
                        memo=f"Matrícula payment for {e['name']}")
                    res["payment_id"] = pay["id"]
                    res["payment"] = f"applied ${e['paid']:.2f}"
                    prior["payment_id"] = pay["id"]
            else:
                res["payment"] = "none ($0 paid)"

            prior["name"] = e["name"]
            log[key] = prior
            _save_log(log)  # persist after each student so a crash is recoverable
            res["status"] = "ok"
        except Exception as ex:  # noqa: BLE001 - report and continue
            res["status"] = "error"
            res["error"] = str(ex)[:300]
        results.append(res)
    return results


# ---- HTTP-friendly wrappers -------------------------------------------------

def plan_json(csv_path: Path) -> dict:
    """Dry-run plan as a JSON-serializable dict (for the /admin endpoint)."""
    students = parse_csv(csv_path)
    if not qb.is_connected():
        return {"error": "quickbooks_not_connected"}
    customers = qb.list_customers()
    plan = build_plan(students, customers)
    accounts = qb.list_accounts()
    return {
        "counts": {
            "students": len(students),
            "matched": len(plan["matched"]),
            "needs_review": len(plan["needs_review"]),
            "not_found": len(plan["not_found"]),
            "qb_customers": len(customers),
        },
        "totals": {
            "invoice_total_matched": round(sum(e["total"] for e in plan["matched"]), 2),
            "payment_total_matched": round(sum(e["paid"] for e in plan["matched"]), 2),
        },
        "matched": plan["matched"],
        "needs_review": plan["needs_review"],
        "not_found": plan["not_found"],
        "items": qb.list_items(),
        "deposit_accounts": [a for a in accounts
                             if a["type"] in ("Bank", "Other Current Asset")],
    }


def execute_json(csv_path: Path, item_id: str, txn_date: str | None,
                 deposit_account_id: str | None, payment_method_id: str | None,
                 include_review: bool = False, description: str = "Matrícula") -> dict:
    """Run the invoices/payments and return a JSON-serializable summary."""
    students = parse_csv(csv_path)
    customers = qb.list_customers()
    plan = build_plan(students, customers)
    to_run = list(plan["matched"])
    if include_review:
        to_run += plan["needs_review"]
    results = execute(to_run, item_id, txn_date, deposit_account_id,
                      payment_method_id, description)
    return {
        "ran": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


# ---- Items Balance Report wrappers ------------------------------------------

def plan_items_balance_json(csv_path: Path) -> dict:
    """Dry-run plan for a CollegeOne Items Balance Report CSV.

    - matched: "Paid" students matched to QB customers (payment will be applied)
    - needs_review: "Partially Paid" students — exact amount unknown, manual action needed
    - not_found: "Paid" students with no QB customer match
    - past_due: "Past Due" students (no payment to apply)
    """
    if not qb.is_connected():
        return {"error": "quickbooks_not_connected"}
    paid_list, partial_list, past_due_list = parse_items_balance_csv(csv_path)
    customers = qb.list_customers()
    log = _load_log()

    # Match paid students to QB customers
    plan = build_plan(paid_list, customers)

    # Annotate each entry with run-log status so the caller knows what's already done
    def _annotate(entries: list[dict]) -> list[dict]:
        for e in entries:
            cid = e.get("customer_id")
            prior = log.get(cid, {}) if cid else {}
            e["log_invoice_id"] = prior.get("invoice_id")
            e["log_payment_id"] = prior.get("payment_id")
            e["action"] = (
                "skip (payment already recorded)" if prior.get("payment_id")
                else "apply payment $650" if prior.get("invoice_id")
                else "create invoice + apply payment $650"
            )
        return entries

    _annotate(plan["matched"])
    _annotate(plan["needs_review"])

    # Match partially-paid and past-due for display only
    partial_plan = build_plan(partial_list, customers)
    past_due_plan = build_plan(past_due_list, customers)

    accounts = qb.list_accounts()
    return {
        "counts": {
            "paid_in_collegeone": len(paid_list),
            "partially_paid": len(partial_list),
            "past_due": len(past_due_list),
            "matched_to_qb": len(plan["matched"]),
            "needs_review": len(plan["needs_review"]),
            "not_found_in_qb": len(plan["not_found"]),
        },
        "totals": {
            "payments_to_apply": round(
                sum(e["total"] for e in plan["matched"]
                    if not e.get("log_payment_id")), 2),
            "already_recorded": round(
                sum(e["total"] for e in plan["matched"]
                    if e.get("log_payment_id")), 2),
        },
        "matched": plan["matched"],
        "needs_review": plan["needs_review"] + partial_plan["matched"] + partial_plan["needs_review"],
        "not_found": plan["not_found"],
        "past_due": past_due_plan["matched"] + past_due_plan["needs_review"] + past_due_plan["not_found"],
        "items": qb.list_items(),
        "deposit_accounts": [a for a in accounts
                             if a["type"] in ("Bank", "Other Current Asset")],
    }


def execute_items_balance_json(csv_path: Path, item_id: str, txn_date: str | None,
                               deposit_account_id: str | None,
                               payment_method_id: str | None,
                               include_review: bool = False,
                               description: str = "Matrícula 2026-2027") -> dict:
    """Apply payments for 'Paid' students in a CollegeOne Items Balance Report.

    Creates a QB invoice if the student has no existing one (run-log check),
    then applies a $650 payment. Already-recorded payments are skipped.
    """
    paid_list, partial_list, _ = parse_items_balance_csv(csv_path)
    customers = qb.list_customers()
    plan = build_plan(paid_list, customers)
    to_run = list(plan["matched"])
    if include_review:
        partial_plan = build_plan(partial_list, customers)
        to_run += partial_plan["matched"] + partial_plan["needs_review"]
    results = execute(to_run, item_id, txn_date, deposit_account_id,
                      payment_method_id, description)
    return {
        "ran": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "not_found_in_qb": [e["name"] for e in plan["not_found"]],
        "results": results,
    }


# ---- CLI --------------------------------------------------------------------

def _print_plan(plan: dict) -> None:
    m, r, nf = plan["matched"], plan["needs_review"], plan["not_found"]
    inv_total = sum(e["total"] for e in m)
    pay_total = sum(e["paid"] for e in m)
    print(f"\n=== MATCHED (will invoice) — {len(m)} students ===")
    print(f"{'Student':38} {'→ QB customer':32} {'invoice':>9} {'payment':>9}")
    for e in m:
        print(f"{e['name'][:37]:38} {e['customer_name'][:31]:32} "
              f"${e['total']:>8.2f} ${e['paid']:>8.2f}")
    print(f"\nTotals for matched: invoices ${inv_total:,.2f} | payments ${pay_total:,.2f}")

    if r:
        print(f"\n=== NEEDS REVIEW (low-confidence match, NOT auto-run) — {len(r)} ===")
        for e in r:
            print(f"  {e['name']!r}  ~ {e['customer_name']!r} "
                  f"(score {e['match_score']}, {e['match_confidence']})")

    if nf:
        print(f"\n=== NOT FOUND IN QUICKBOOKS — {len(nf)} ===")
        for e in nf:
            cand = ", ".join(f"{c['name']}({c['score']})" for c in e["candidates"][:3])
            print(f"  {e['name']!r}  (paid ${e['paid']:.0f}, owes ${e['remaining']:.0f})"
                  f"  closest: {cand or 'none'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Matrícula invoicing → QuickBooks")
    ap.add_argument("--csv", required=True, help="path to the payments CSV")
    ap.add_argument("--execute", action="store_true", help="actually write to QB")
    ap.add_argument("--item-id", help="QB Item (Product/Service) Id for the line")
    ap.add_argument("--deposit-account-id", help="QB account to deposit payments into")
    ap.add_argument("--payment-method-id", help="QB PaymentMethod Id (optional)")
    ap.add_argument("--date", help="txn date YYYY-MM-DD (default: today)")
    ap.add_argument("--description", default="Matrícula", help="invoice line description")
    ap.add_argument("--include-review", action="store_true",
                    help="also execute the needs-review matches")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    students = parse_csv(csv_path)
    print(f"Parsed {len(students)} students with a balance to invoice.")

    if not qb.is_connected():
        print("QuickBooks is NOT connected — visit /qb/connect first.", file=sys.stderr)
        return 3

    customers = qb.list_customers()
    print(f"Loaded {len(customers)} QB customers.")
    plan = build_plan(students, customers)
    _print_plan(plan)

    if not args.execute:
        print("\n(DRY RUN — nothing written. Re-run with --execute --item-id <id> to apply.)")
        # show item + deposit-account options to help pick
        print("\nAvailable Items (for --item-id):")
        for it in qb.list_items():
            print(f"  {it['id']:>4}  {it['fully_qualified_name'] or it['name']}  [{it['type']}]")
        return 0

    if not args.item_id:
        print("--item-id is required with --execute.", file=sys.stderr)
        return 4

    to_run = list(plan["matched"])
    if args.include_review:
        to_run += plan["needs_review"]
    print(f"\nExecuting against PRODUCTION QuickBooks for {len(to_run)} students...")
    results = execute(to_run, args.item_id, args.date,
                      args.deposit_account_id, args.payment_method_id, args.description)
    ok = sum(1 for r in results if r["status"] == "ok")
    err = [r for r in results if r["status"] == "error"]
    for r in results:
        line = f"  [{r['status']}] {r['name']}: {r.get('invoice','')}; {r.get('payment','')}"
        if r["status"] == "error":
            line += f"  ERROR: {r['error']}"
        print(line)
    print(f"\nDone: {ok} ok, {len(err)} errors.")
    return 0 if not err else 1


if __name__ == "__main__":
    raise SystemExit(main())
