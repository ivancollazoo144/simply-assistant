"""Simply Personal Assistant — FastAPI app."""
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ---- API auth ---------------------------------------------------------------

def _ensure_api_token() -> str:
    """Read SIMPLY_API_TOKEN from env. Required — fail loudly if missing.

    Add to .env on the host: `SIMPLY_API_TOKEN=<token>`.
    Generate one via: `python -c "import secrets; print(secrets.token_urlsafe(40))"`.
    """
    tok = os.getenv("SIMPLY_API_TOKEN")
    if not tok:
        raise RuntimeError(
            "SIMPLY_API_TOKEN missing from .env — generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(40))\"` "
            "and add it to .env, then restart."
        )
    return tok


API_TOKEN = _ensure_api_token()


def require_token(
    authorization: str | None = Header(default=None),
    token: str | None = None,  # query param fallback
) -> None:
    """Require Bearer auth on protected routes. Allows ?token=... as fallback
    so simple browser links / curl tests work."""
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(None, 1)[1].strip()
    elif token:
        presented = token
    if not presented or not secrets.compare_digest(presented, API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

import approvals
import chat as chat_engine
import executor
from claude_client import MODEL
from db import connect, init_db
from integrations import collegeone, google_workspace, quickbooks
from integrations import telegram_bot

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await telegram_bot.start_polling()
    try:
        yield
    finally:
        await telegram_bot.stop_polling()


app = FastAPI(title="Simply Personal Assistant", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


# ---- Chat -------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role, content}]


@app.post("/chat", dependencies=[Depends(require_token)])
def chat(req: ChatRequest):
    turn = chat_engine.run_turn(req.message, req.history)
    return {"reply": turn.text, "history": turn.history, "queued_ids": turn.queued_ids}


# ---- Approval queue ---------------------------------------------------------

@app.get("/queue", dependencies=[Depends(require_token)])
def queue_list(status: str = "pending"):
    if status == "pending":
        return approvals.list_pending()
    return approvals.list_all()


@app.post("/queue/{queue_id}/approve", dependencies=[Depends(require_token)])
def queue_approve(queue_id: int, edited_payload: dict | None = None):
    item = approvals.decide(queue_id, "approved", edited_payload)
    if not item:
        raise HTTPException(404, "queue item not found")
    success, result = executor.execute(item["action_type"], item["payload"])
    approvals.mark_executed(queue_id, success, result)
    return approvals.get(queue_id)


@app.post("/queue/{queue_id}/reject", dependencies=[Depends(require_token)])
def queue_reject(queue_id: int):
    item = approvals.decide(queue_id, "rejected")
    if not item:
        raise HTTPException(404, "queue item not found")
    return item


# ---- QuickBooks OAuth -------------------------------------------------------

@app.get("/qb/status", dependencies=[Depends(require_token)])
def qb_status():
    return {"connected": quickbooks.is_connected()}


@app.get("/qb/connect", dependencies=[Depends(require_token)])
def qb_connect():
    return RedirectResponse(quickbooks.get_auth_url())


@app.get("/qb/callback")
def qb_callback(code: str, realmId: str):
    # NO auth — Intuit's servers hit this with no Bearer header
    tokens = quickbooks.handle_callback(code, realmId)
    return {"connected": True, "realm_id": tokens["realm_id"]}


@app.post("/qb/bookkeeper/propose", dependencies=[Depends(require_token)])
def qb_bookkeeper_propose(limit: int = 20, since: str | None = None):
    """Find QB transactions needing categorization, ask Claude for proposals,
    queue each for Ivan's approval. Returns counts + queue IDs."""
    import bookkeeper
    return bookkeeper.propose_categorizations(limit=limit, since=since)


@app.post("/qb/chart/propose-cleanup", dependencies=[Depends(require_token)])
def qb_propose_cleanup():
    """Queue the chart-of-accounts cleanup (Path B: add school accounts +
    deactivate unused QBO defaults). Each action lands in the approval queue."""
    import chart_cleanup
    return chart_cleanup.propose_chart_cleanup()


# ---- Matrícula invoicing ----------------------------------------------------

@app.post("/admin/matricula/plan", dependencies=[Depends(require_token)])
async def matricula_plan(request: Request):
    """Upload the payments CSV (raw request body) and get the dry-run plan back.

    Writes the CSV to data/matriculas.csv, then matches every student to a QB
    customer. Returns matched / needs_review / not_found plus items + accounts.
    """
    body = await request.body()
    dest = Path("/app/data/matriculas.csv")
    dest.write_bytes(body)
    import matricula_payments as mp
    return mp.plan_json(dest)


@app.post("/admin/matricula/run", dependencies=[Depends(require_token)])
def matricula_run(item_id: str, deposit_account_id: str | None = None,
                  payment_method_id: str | None = None, date: str | None = None,
                  include_review: bool = False):
    """Execute invoices + payments in QB for the previously uploaded CSV.

    Idempotent — students already invoiced/paid (per the run log) are skipped.
    """
    import matricula_payments as mp
    return mp.execute_json(Path("/app/data/matriculas.csv"), item_id, date,
                           deposit_account_id, payment_method_id, include_review)


@app.post("/admin/matricula/plan-balance", dependencies=[Depends(require_token)])
async def matricula_plan_balance(request: Request):
    """Upload a CollegeOne Items Balance Report CSV and get the dry-run plan.

    Shows which 'Paid' students will have payments applied, which are already
    recorded, and which couldn't be matched in QB.
    """
    body = await request.body()
    dest = Path("/app/data/matriculas_balance.csv")
    dest.write_bytes(body)
    import matricula_payments as mp
    return mp.plan_items_balance_json(dest)


@app.post("/admin/matricula/run-balance", dependencies=[Depends(require_token)])
def matricula_run_balance(item_id: str, deposit_account_id: str | None = None,
                          payment_method_id: str | None = None, date: str | None = None,
                          include_review: bool = False):
    """Apply QB payments for 'Paid' students in the previously uploaded balance CSV.

    Idempotent — students already in the run log are skipped.
    """
    import matricula_payments as mp
    return mp.execute_items_balance_json(
        Path("/app/data/matriculas_balance.csv"), item_id, date,
        deposit_account_id, payment_method_id, include_review)


@app.get("/admin/expenses", dependencies=[Depends(require_token)])
def admin_expenses(days: int = 365, max_results: int = 1000):
    """Raw expense (Purchase) transactions over the last `days`, for analysis.

    Includes payee_name, memo, total, date, and per-line account info so the
    caller can group/identify recurring vendors (memo holds the merchant when
    EntityRef is blank on bank-feed entries).
    """
    return {"purchases": quickbooks.list_recent_transactions(days=days,
                                                             max_results=max_results)}


@app.post("/admin/qb/customer", dependencies=[Depends(require_token)])
def admin_create_customer(display_name: str, first_name: str = "",
                          last_name: str = "", email: str = "", phone: str = ""):
    """Create a new QB customer by name."""
    return quickbooks.create_customer(display_name, first_name, last_name, email, phone)


@app.delete("/admin/qb/customer/{customer_id}", dependencies=[Depends(require_token)])
def admin_deactivate_customer(customer_id: str):
    """Make a QB customer inactive (reversible soft-delete)."""
    return quickbooks.deactivate_customer(customer_id)


@app.get("/qb/disconnect")
def qb_disconnect():
    """Public disconnect URL Intuit requires for app listing. Removes local tokens."""
    from pathlib import Path
    try:
        Path(quickbooks.TOKENS_PATH).unlink(missing_ok=True)
        return {"disconnected": True, "message": "Simply Assistant QuickBooks access revoked locally."}
    except Exception as e:
        return {"disconnected": False, "error": str(e)}


# ---- Google OAuth -----------------------------------------------------------

@app.get("/google/status", dependencies=[Depends(require_token)])
def google_status():
    return {"connected": google_workspace.is_connected()}


@app.get("/google/connect", dependencies=[Depends(require_token)])
def google_connect():
    return RedirectResponse(google_workspace.get_auth_url())


@app.get("/google/callback")
def google_callback(code: str):
    # NO auth — Google's servers hit this with no Bearer header
    return google_workspace.handle_callback(code)


# ---- CollegeOne paste ingest ------------------------------------------------

class PasteRequest(BaseModel):
    text: str


@app.post("/collegeone/parse", dependencies=[Depends(require_token)])
def collegeone_parse(req: PasteRequest):
    parsed = collegeone.parse_paste(req.text)
    # Drop into approval queue for Ivan to review before inserting into DB.
    qid = approvals.enqueue(
        agent="records",
        action_type=f"collegeone.import_{parsed.get('type', 'unknown')}",
        summary=f"Import {len(parsed.get('records', []))} {parsed.get('type', 'unknown')} records from CollegeOne paste",
        payload=parsed,
    )
    return {"queued": True, "queue_id": qid, "preview": parsed}


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "qb_connected": quickbooks.is_connected()}
