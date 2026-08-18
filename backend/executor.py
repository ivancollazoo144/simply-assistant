"""Action executor — runs approved queue items.

Dispatch table maps action_type → callable(payload) → dict result.
Returns (success, result_dict). The caller persists this via approvals.mark_executed.
"""
from typing import Any, Callable

from integrations import google_workspace as gw
from integrations import quickbooks


def _exec_calendar_add(payload: dict) -> dict:
    return gw.add_event(
        title=payload["title"],
        start=payload["start"],
        duration_minutes=payload.get("duration_minutes", 60),
        notes=payload.get("notes", ""),
        calendar_id=payload.get("calendar_id", gw.DEFAULT_CALENDAR_ID),
        location=payload.get("location", ""),
    )


def _exec_task_add(payload: dict) -> dict:
    return gw.add_task(
        title=payload["title"],
        due=payload.get("due"),
        notes=payload.get("notes", ""),
        tasklist_id=payload.get("tasklist_id", gw.DEFAULT_TASKLIST_ID),
    )


def _exec_email_draft(payload: dict) -> dict:
    return gw.create_draft(
        to=payload["to"],
        subject=payload["subject"],
        body=payload["body"],
        cc=payload.get("cc", ""),
        bcc=payload.get("bcc", ""),
    )


def _exec_qb_create_account(payload: dict) -> dict:
    return quickbooks.create_account(
        name=payload["name"],
        account_type=payload["account_type"],
        account_sub_type=payload.get("account_sub_type"),
        description=payload.get("description", ""),
    )


def _exec_qb_deactivate_account(payload: dict) -> dict:
    return quickbooks.deactivate_account(account_id=payload["account_id"])


def _exec_qb_categorize_txn(payload: dict) -> dict:
    return quickbooks.categorize_purchase(
        purchase_id=payload["purchase_id"],
        account_id=payload["account_id"],
        account_name=payload.get("account_name", ""),
    )


def _exec_qb_create_expense(payload: dict) -> dict:
    return quickbooks.create_purchase(
        txn_date=payload["txn_date"],
        amount=payload["amount"],
        payment_account_id=payload["payment_account_id"],
        expense_account_id=payload["expense_account_id"],
        payment_type=payload.get("payment_type", "Cash"),
        payee_name=payload.get("payee_name", ""),
        memo=payload.get("memo", ""),
    )


EXECUTORS: dict[str, Callable[[dict], dict]] = {
    "calendar.add_event": _exec_calendar_add,
    "tasks.add_task": _exec_task_add,
    "email.draft": _exec_email_draft,
    "qb.create_account": _exec_qb_create_account,
    "qb.deactivate_account": _exec_qb_deactivate_account,
    "qb.categorize_txn": _exec_qb_categorize_txn,
    "qb.create_expense": _exec_qb_create_expense,
}


def execute(action_type: str, payload: dict) -> tuple[bool, dict[str, Any]]:
    fn = EXECUTORS.get(action_type)
    if not fn:
        return False, {"error": f"no executor registered for action_type={action_type}"}
    try:
        return True, fn(payload)
    except Exception as e:
        return False, {"error": str(e), "error_type": type(e).__name__}
