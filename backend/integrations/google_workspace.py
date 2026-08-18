"""Google Calendar + Tasks integration (OAuth 2.0).

Setup:
  1. Create a Google Cloud project + OAuth client (Web app type).
  2. Add redirect URI: http://localhost:8765/google/callback
  3. Enable Google Calendar API and Google Tasks API.
  4. Put GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env.
  5. Visit /google/connect to authorize the account.

Tokens persist to data/google_tokens.json (refresh token kept for long life).
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

TOKENS_PATH = Path(os.getenv("GOOGLE_TOKENS_PATH",
                             Path(__file__).parent.parent.parent / "data" / "google_tokens.json"))

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts (no auto-send)
    "openid", "https://www.googleapis.com/auth/userinfo.email",
]

REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8765/google/callback")
DEFAULT_CALENDAR_ID = "primary"
DEFAULT_TASKLIST_ID = "@default"
TZ = "America/Puerto_Rico"

# Send emails appearing FROM this address (must be a verified "Send As" alias
# on the authorized Google account, or the account's own address). Override
# in .env with GMAIL_SEND_AS.
SEND_AS = os.getenv("GMAIL_SEND_AS", "simplicitylcd@gmail.com")
SEND_AS_NAME = os.getenv("GMAIL_SEND_AS_NAME", "Simplicity Learning Center")


def _client_config() -> dict:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing in .env")
    return {
        "web": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def _save_tokens(creds: Credentials) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(creds.to_json())


def _load_creds() -> Credentials | None:
    if not TOKENS_PATH.exists():
        return None
    data = json.loads(TOKENS_PATH.read_text())
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        _save_tokens(creds)
    return creds


def is_connected() -> bool:
    try:
        return _load_creds() is not None
    except Exception:
        return False


# Single-user app: keep the in-flight OAuth Flow object alive between the
# /connect redirect and the /callback so the PKCE code_verifier is preserved.
_pending_flow: Flow | None = None


def get_auth_url() -> str:
    global _pending_flow
    _pending_flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _state = _pending_flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    return url


def handle_callback(code: str) -> dict:
    global _pending_flow
    flow = _pending_flow or Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    _pending_flow = None
    _save_tokens(flow.credentials)
    # Lookup which email was authorized
    svc = build("oauth2", "v2", credentials=flow.credentials, cache_discovery=False)
    info = svc.userinfo().get().execute()
    return {"connected": True, "email": info.get("email")}


# ---- Calendar ---------------------------------------------------------------

def _calendar_service():
    creds = _load_creds()
    if not creds:
        raise RuntimeError("Google not connected — visit /google/connect first")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def add_event(title: str, start: str, duration_minutes: int = 60,
              notes: str = "", calendar_id: str = DEFAULT_CALENDAR_ID,
              location: str = "") -> dict:
    """`start` is ISO 8601 local time, e.g. '2026-05-30T14:30'."""
    start_dt = datetime.fromisoformat(start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    body: dict[str, Any] = {
        "summary": title,
        "description": notes,
        "location": location,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TZ},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TZ},
    }
    ev = _calendar_service().events().insert(calendarId=calendar_id, body=body).execute()
    return {"event_id": ev["id"], "html_link": ev.get("htmlLink"),
            "title": title, "start": start_dt.isoformat()}


def list_calendars() -> list[dict]:
    raw = _calendar_service().calendarList().list().execute().get("items", [])
    return [{"id": c["id"], "summary": c["summary"],
             "primary": c.get("primary", False)} for c in raw]


def list_events_today(calendar_id: str = DEFAULT_CALENDAR_ID) -> list[dict]:
    """Return today's events in Ivan's local TZ, sorted by start time."""
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TZ)
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tzinfo=tz)
    end = datetime.combine(today, time.max, tzinfo=tz)
    raw = _calendar_service().events().list(
        calendarId=calendar_id,
        timeMin=start.astimezone(timezone.utc).isoformat(),
        timeMax=end.astimezone(timezone.utc).isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])
    out = []
    for e in raw:
        start_obj = e.get("start", {})
        # All-day events have 'date', timed events have 'dateTime'
        when = start_obj.get("dateTime") or start_obj.get("date")
        out.append({
            "id": e["id"],
            "title": e.get("summary", "(sin título)"),
            "start": when,
            "all_day": "date" in start_obj,
            "location": e.get("location"),
        })
    return out


# ---- Tasks (Reminders replacement) ------------------------------------------

def _tasks_service():
    creds = _load_creds()
    if not creds:
        raise RuntimeError("Google not connected — visit /google/connect first")
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def add_task(title: str, due: str | None = None, notes: str = "",
             tasklist_id: str = DEFAULT_TASKLIST_ID) -> dict:
    """`due` is ISO 8601 date or datetime — Google Tasks API only honors the date."""
    body: dict[str, Any] = {"title": title, "notes": notes}
    if due:
        due_dt = datetime.fromisoformat(due)
        # Tasks API needs RFC3339; only the date portion is shown to user.
        body["due"] = due_dt.strftime("%Y-%m-%dT00:00:00.000Z")
    t = _tasks_service().tasks().insert(tasklist=tasklist_id, body=body).execute()
    return {"task_id": t["id"], "title": title, "due": due, "tasklist": tasklist_id}


def list_tasklists() -> list[dict]:
    raw = _tasks_service().tasklists().list().execute().get("items", [])
    return [{"id": tl["id"], "title": tl["title"]} for tl in raw]


def list_tasks_due_today_or_overdue(tasklist_id: str = DEFAULT_TASKLIST_ID) -> dict:
    """Return uncompleted tasks due today (Ivan's TZ) or earlier.

    Splits into 'overdue' (due before today) and 'today' (due today).
    Google Tasks API stores `due` as a UTC midnight datetime keyed off the
    user-entered date — we compare just the date portion.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(TZ)).date()
    raw = _tasks_service().tasks().list(
        tasklist=tasklist_id, showCompleted=False, maxResults=100,
    ).execute().get("items", [])
    overdue, due_today = [], []
    for t in raw:
        due_str = t.get("due")
        if not due_str:
            continue
        try:
            due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        item = {"id": t["id"], "title": t.get("title", ""), "due": due_date.isoformat()}
        if due_date < today:
            overdue.append(item)
        elif due_date == today:
            due_today.append(item)
    return {"overdue": overdue, "today": due_today}


# ---- Gmail drafts -----------------------------------------------------------

def _gmail_service():
    creds = _load_creds()
    if not creds:
        raise RuntimeError("Google not connected — visit /google/connect first")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_draft(to: str, subject: str, body: str,
                 cc: str = "", bcc: str = "") -> dict:
    """Create a Gmail draft (NEVER sends). Returns draft_id + URL to open in Gmail.

    From address is `SEND_AS` (verified Send-As alias). Must be configured in the
    Gmail account, otherwise Gmail will silently fall back to the account address.
    """
    import base64
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = f"{SEND_AS_NAME} <{SEND_AS}>" if SEND_AS_NAME else SEND_AS
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = _gmail_service().users().drafts().create(
        userId="me", body={"message": {"raw": raw}},
    ).execute()
    draft_id = draft["id"]
    # Permalink-style URL that opens the draft in Gmail web
    url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}"
    return {"draft_id": draft_id, "url": url, "to": to, "subject": subject}
