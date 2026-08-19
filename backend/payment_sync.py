"""CollegeOne → QuickBooks payment sync.

For each unsynced row in payments_received:
  1. Match CollegeOne customer_name → QB customer (fuzzy)
  2. Find QB invoice (by DocNumber, then by amount match on open invoices)
  3. If confident match → create_payment in QB, mark qb_synced_at
  4. If ambiguous → push to approval_queue for manual review

Entry points:
  sync_all()         – sync all pending rows, return summary dict
  sync_one(row)      – sync a single payment row dict
  push_to_queue(row, reason)  – add to approval_queue for manual review

Run as script:  python -m payment_sync [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "simply.db"


# ──────────────────────────────────────────────────────────────────────────────
# Name normalisation (same as matricula_payments.py)
# ──────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def _match_score(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _match_customer(co_name: str, qb_customers: list[dict]) -> dict | None:
    """Return best QB customer for a CollegeOne customer name, or None if unsure."""
    src = _tokens(co_name)
    if not src:
        return None

    exact, subset, scored = [], [], []
    for c in qb_customers:
        cust_tokens = _tokens(c.get("name") or "")
        if not cust_tokens:
            continue
        if src == cust_tokens:
            exact.append(c)
        elif src <= cust_tokens or cust_tokens <= src:
            subset.append(c)
        scored.append((_match_score(co_name, c.get("name") or ""), c))

    scored.sort(key=lambda x: x[0], reverse=True)

    if len(exact) == 1:
        return exact[0]
    if not exact and len(subset) == 1:
        return subset[0]
    if not exact and not subset and scored and scored[0][0] >= 0.82:
        return scored[0][1]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Payment method mapping  (CollegeOne → QB PaymentMethod name)
# ──────────────────────────────────────────────────────────────────────────────

_METHOD_KEYWORDS: list[tuple[list[str], str]] = [
    (["cash", "efectivo"], "Cash"),
    (["check", "cheque"], "Check"),
    (["card", "tarjeta", "credit", "debit", "cc"], "Credit Card"),
    (["bank", "banco", "ach", "transfer"], "Check"),  # no "Bank Transfer" in default QB
]


def _resolve_method_id(co_method: str, qb_methods: list[dict]) -> str | None:
    """Map a CollegeOne payment method string to a QB PaymentMethod Id."""
    key = _norm(co_method)
    for keywords, qb_name in _METHOD_KEYWORDS:
        if any(kw in key for kw in keywords):
            for m in qb_methods:
                if _norm(m["name"]) == _norm(qb_name):
                    return m["id"]
            break
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Invoice matching
# ──────────────────────────────────────────────────────────────────────────────

def _strip_leading_zeros(s: str) -> str:
    return str(int(s)) if s.isdigit() else s.lstrip("0") or "0"


def _find_qb_invoice(co_invoice_id: str, amount: float,
                     qb_invoices: list[dict]) -> dict | None:
    """Find the best QB invoice to apply a payment to.

    Strategy:
      1. DocNumber exact match (strip leading zeros from both sides)
      2. Single open invoice with balance >= amount
      3. Closest balance to amount if unique
      Return None if ambiguous or no match.
    """
    if not qb_invoices:
        return None

    co_num = _strip_leading_zeros(re.sub(r"\D", "", co_invoice_id))

    # 1. DocNumber match
    for inv in qb_invoices:
        doc = _strip_leading_zeros(re.sub(r"\D", "", inv.get("doc_number") or ""))
        if doc and doc == co_num:
            return inv

    # 2. Only one open invoice → apply there unconditionally.
    # Payment may exceed invoice balance when CollegeOne adds a convenience fee
    # (2.5% CC, 0.5% bank). QB applies up to the invoice balance and keeps the
    # rest as unapplied credit — correct behavior.
    if len(qb_invoices) == 1:
        return qb_invoices[0]

    # 3. Invoices where balance exactly equals payment amount
    exact_balance = [i for i in qb_invoices if abs(i["balance"] - amount) < 0.02]
    if len(exact_balance) == 1:
        return exact_balance[0]

    # 4. Invoices whose balance is within 3% above the payment (fee-adjusted match)
    fee_match = [i for i in qb_invoices if i["balance"] <= amount * 1.03 + 0.50
                 and i["balance"] >= amount * 0.97 - 0.50]
    if len(fee_match) == 1:
        return fee_match[0]

    # 5. Invoices where balance >= payment (partial payment)
    sufficient = [i for i in qb_invoices if i["balance"] >= amount - 0.01]
    if len(sufficient) == 1:
        return sufficient[0]

    return None  # ambiguous


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_db():
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _pending_payments() -> list[dict]:
    con = _get_db()
    rows = con.execute(
        "SELECT pr.*, s.name AS student_name "
        "FROM payments_received pr "
        "LEFT JOIN students s ON pr.student_id = s.id "
        "WHERE pr.qb_synced_at IS NULL "
        "AND pr.status IN ('Processed', 'In Process') "
        "ORDER BY pr.payment_date ASC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _mark_synced(payment_id: int, qb_payment_id: str) -> None:
    con = _get_db()
    con.execute(
        "UPDATE payments_received SET qb_synced_at = ?, qb_payment_id = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), qb_payment_id, payment_id),
    )
    con.commit()
    con.close()


def _push_to_queue(row: dict, reason: str, qb_customers: list[dict],
                   qb_customer: dict | None, qb_invoices: list[dict]) -> None:
    payload = {
        "payment_row_id": row["id"],
        "collegeone_payment_id": row["collegeone_payment_id"],
        "customer_name": row["customer_name"],
        "invoice_id": row["invoice_id"],
        "amount": row["amount"],
        "payment_date": row["payment_date"],
        "payment_method": row["payment_method"],
        "reason": reason,
        "qb_customer_id": (qb_customer or {}).get("id"),
        "qb_customer_name": (qb_customer or {}).get("name"),
        "open_invoices": qb_invoices[:5] if qb_invoices else [],
    }
    summary = (
        f"Sync pago CollegeOne: {row['customer_name']} ${row['amount']:.2f} "
        f"({row['payment_date']}) — {reason}"
    )
    con = _get_db()
    con.execute(
        "INSERT INTO approval_queue (agent, action_type, summary, payload_json) "
        "VALUES (?, ?, ?, ?)",
        ("payment_sync", "qb.apply_payment", summary, json.dumps(payload, default=str)),
    )
    con.commit()
    con.close()


# ──────────────────────────────────────────────────────────────────────────────
# Deposit account config
# ──────────────────────────────────────────────────────────────────────────────

def _get_deposit_account_id(accounts: list[dict]) -> str | None:
    """Find the main bank account to deposit into (Operating / Checking)."""
    import os
    env_id = os.getenv("QB_DEPOSIT_ACCOUNT_ID")
    if env_id:
        return env_id
    for a in accounts:
        name = _norm(a.get("name") or "")
        if any(kw in name for kw in ["checking", "operating", "corriente"]):
            return a["id"]
    # Fallback: first Bank account
    for a in accounts:
        if a.get("type") == "Bank":
            return a["id"]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Core sync
# ──────────────────────────────────────────────────────────────────────────────

def sync_all(dry_run: bool = False) -> dict:
    """Sync all unsynced CollegeOne payments to QuickBooks.

    Returns summary with counts of synced, queued, skipped, and errors.
    """
    from integrations import quickbooks as qb

    if not qb.is_connected():
        return {"error": "quickbooks_not_connected"}

    pending = _pending_payments()
    if not pending:
        return {"synced": 0, "queued": 0, "skipped": 0, "errors": 0,
                "details": [], "message": "No hay pagos pendientes de sincronizar"}

    qb_customers = qb.list_customers()
    qb_methods = qb.list_payment_methods()
    accounts = qb.list_accounts()
    deposit_account_id = _get_deposit_account_id(accounts)

    results = []
    counts = {"synced": 0, "queued": 0, "skipped": 0, "errors": 0}

    for row in pending:
        res = _sync_one_row(
            row, qb_customers, qb_methods, deposit_account_id,
            dry_run=dry_run, qb_module=qb,
        )
        results.append(res)
        counts[res["outcome"]] = counts.get(res["outcome"], 0) + 1

    return {**counts, "details": results}


def _sync_one_row(row: dict, qb_customers: list[dict], qb_methods: list[dict],
                  deposit_account_id: str | None, dry_run: bool,
                  qb_module) -> dict:
    co_id = row["collegeone_payment_id"]
    amount = float(row["amount"])
    customer_name = row.get("customer_name") or ""

    res = {
        "co_id": co_id,
        "customer": customer_name,
        "invoice": row.get("invoice_id"),
        "amount": amount,
        "date": row.get("payment_date"),
        "outcome": "error",
        "note": "",
    }

    # 1. Match QB customer — prefer DB student link over fuzzy name matching
    # The payments_received table stores the parent/family name, but QB customers
    # are student names. Use the pre-linked student_name from the DB join first.
    student_name = row.get("student_name") or ""
    qb_customer = None
    if student_name:
        qb_customer = _match_customer(student_name, qb_customers)
    if not qb_customer:
        # Fallback: try the C1 customer (parent) name directly
        qb_customer = _match_customer(customer_name, qb_customers)
    if not qb_customer:
        _push_to_queue(row, "No se encontró cliente en QB", qb_customers[:0], None, [])
        res.update(outcome="queued", note="No QB customer match")
        return res

    # 2. Find QB invoices for this customer
    try:
        qb_invoices = qb_module.list_invoices_for_customer(qb_customer["id"])
    except Exception as e:
        res.update(outcome="error", note=f"Error listando invoices QB: {e}")
        return res

    # 3. Find the right invoice
    qb_inv = _find_qb_invoice(row.get("invoice_id") or "", amount, qb_invoices)
    if not qb_inv:
        _push_to_queue(row, f"No se pudo identificar invoice QB (tiene {len(qb_invoices)} abiertas)",
                       qb_customers, qb_customer, qb_invoices)
        res.update(outcome="queued", note=f"Invoice ambigua ({len(qb_invoices)} abiertas en QB)")
        return res

    # 4. Payment method
    method_id = _resolve_method_id(row.get("payment_method") or "", qb_methods)

    # 5. Apply payment
    memo = (
        f"CollegeOne pago {co_id} | "
        f"Factura CO #{row.get('invoice_id','?')} | "
        f"{row.get('payment_method','')} {row.get('payment_date','')}"
    )

    if dry_run:
        res.update(
            outcome="synced",
            note=f"[DRY RUN] QB customer {qb_customer['name']} → invoice {qb_inv['id']} "
                 f"(doc #{qb_inv.get('doc_number')})",
            qb_customer_id=qb_customer["id"],
            qb_invoice_id=qb_inv["id"],
        )
        return res

    try:
        payment = qb_module.create_payment(
            customer_id=qb_customer["id"],
            invoice_id=qb_inv["id"],
            amount=amount,
            txn_date=row.get("payment_date"),
            deposit_account_id=deposit_account_id,
            payment_method_id=method_id,
            memo=memo,
        )
        _mark_synced(row["id"], payment["id"])
        res.update(
            outcome="synced",
            note=f"QB pago {payment['id']} → invoice {qb_inv['id']}",
            qb_payment_id=payment["id"],
            qb_invoice_id=qb_inv["id"],
        )
    except Exception as e:
        res.update(outcome="error", note=f"QB error: {e}")

    return res


# ──────────────────────────────────────────────────────────────────────────────
# Telegram notification
# ──────────────────────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> None:
    import os, httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _build_telegram_summary(result: dict) -> str:
    if "error" in result:
        return f"❌ *Payment Sync*: {result['error']}"
    lines = [
        f"✅ *Payment Sync* completado",
        f"• Sincronizados a QB: {result['synced']}",
        f"• En cola de aprobación: {result['queued']}",
        f"• Errores: {result['errors']}",
    ]
    if result.get("queued") or result.get("errors"):
        lines.append("→ Revisa `/admin/approval-queue` para los pendientes")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Full daily cycle (scrape + import + sync)
# ──────────────────────────────────────────────────────────────────────────────

def daily_cycle(dry_run: bool = False, headed: bool = False) -> dict:
    """Scrape CollegeOne, import, sync to QB, notify. Returns combined stats."""
    from integrations.collegeone_scraper import refresh_and_import

    # Step 1: scrape + import
    print("→ Scraping CollegeOne...")
    try:
        import_stats = refresh_and_import()
        print(f"  Descargados: {import_stats.get('downloaded', [])}")
        new_payments = (import_stats.get("payments") or {}).get("inserted", 0)
        print(f"  Pagos nuevos importados: {new_payments}")
    except Exception as e:
        print(f"  ✗ Scrape falló: {e}")
        import_stats = {"error": str(e)}
        new_payments = 0

    # Step 2: sync to QB
    print("→ Sincronizando a QuickBooks...")
    sync_result = sync_all(dry_run=dry_run)
    print(f"  {sync_result}")

    # Step 3: notify
    msg = _build_telegram_summary(sync_result)
    if new_payments:
        msg = f"📥 {new_payments} pago(s) nuevos importados\n" + msg
    _send_telegram(msg)

    return {"import": import_stats, "sync": sync_result}


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync CollegeOne payments → QuickBooks")
    ap.add_argument("--dry-run", action="store_true", help="No escribe en QB")
    ap.add_argument("--scrape", action="store_true", help="Scrape CollegeOne primero")
    ap.add_argument("--headed", action="store_true", help="Browser visible (con --scrape)")
    args = ap.parse_args(argv)

    if args.scrape:
        result = daily_cycle(dry_run=args.dry_run, headed=args.headed)
    else:
        result = sync_all(dry_run=args.dry_run)

    import json
    print(json.dumps(result, indent=2, default=str))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
