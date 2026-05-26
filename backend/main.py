"""Simply Personal Assistant — FastAPI app."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import approvals
from agents.tools import TOOLS, dispatch
from claude_client import MODEL, cached_system_block, get_client
from db import connect, init_db
from integrations import collegeone, quickbooks

app = FastAPI(title="Simply Personal Assistant")
init_db()

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


# ---- Chat -------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role, content}]


@app.post("/chat")
def chat(req: ChatRequest):
    client = get_client()
    messages = list(req.history) + [{"role": "user", "content": req.message}]

    # Tool-use loop — keep calling Claude until it stops requesting tools.
    final_text = ""
    for _ in range(6):  # safety cap
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=cached_system_block(),
            tools=TOOLS,
            messages=messages,
        )
        assistant_blocks = [b.model_dump() for b in resp.content]
        messages.append({"role": "assistant", "content": assistant_blocks})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break

        tool_results = []
        for tu in tool_uses:
            try:
                result = dispatch(tu.name, tu.input)
            except Exception as e:
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_results})

    # Persist user + final assistant message for history
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (role, content) VALUES (?, ?)",
            ("user", req.message),
        )
        conn.execute(
            "INSERT INTO chat_messages (role, content) VALUES (?, ?)",
            ("assistant", final_text),
        )

    return {"reply": final_text, "history": messages}


# ---- Approval queue ---------------------------------------------------------

@app.get("/queue")
def queue_list(status: str = "pending"):
    if status == "pending":
        return approvals.list_pending()
    return approvals.list_all()


@app.post("/queue/{queue_id}/approve")
def queue_approve(queue_id: int, edited_payload: dict | None = None):
    item = approvals.decide(queue_id, "approved", edited_payload)
    if not item:
        raise HTTPException(404, "queue item not found")
    # TODO: dispatch to executor based on action_type. For now, just mark approved.
    return item


@app.post("/queue/{queue_id}/reject")
def queue_reject(queue_id: int):
    item = approvals.decide(queue_id, "rejected")
    if not item:
        raise HTTPException(404, "queue item not found")
    return item


# ---- QuickBooks OAuth -------------------------------------------------------

@app.get("/qb/status")
def qb_status():
    return {"connected": quickbooks.is_connected()}


@app.get("/qb/connect")
def qb_connect():
    return RedirectResponse(quickbooks.get_auth_url())


@app.get("/qb/callback")
def qb_callback(code: str, realmId: str):
    tokens = quickbooks.handle_callback(code, realmId)
    return {"connected": True, "realm_id": tokens["realm_id"]}


# ---- CollegeOne paste ingest ------------------------------------------------

class PasteRequest(BaseModel):
    text: str


@app.post("/collegeone/parse")
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
