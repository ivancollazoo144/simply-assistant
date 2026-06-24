"""QuickBooks Online integration (read + OAuth, write later).

OAuth uses intuitlib; data reads use the REST API directly via httpx so we
don't carry the python-quickbooks ORM overhead.

Setup:
  1. Create an app at https://developer.intuit.com
  2. Add redirect URI: http://localhost:8765/qb/callback
  3. Put QB_CLIENT_ID / QB_CLIENT_SECRET / QB_REDIRECT_URI in .env
  4. QB_ENVIRONMENT=sandbox or production
  5. Visit /qb/connect to do the OAuth dance

Tokens persist to data/qb_tokens.json. Access token refreshes automatically
on 401 or when within 60s of expiry.
"""
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

TOKENS_PATH = Path(os.getenv("QB_TOKEN_PATH",
                             Path(__file__).parent.parent.parent / "data" / "qb_tokens.json"))

PROD_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
MINOR_VERSION = "73"


def _env() -> str:
    return os.getenv("QB_ENVIRONMENT", "sandbox")


def _api_base() -> str:
    return PROD_BASE if _env() == "production" else SANDBOX_BASE


# ---- OAuth ------------------------------------------------------------------

def is_connected() -> bool:
    return TOKENS_PATH.exists()


def _load_tokens() -> dict | None:
    if not TOKENS_PATH.exists():
        return None
    return json.loads(TOKENS_PATH.read_text())


def _save_tokens(t: dict) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(t, indent=2))


def _auth_client():
    from intuitlib.client import AuthClient
    return AuthClient(
        client_id=os.environ["QB_CLIENT_ID"],
        client_secret=os.environ["QB_CLIENT_SECRET"],
        redirect_uri=os.environ["QB_REDIRECT_URI"],
        environment=_env(),
    )


def get_auth_url() -> str:
    from intuitlib.enums import Scopes
    return _auth_client().get_authorization_url([Scopes.ACCOUNTING])


def handle_callback(auth_code: str, realm_id: str) -> dict:
    client = _auth_client()
    client.get_bearer_token(auth_code, realm_id=realm_id)
    tokens = {
        "access_token": client.access_token,
        "refresh_token": client.refresh_token,
        "realm_id": realm_id,
        "environment": _env(),
        "expires_at": int(time.time()) + (client.expires_in or 3600) - 60,
    }
    _save_tokens(tokens)
    return {"connected": True, "realm_id": realm_id, "environment": _env()}


def _refresh_if_needed(tokens: dict) -> dict:
    """Refresh access token if expired (or within 60s). Returns updated tokens."""
    if tokens.get("expires_at", 0) > int(time.time()):
        return tokens
    client = _auth_client()
    client.refresh(refresh_token=tokens["refresh_token"])
    tokens["access_token"] = client.access_token
    tokens["refresh_token"] = client.refresh_token or tokens["refresh_token"]
    tokens["expires_at"] = int(time.time()) + (client.expires_in or 3600) - 60
    _save_tokens(tokens)
    return tokens


def _get_active_tokens() -> dict:
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError("QuickBooks not connected — visit /qb/connect first")
    return _refresh_if_needed(tokens)


# ---- REST helpers -----------------------------------------------------------

def _request(method: str, path: str, **kwargs) -> dict:
    """Authenticated request against the QBO API. Auto-refreshes once on 401."""
    tokens = _get_active_tokens()
    url = f"{_api_base()}/v3/company/{tokens['realm_id']}{path}"
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Accept": "application/json",
    }
    params = kwargs.pop("params", {}) or {}
    params.setdefault("minorversion", MINOR_VERSION)
    with httpx.Client(timeout=30.0) as c:
        resp = c.request(method, url, headers=headers, params=params, **kwargs)
        if resp.status_code == 401:
            # Force refresh & retry once
            tokens["expires_at"] = 0
            tokens = _refresh_if_needed(tokens)
            headers["Authorization"] = f"Bearer {tokens['access_token']}"
            resp = c.request(method, url, headers=headers, params=params, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"QBO {method} {path} → {resp.status_code}: {resp.text[:400]}")
        return resp.json()


