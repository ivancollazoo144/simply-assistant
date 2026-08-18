"""Bookkeeper agent — rules-based QB transaction categorizer.

Philosophy: only act on payees Ivan has explicitly told us how to handle.
No guessing. For unknown payees, surface them for review so Ivan can either
(a) set a rule that auto-routes that payee, or (b) tell us to skip / always ask.

Tables involved:
  - payee_rules: payee_norm → (qb_account_id, mode='always'|'ask'|'skip')

Modes:
  - always: auto-queue a `qb.categorize_txn` for approval (still gated by Ivan tap)
  - ask:    queue with empty account_id; Ivan picks at approval time
  - skip:   never propose; leave alone
"""
from collections import Counter

import approvals
from db import connect
from integrations import quickbooks

# Accounts that mean "still needs a real category"
GENERIC_TARGETS = {
    "Uncategorized Expense",
    "Meals with clients",
    "Memberships & subscriptions",
    "Supplies & materials",
}


def _normalize_payee(name: str) -> str:
    return (name or "").strip().lower()


def _is_generic(account_full_name: str) -> bool:
    leaf = (account_full_name or "").rsplit(":", 1)[-1]
    return leaf in GENERIC_TARGETS


# ---- Rule CRUD --------------------------------------------------------------

def list_rules() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM payee_rules ORDER BY times_applied DESC, payee_display"
        ).fetchall()
        return [dict(r) for r in rows]


def get_rule(payee: str) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM payee_rules WHERE payee_norm = ?",
            (_normalize_payee(payee),),
        ).fetchone()
        return dict(r) if r else None


def set_rule(payee: str, mode: str, qb_account_id: str | None = None,
             qb_account_name: str = "") -> dict:
    """mode in {'always', 'ask', 'skip'}. 'always' requires qb_account_id."""
    if mode not in ("always", "ask", "skip"):
        raise ValueError("mode must be always|ask|skip")
    if mode == "always" and not qb_account_id:
        raise ValueError("mode='always' requires qb_account_id")
    norm = _normalize_payee(payee)
    if not norm:
        raise ValueError("payee cannot be empty")
    with connect() as conn:
        conn.execute(
            "INSERT INTO payee_rules (payee_norm, payee_display, qb_account_id, "
            "qb_account_name, mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(payee_norm) DO UPDATE SET "
            "qb_account_id=excluded.qb_account_id, "
            "qb_account_name=excluded.qb_account_name, "
            "mode=excluded.mode, "
            "updated_at=CURRENT_TIMESTAMP",
            (norm, payee.strip(), qb_account_id, qb_account_name, mode),
        )
        r = conn.execute(
            "SELECT * FROM payee_rules WHERE payee_norm = ?", (norm,),
        ).fetchone()
        return dict(r)


def delete_rule(payee: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM payee_rules WHERE payee_norm = ?",
            (_normalize_payee(payee),),
        )
        return cur.rowcount > 0


# ---- Discovery --------------------------------------------------------------

def list_unruled_payees(limit: int = 200, since: str | None = None,
                        only_generic: bool = True) -> dict:
    """Inspect recent Purchases. Group by payee, return those without rules.

    Sorted by frequency so Ivan handles the highest-leverage ones first.
    """
    where = f"WHERE TxnDate >= '{since}' " if since else ""
    rows = quickbooks._query(
        f"SELECT * FROM Purchase {where}"
        f"ORDER BY TxnDate DESC MAXRESULTS {limit}"
    )
    counter: Counter[str] = Counter()
    totals: dict[str, float] = {}
    sample_acct: dict[str, str] = {}
    for p in rows:
        for li in (p.get("Line") or []):
            det = li.get("AccountBasedExpenseLineDetail") or {}
            acct_name = (det.get("AccountRef") or {}).get("name", "")
            if only_generic and not _is_generic(acct_name):
                continue
            payee = (p.get("EntityRef") or {}).get("name") or "(sin payee)"
            counter[payee] += 1
            totals[payee] = totals.get(payee, 0) + (li.get("Amount") or 0)
            sample_acct.setdefault(payee, acct_name)
            break

    with connect() as conn:
        ruled = {r[0] for r in conn.execute(
            "SELECT payee_norm FROM payee_rules"
        ).fetchall()}

    unruled = []
    for payee, count in counter.most_common():
        if _normalize_payee(payee) in ruled:
            continue
        unruled.append({
            "payee": payee,
            "txn_count": count,
            "total_amount": round(totals[payee], 2),
            "currently_in": sample_acct[payee],
        })

    return {
        "scanned_purchases": len(rows),
        "unruled_payees": unruled[:50],  # cap for readability
        "unruled_total": len(unruled),
    }


