# Simply Personal Assistant

Admin AI assistant for the school. Runs locally on Mac.

## What it does (Phase 1)

- **Bookkeeping** — proposes QuickBooks transaction categorizations, monthly summaries, tax-prep exports.
- **Payments** — tracks which of 72 families paid tuition, who's late, drafts WhatsApp reminders.
- **Chat interface** — ask in Spanish or English. Every action that touches QuickBooks or external systems goes through an **approval queue** — nothing runs without your OK.

Future phases: file digitization (phone-camera OCR), message triage, scheduling, document generation, vendor management.

## Architecture

```
~/simply-assistant/
├── backend/                FastAPI app
│   ├── main.py             API + serves static UI
│   ├── db.py               SQLite schema + helpers
│   ├── queue.py            Approval queue logic
│   ├── claude_client.py    Claude API wrapper (with caching)
│   ├── agents/             Agent logic per domain
│   │   ├── bookkeeper.py
│   │   └── payments.py
│   ├── integrations/
│   │   ├── quickbooks.py   QBO API (read-write, OAuth)
│   │   └── collegeone.py   Paste parser (no API)
│   └── static/             Chat + queue UI
├── data/                   SQLite DB + uploaded files
└── .env                    Secrets (not committed)
```

## Setup

```bash
cd ~/simply-assistant/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example ../.env
# Then edit ../.env with your keys (see below)

# Run
uvicorn main:app --reload --port 8765
# Open http://localhost:8765
```

## Keys you'll need

1. **Anthropic API key** — https://console.anthropic.com → API Keys. Set `ANTHROPIC_API_KEY`.
2. **QuickBooks** — https://developer.intuit.com → create an app. Get Client ID + Secret. Set `QB_CLIENT_ID`, `QB_CLIENT_SECRET`. (First-run OAuth flow stores tokens.)

That's it for Phase 1. CollegeOne uses paste, WhatsApp uses copy/paste.

## How the approval queue works

1. You ask: *"Categorize last month's transactions"*
2. Assistant pulls transactions from QB, proposes categories, drops them in queue.
3. You review in the UI — approve in bulk, edit a few, reject any.
4. Only approved items get written back to QB.

Same pattern for tuition reminders, invoice creation, anything that touches the outside world.

## Cost

Targeting under $60/month total — mostly Claude API. Prompt caching keeps it low.