def _query(qbo_sql: str) -> list[dict]:
    """Run a QBO-SQL query (their dialect) and return entity rows."""
    data = _request("GET", "/query", params={"query": qbo_sql})
    qr = data.get("QueryResponse", {})
    # First non-meta key is the entity name (e.g., 'Account', 'Invoice')
    for k, v in qr.items():
        if isinstance(v, list):
            return v
    return []


def _report(name: str, params: dict | None = None) -> dict:
    return _request("GET", f"/reports/{name}", params=params or {})


# ---- Read APIs --------------------------------------------------------------

def list_accounts(active_only: bool = True) -> list[dict]:
    """Chart of accounts. Returns id, name, type, subtype, balance, active."""
    where = "WHERE Active = true" if active_only else ""
    rows = _query(f"SELECT * FROM Account {where} ORDER BY Name MAXRESULTS 1000")
    return [{
        "id": a["Id"],
        "name": a["Name"],
        "fully_qualified_name": a.get("FullyQualifiedName"),
        "type": a.get("AccountType"),
        "subtype": a.get("AccountSubType"),
        "balance": a.get("CurrentBalance", 0),
        "active": a.get("Active", True),
    } for a in rows]


def list_customers(active_only: bool = True, max_results: int = 1000) -> list[dict]:
    where = "WHERE Active = true" if active_only else ""
    rows = _query(f"SELECT * FROM Customer {where} ORDER BY DisplayName MAXRESULTS {max_results}")
    return [{
        "id": c["Id"],
        "name": c.get("DisplayName"),
        "email": (c.get("PrimaryEmailAddr") or {}).get("Address"),
        "phone": (c.get("PrimaryPhone") or {}).get("FreeFormNumber"),
        "balance": c.get("Balance", 0),
    } for c in rows]


