"""QuickBooks Online integration (read-write).

Setup: create an app at https://developer.intuit.com, set client ID/secret in .env,
then visit /qb/connect in the running app to do the OAuth dance.
"""
import json
import os
from pathlib import Path
from typing import Any

TOKEN_PATH = Path(os.getenv("QB_TOKEN_PATH", "../data/qb_tokens.json")).resolve()


def is_connected() -> bool:
    return TOKEN_PATH.exists()


def load_tokens() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    return json.loads(TOKEN_PATH.read_text())


def save_tokens(tokens: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))


def get_auth_url() -> str:
    """Step 1 of OAuth — returns URL Ivan visits to grant access."""
    from intuitlib.client import AuthClient
    from intuitlib.enums import Scopes

    auth_client = AuthClient(
        client_id=os.environ["QB_CLIENT_ID"],
        client_secret=os.environ["QB_CLIENT_SECRET"],
        redirect_uri=os.environ["QB_REDIRECT_URI"],
        environment=os.getenv("QB_ENVIRONMENT", "sandbox"),
    )
    return auth_client.get_authorization_url([Scopes.ACCOUNTING])


def handle_callback(auth_code: str, realm_id: str) -> dict:
    """Step 2 — exchange auth code for tokens."""
    from intuitlib.client import AuthClient

    auth_client = AuthClient(
        client_id=os.environ["QB_CLIENT_ID"],
        client_secret=os.environ["QB_CLIENT_SECRET"],
        redirect_uri=os.environ["QB_REDIRECT_URI"],
        environment=os.getenv("QB_ENVIRONMENT", "sandbox"),
    )
    auth_client.get_bearer_token(auth_code, realm_id=realm_id)
    tokens = {
        "access_token": auth_client.access_token,
        "refresh_token": auth_client.refresh_token,
        "realm_id": realm_id,
        "environment": os.getenv("QB_ENVIRONMENT", "sandbox"),
    }
    save_tokens(tokens)
    return tokens


# ---- Read API stubs (Phase 1 will flesh these out) -----------------------

def list_recent_transactions(days: int = 30) -> list[dict]:
    """Pull recent transactions from QB for the bookkeeper to categorize."""
    raise NotImplementedError("Wire up after first OAuth connection. See README.")


def list_open_invoices() -> list[dict]:
    """Outstanding invoices (tuition not yet paid)."""
    raise NotImplementedError("Wire up after first OAuth connection.")


# ---- Write API (only called from approval-queue executor) ----------------

def create_invoice(payload: dict[str, Any]) -> dict:
    """Create an invoice in QB. Called only by queue executor after approval."""
    raise NotImplementedError("Wire up after first OAuth connection.")


def categorize_transaction(qb_txn_id: str, category: str) -> dict:
    """Update a transaction's category in QB. Approval-gated."""
    raise NotImplementedError("Wire up after first OAuth connection.")