# ---- Propose categorizations ------------------------------------------------

def propose_categorizations(limit: int = 50, since: str | None = None) -> dict:
    """Apply payee rules to recent generic-categorized Purchases.

    Per Purchase:
      - look up rule by payee
      - 'always' → queue qb.categorize_txn with the rule's account
      - 'ask'    → queue qb.categorize_txn with NO account; Ivan picks at approval
      - 'skip'   → ignore
      - no rule  → ignore (use list_unruled_payees to surface them)
    """
    where = f"WHERE TxnDate >= '{since}' " if since else ""
    rows = quickbooks._query(
        f"SELECT * FROM Purchase {where}"
        f"ORDER BY TxnDate DESC MAXRESULTS {max(limit * 4, 100)}"
    )

    with connect() as conn:
        rule_rows = conn.execute("SELECT * FROM payee_rules").fetchall()
        rules = {r["payee_norm"]: dict(r) for r in rule_rows}

    queued_always = 0
    queued_ask = 0
    skipped_no_rule = 0
    skipped_already_clean = 0
    processed = 0

    for p in rows:
        if processed >= limit:
            break
        for li in (p.get("Line") or []):
            det = li.get("AccountBasedExpenseLineDetail") or {}
            acct_name = (det.get("AccountRef") or {}).get("name", "")
            if not _is_generic(acct_name):
                skipped_already_clean += 1
                break
            payee = (p.get("EntityRef") or {}).get("name", "")
            rule = rules.get(_normalize_payee(payee))
            if not rule:
                skipped_no_rule += 1
                break
            if rule["mode"] == "skip":
                break
            processed += 1
            amount = li.get("Amount") or p.get("TotalAmt", 0)
            if rule["mode"] == "always":
                approvals.enqueue(
                    agent="bookkeeper",
                    action_type="qb.categorize_txn",
                    summary=(
                        f"Recategorizar ${amount:.2f} "
                        f"({payee}, {p.get('TxnDate')}): "
                        f"{acct_name} → {rule['qb_account_name']} "
                        f"(regla)"
                    ),
                    payload={
                        "purchase_id": p["Id"],
                        "account_id": rule["qb_account_id"],
                        "account_name": rule["qb_account_name"],
                    },
                )
                queued_always += 1
            else:  # ask
                approvals.enqueue(
                    agent="bookkeeper",
                    action_type="qb.categorize_txn",
                    summary=(
                        f"Categorizar ${amount:.2f} ({payee}, "
                        f"{p.get('TxnDate')}): currently {acct_name}. "
                        f"Regla=ask → escoge cuenta al aprobar."
                    ),
                    payload={
                        "purchase_id": p["Id"],
                        "account_id": None,
                        "account_name": "(pendiente — elegir)",
                        "needs_account_choice": True,
                    },
                )
                queued_ask += 1
            break

    return {
        "scanned": len(rows),
        "processed": processed,
        "queued_auto": queued_always,
        "queued_ask": queued_ask,
        "skipped_no_rule": skipped_no_rule,
        "skipped_already_categorized": skipped_already_clean,
    }