def list_recent_transactions(days: int = 30, max_results: int = 200) -> list[dict]:
    """Recent purchases (expenses) from QB. For categorization workflows."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = _query(
        f"SELECT * FROM Purchase WHERE TxnDate >= '{since}' "
        f"ORDER BY TxnDate DESC MAXRESULTS {max_results}"
    )
    return [_norm_purchase(p) for p in rows]


def list_open_invoices(max_results: int = 200) -> list[dict]:
    rows = _query(
        f"SELECT * FROM Invoice WHERE Balance > '0' "
        f"ORDER BY TxnDate DESC MAXRESULTS {max_results}"
    )
    return [{
        "id": i["Id"],
        "doc_number": i.get("DocNumber"),
        "customer_id": (i.get("CustomerRef") or {}).get("value"),
        "customer_name": (i.get("CustomerRef") or {}).get("name"),
        "txn_date": i.get("TxnDate"),
        "due_date": i.get("DueDate"),
        "total": i.get("TotalAmt", 0),
        "balance": i.get("Balance", 0),
    } for i in rows]


def profit_and_loss(start: str | None = None, end: str | None = None) -> dict:
    """P&L summary report. Dates are YYYY-MM-DD. Defaults to year-to-date."""
    params: dict[str, str] = {"summarize_column_by": "Total"}
    if start:
        params["start_date"] = start
    if end:
        params["end_date"] = end
    raw = _report("ProfitAndLoss", params)
    return _flatten_pl(raw)


def balance_summary() -> dict:
    """One-line cash/bank balances + AR + AP. Quick dashboard view."""
    accounts = list_accounts(active_only=True)
    by_type: dict[str, float] = {}
    for a in accounts:
        t = a["type"]
        by_type[t] = round(by_type.get(t, 0) + (a["balance"] or 0), 2)
    return {
        "environment": _env(),
        "bank_total": by_type.get("Bank", 0),
        "accounts_receivable": by_type.get("Accounts Receivable", 0),
        "accounts_payable": by_type.get("Accounts Payable", 0),
        "credit_cards": by_type.get("Credit Card", 0),
        "by_type": by_type,
    }


def company_info() -> dict:
    tokens = _get_active_tokens()
    data = _request("GET", f"/companyinfo/{tokens['realm_id']}")
    info = (data.get("CompanyInfo") or {})
    return {
        "company_name": info.get("CompanyName"),
        "legal_name": info.get("LegalName"),
        "country": info.get("Country"),
        "fiscal_year_start": info.get("FiscalYearStartMonth"),
        "realm_id": tokens["realm_id"],
        "environment": _env(),
    }


# ---- Normalizers ------------------------------------------------------------

def _norm_purchase(p: dict) -> dict:
    lines = []
    for li in p.get("Line", []) or []:
        det = li.get("AccountBasedExpenseLineDetail") or {}
        acct = det.get("AccountRef") or {}
        lines.append({
            "amount": li.get("Amount", 0),
            "account_id": acct.get("value"),
            "account_name": acct.get("name"),
            "description": li.get("Description"),
        })
    payee = p.get("EntityRef") or {}
    return {
        "id": p["Id"],
        "txn_date": p.get("TxnDate"),
        "total": p.get("TotalAmt", 0),
        "payment_type": p.get("PaymentType"),
        "payee_name": payee.get("name"),
        "payee_id": payee.get("value"),
        "doc_number": p.get("DocNumber"),
        "memo": p.get("PrivateNote"),
        "lines": lines,
    }


def _flatten_pl(report: dict) -> dict:
    """Pull the income/expense/net values out of a P&L report.

    QBO report JSON nests `Rows.Row[]` with optional `Summary.ColData`. We walk
    it and surface a flat dict of {section_name: amount}.
    """
    out: dict[str, Any] = {
        "name": report.get("Header", {}).get("ReportName"),
        "start_date": report.get("Header", {}).get("StartPeriod"),
        "end_date": report.get("Header", {}).get("EndPeriod"),
        "sections": [],
        "totals": {},
    }
    rows = (report.get("Rows") or {}).get("Row", [])
    for r in rows:
        header = (r.get("Header") or {}).get("ColData", [])
        summary = (r.get("Summary") or {}).get("ColData", [])
        if header and summary and len(summary) >= 2:
            label = header[0].get("value")
            try:
                value = float(summary[-1].get("value") or 0)
            except ValueError:
                value = 0
            out["sections"].append({"label": label, "amount": value})
            if label and label.lower() in ("total income", "total expenses",
                                           "net income", "gross profit",
                                           "net operating income"):
                out["totals"][label] = value
    return out


# ---- Write API --------------------------------------------------------------

def create_account(name: str, account_type: str, account_sub_type: str | None = None,
                   description: str = "") -> dict:
    """Create a new account in QB's chart of accounts.

    account_type examples: 'Income', 'Expense', 'Bank', 'Other Income'.
    Common subtypes for school use:
      - Income: 'ServiceFeeIncome', 'SalesOfProductIncome', 'OtherPrimaryIncome'
      - Expense: 'SuppliesMaterials', 'Utilities', 'Travel', 'Other', 'CostOfLabor'
    """
    body: dict[str, Any] = {"Name": name, "AccountType": account_type}
    if account_sub_type:
        body["AccountSubType"] = account_sub_type
    if description:
        body["Description"] = description
    data = _request("POST", "/account", json=body)
    a = data.get("Account") or {}
    return {"id": a.get("Id"), "name": a.get("Name"),
            "type": a.get("AccountType"), "subtype": a.get("AccountSubType"),
            "active": a.get("Active", True)}


def deactivate_account(account_id: str) -> dict:
    """Mark an account inactive (it disappears from pickers but data is preserved).

    Reversible — call update_account with Active=true to bring it back.
    """
    # Fetch first so we have the SyncToken QBO requires for updates
    existing = _request("GET", f"/account/{account_id}").get("Account") or {}
    if not existing:
        return {"error": f"account {account_id} not found"}
    body = {
        "Id": existing["Id"],
        "SyncToken": existing["SyncToken"],
        "Name": existing["Name"],  # QBO requires Name even with sparse=true
        "sparse": True,
        "Active": False,
    }
    data = _request("POST", "/account", params={"operation": "update"}, json=body)
    a = data.get("Account") or {}
    return {"id": a.get("Id"), "name": a.get("Name"), "active": a.get("Active")}


def categorize_purchase(purchase_id: str, account_id: str, account_name: str = "") -> dict:
    """Update a Purchase row to point its single expense line at a different account.

    Assumes one line per Purchase (typical for bank-feed entries). For multi-line
    Purchases we'd need to specify which line, but those are rare in this dataset.
    """
    existing = _request("GET", f"/purchase/{purchase_id}").get("Purchase") or {}
    if not existing:
        return {"error": f"purchase {purchase_id} not found"}
    lines = existing.get("Line", [])
    if not lines:
        return {"error": "purchase has no lines"}
    for li in lines:
        det = li.get("AccountBasedExpenseLineDetail")
        if det is None:
            continue
        det["AccountRef"] = {"value": account_id}
        if account_name:
            det["AccountRef"]["name"] = account_name
    body = {**existing, "sparse": True, "Line": lines}
    data = _request("POST", "/purchase", params={"operation": "update"}, json=body)
    p = data.get("Purchase") or {}
    return {"id": p.get("Id"), "txn_date": p.get("TxnDate"),
            "total": p.get("TotalAmt"), "new_account_id": account_id}


def list_items(active_only: bool = True, max_results: int = 1000) -> list[dict]:
    """Products/Services available for invoice lines."""
    where = "WHERE Active = true" if active_only else ""
    rows = _query(f"SELECT * FROM Item {where} ORDER BY Name MAXRESULTS {max_results}")
    return [{
        "id": i["Id"],
        "name": i.get("Name"),
        "fully_qualified_name": i.get("FullyQualifiedName"),
        "type": i.get("Type"),
        "income_account_id": (i.get("IncomeAccountRef") or {}).get("value"),
        "income_account_name": (i.get("IncomeAccountRef") or {}).get("name"),
        "active": i.get("Active", True),
    } for i in rows]


def get_invoice(invoice_id: str) -> dict | None:
    data = _request("GET", f"/invoice/{invoice_id}")
    return data.get("Invoice")


def create_customer(display_name: str, first_name: str = "", last_name: str = "",
                    email: str = "", phone: str = "") -> dict:
    """Create a new customer in QuickBooks Online.

    Args:
      display_name: unique name shown in QB (required by QBO).
      first_name / last_name: optional parsed name fields.
      email / phone: optional contact info.
    """
    body: dict = {"DisplayName": display_name}
    if first_name or last_name:
        body["GivenName"] = first_name
        body["FamilyName"] = last_name
    if email:
        body["PrimaryEmailAddr"] = {"Address": email}
    if phone:
        body["PrimaryPhone"] = {"FreeFormNumber": phone}
    data = _request("POST", "/customer", json=body)
    c = data.get("Customer") or {}
    return {
        "id": c.get("Id"),
        "name": c.get("DisplayName"),
        "email": (c.get("PrimaryEmailAddr") or {}).get("Address"),
        "phone": (c.get("PrimaryPhone") or {}).get("FreeFormNumber"),
    }


def create_invoice(customer_id: str, item_id: str, amount: float,
                   txn_date: str | None = None, description: str = "",
                   doc_number: str | None = None) -> dict:
    """Create a single-line invoice for a customer.

    Args:
      customer_id: QBO Customer Id.
      item_id: QBO Item (Product/Service) Id used for the line.
      amount: line + invoice total.
      txn_date: YYYY-MM-DD; defaults to today (QBO default if omitted).
      description: optional line description (e.g. "Matrícula 2026-2027").
      doc_number: optional custom invoice number.
    """
    line: dict[str, Any] = {
        "DetailType": "SalesItemLineDetail",
        "Amount": round(amount, 2),
        "SalesItemLineDetail": {"ItemRef": {"value": item_id}},
    }
    if description:
        line["Description"] = description
    body: dict[str, Any] = {
        "CustomerRef": {"value": customer_id},
        "Line": [line],
    }
    if txn_date:
        body["TxnDate"] = txn_date
    if doc_number:
        body["DocNumber"] = doc_number
    data = _request("POST", "/invoice", json=body)
    inv = data.get("Invoice") or {}
    return {
        "id": inv.get("Id"),
        "doc_number": inv.get("DocNumber"),
        "customer_id": (inv.get("CustomerRef") or {}).get("value"),
        "total": inv.get("TotalAmt"),
        "balance": inv.get("Balance"),
        "txn_date": inv.get("TxnDate"),
    }


def create_payment(customer_id: str, invoice_id: str, amount: float,
                   txn_date: str | None = None,
                   deposit_account_id: str | None = None,
                   payment_method_id: str | None = None,
                   memo: str = "") -> dict:
    """Record a payment from a customer and apply it to one invoice.

    Args:
      customer_id: QBO Customer Id (must match the invoice's customer).
      invoice_id: QBO Invoice Id to apply the payment against.
      amount: payment amount (can be a partial payment).
      txn_date: YYYY-MM-DD; defaults to today.
      deposit_account_id: Bank/Other Current Asset account to deposit into.
        If omitted, QBO routes it to Undeposited Funds.
      payment_method_id: optional PaymentMethod Id (Cash/Check/etc.).
      memo: optional private note.
    """
    body: dict[str, Any] = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": round(amount, 2),
        "Line": [{
            "Amount": round(amount, 2),
            "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
        }],
    }
    if txn_date:
        body["TxnDate"] = txn_date
    if deposit_account_id:
        body["DepositToAccountRef"] = {"value": deposit_account_id}
    if payment_method_id:
        body["PaymentMethodRef"] = {"value": payment_method_id}
    if memo:
        body["PrivateNote"] = memo[:4000]
    data = _request("POST", "/payment", json=body)
    p = data.get("Payment") or {}
    return {
        "id": p.get("Id"),
        "customer_id": (p.get("CustomerRef") or {}).get("value"),
        "total": p.get("TotalAmt"),
        "unapplied": p.get("UnappliedAmt"),
        "txn_date": p.get("TxnDate"),
    }


def create_purchase(txn_date: str, amount: float, payment_account_id: str,
                    expense_account_id: str, payment_type: str = "Cash",
                    payee_name: str = "", memo: str = "") -> dict:
    """Create a new Purchase (expense) in QB.

    Args:
      txn_date: YYYY-MM-DD
      amount: total
      payment_account_id: the Bank or CreditCard account the money came out of
      expense_account_id: the Expense/COGS account to categorize as
      payment_type: 'Cash' | 'Check' | 'CreditCard'
      payee_name: vendor name (creates a Vendor if not found — handled by QBO)
      memo: free-text private note (the receipt's raw text often goes here)
    """
    body: dict[str, Any] = {
        "AccountRef": {"value": payment_account_id},
        "PaymentType": payment_type,
        "TxnDate": txn_date,
        "TotalAmt": round(amount, 2),
        "Line": [{
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": round(amount, 2),
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": expense_account_id},
            },
        }],
    }
    if memo:
        body["PrivateNote"] = memo[:4000]
    if payee_name:
        # Resolve or create the vendor
        vendor_id = _resolve_vendor(payee_name)
        if vendor_id:
            body["EntityRef"] = {"value": vendor_id, "type": "Vendor"}

    data = _request("POST", "/purchase", json=body)
    p = data.get("Purchase") or {}
    return {"id": p.get("Id"), "txn_date": p.get("TxnDate"),
            "total": p.get("TotalAmt"), "payee": payee_name}


def _resolve_vendor(name: str) -> str | None:
    """Find a Vendor by name or create one. Returns the Vendor Id."""
    safe = name.replace("'", "''")
    rows = _query(f"SELECT * FROM Vendor WHERE DisplayName = '{safe}' MAXRESULTS 1")
    if rows:
        return rows[0]["Id"]
    # Create
    try:
        data = _request("POST", "/vendor", json={"DisplayName": name})
        return (data.get("Vendor") or {}).get("Id")
    except Exception:
        return None
