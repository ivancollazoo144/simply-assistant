# Simply Personal Assistant — Build Plan

**Owner:** Ivan Collazo
**Repo:** `~/simply-assistant`
**Last updated:** 2026-05-27
**Status:** Phase 0 scaffold complete; planning Phase 1 execution

---

## 1. Vision

A single chat interface where Ivan can ask, in Spanish or English, anything about running his bilingual Puerto Rico school (85 students, 72 families) — and have an AI agent do the work. Bookkeeping, tuition tracking, family lookups, message drafts, document generation. Every action that touches the outside world (QuickBooks, WhatsApp, email, file changes) lands in an approval queue first. Nothing executes without Ivan's OK.

**Success in 90 days:** Ivan spends less than 30 minutes per week on routine admin (tuition reconciliation, payment reminders, bookkeeping categorization) and gets back hours per week. All 72 families' records are digital and queryable. QuickBooks stays accurate without manual data entry.

**Non-goals:** Not building a SaaS. Not multi-tenant. Not replacing QuickBooks or CollegeOne — it sits on top of them. No autonomous execution of irreversible actions ever.

---

## 2. Guiding Principles

1. **Approval queue is the spine.** Every external mutation (QB write, message sent, file moved/deleted) is proposed, never auto-executed.
2. **Bilingual by default.** Spanish and English are equal first-class citizens in UI and AI responses.
3. **Local-first, cheap.** Runs on Ivan's Mac. No cloud hosting, no DB server, no per-seat fees.
4. **Bias toward read.** Read access is broad; write access is narrow, opt-in, and gated.
5. **Boring tech.** Python + SQLite + FastAPI. No frameworks Ivan would have to relearn in 6 months.
6. **Build narrow, validate, expand.** Don't build phase 3 until phase 1 is actually saving time.

---

## 3. Phasing & Scope

Priority order is Ivan's: bookkeeping → payments → files → messages → scheduling → docs → vendor/expense.

### Phase 0 — Scaffold (complete)
- FastAPI app, SQLite schema, approval queue, chat UI, QB OAuth flow, CollegeOne paste parser.
- All integration write-paths are stubs that raise `NotImplementedError` — safe by default.

### Phase 1 — Bookkeeping + Payments (target: 2-3 weeks)

**Deliverable:** Ivan can ask "what did I spend on supplies last month?" or "who's behind on May tuition?" and get accurate answers. He can categorize a month of QB transactions in one approval session and send tuition reminders by approving WhatsApp drafts.

Concrete features:
- Import all 72 families + 85 students from CollegeOne via paste sessions (Claude normalizes the paste, queues records for approval, then writes to SQLite).
- QB read: pull last-90-days transactions, account balances, open invoices.
- Bookkeeper agent: proposes a category for each uncategorized transaction, batched in the approval queue. Bulk-approve UI ("approve all in this batch", "reject + recategorize").
- Tuition charge generator: monthly batch creation in QB, approval-gated.
- Tuition reconciliation: match QB payments to expected tuition; flag missing/partial payments.
- Payments agent: drafts WhatsApp + email reminders for late-paying families. Output is copy-pasteable text (no auto-send).
- Monthly summary report: income/expense by category, exportable for tax prep.

**Out of scope for Phase 1:** OCR, scheduling, document templates, vendor tracking.

### Phase 2 — File Digitization + Voice (target: 2-3 weeks)

**Deliverable:** Ivan can scan a paper document with his iPhone, and within seconds find it later by asking in plain language. Voice query support for hands-free use.

- iPhone scan workflow: photo → upload endpoint → Claude vision OCR → extracted text + auto-tags (student name, doc type, date) → searchable archive.
- File search: "find Sofia Martínez's enrollment form from 2024."
- Whisper API for voice queries on iPhone (browser mic → transcript → existing chat flow).
- Storage: local filesystem under `data/files/`, indexed in SQLite.

### Phase 3 — Message Triage + Scheduling (target: 2 weeks)

- Gmail/IMAP read (read-only) → daily digest of unread parent emails.
- Drafted replies in approval queue.
- Google Calendar read → "what's on my schedule this week?"
- Meeting reminder drafts.

### Phase 4 — Document Generation + Vendor/Expense (target: 2 weeks)

- Letter/certificate/report card generation from templates (Jinja2 + Claude for personalization).
- Vendor list, recurring bill tracking, expense forecasting.

### Phase 5+ — Iteration
Whatever's actually slowing Ivan down by then. We re-plan based on what worked.

---

## 4. Architecture

```
┌─────────────────────────┐
│  Browser (Mac + iPhone) │
│  Chat UI + Queue UI     │
└──────────┬──────────────┘
           │ HTTP (localhost:8765, Tailscale for iPhone)
┌──────────▼──────────────┐
│  FastAPI (main.py)      │
│  ├─ /chat   (Claude loop with tools)
│  ├─ /queue  (approval CRUD)
│  ├─ /qb/*   (OAuth + read/write)
│  ├─ /collegeone/parse
│  └─ /files  (Phase 2)
└──────────┬──────────────┘
           │
   ┌───────┼────────┬───────────────┐
   ▼       ▼        ▼               ▼
┌─────┐ ┌──────┐ ┌──────────┐  ┌──────────┐
│ DB  │ │Claude│ │QuickBooks│  │Filesystem│
│SQLite│ │ API │ │  API     │  │~/data/   │
└─────┘ └──────┘ └──────────┘  └──────────┘
```

**Why this stack:**
- **FastAPI**: same pattern as Ivan's trading-alerts tool. Familiar.
- **SQLite**: no server, file-based backup is trivial (just copy `.db`). 85 students × decades of data still fits in <1 GB.
- **Claude API w/ prompt caching**: cheapest tool-using LLM with good Spanish. Caching keeps system prompt cost near-zero across messages.
- **Vanilla HTML/JS UI**: no React build step, no node_modules. ~250 lines of HTML serves chat + queue.

**Deployment:** runs locally with `uvicorn`. Tailscale for iPhone access (already common pattern for Ivan's other tools).

---

## 5. Data Model

Already implemented in `backend/db.py`:

- **families** — 72 rows expected. Contact info + WhatsApp number.
- **students** — 85 rows expected. FK to family, grade, enrollment date.
- **tuition_charges** — one row per family per month. Tracks expected amount, due date, paid_at, link to QB invoice.
- **transactions** — mirror of QB transactions Ivan reviews/categorizes. `qb_txn_id` unique.
- **approval_queue** — the spine. agent, action_type, summary, payload (JSON), status, decided_at, executed_at, result.
- **chat_messages** — conversation history. Used for context + audit.

Tables to add in later phases:
- **files** (Phase 2): path, ocr_text, tags, family_id, student_id, doc_type, created_at.
- **calendar_events** (Phase 3): synced from Google Calendar.
- **vendors / recurring_bills** (Phase 4).

---

## 6. Integrations

### QuickBooks Online (read-write, OAuth 2.0)

- Library: `python-quickbooks` + `intuit-oauth`.
- OAuth flow already scaffolded at `/qb/connect` and `/qb/callback`. Tokens persisted to `data/qb_tokens.json` (gitignored).
- **Read** (used by agent freely): Account list, recent transactions, open invoices, customer list, P&L report.
- **Write** (only invoked from approval queue executor): CreateInvoice, UpdateTransaction (categorize), SendInvoice.
- Token refresh on 401, auto-retry once.
- Sandbox first, switch `QB_ENVIRONMENT=production` when ready.

**Risk:** Intuit may rate-limit or break OAuth. Mitigation: degrade gracefully — if QB call fails, queue the action with a "needs retry" status; Ivan retries manually.

### CollegeOne (no API)

- Pure paste-in workflow at `/collegeone/parse`.
- Claude parses any format Ivan pastes (roster, payments, etc.), normalizes to schema, drops in approval queue.
- Approved records get inserted into `families` / `students` / `tuition_charges`.
- **Risk:** Paste formats vary; Claude may misread. Mitigation: approval-queue preview shows the parsed JSON before insert — Ivan always sees what's about to be added.

### WhatsApp (drafts only)

- No Twilio, no Meta Business API.
- Agent generates message text in approval queue with the family's WhatsApp number.
- Approval UI has a "Copy" button next to each draft. Ivan opens WhatsApp himself and pastes.
- **Tradeoff:** Manual send step preserved as Ivan's choice. No outbound API costs, no Meta approval lead time. Acceptable for 72 families.

### Future integrations (out of scope now)
- Gmail/IMAP (Phase 3) — read-only via app password.
- Google Calendar (Phase 3) — OAuth, read + create.
- iPhone scan upload (Phase 2) — just a file upload endpoint, iPhone Camera → Files → Share to Safari URL.

---

## 7. Approval Queue Design

The single most important pattern in this system. Every external mutation flows through it.

**Lifecycle:**
1. Agent calls `propose_action` tool → row inserted with `status='pending'`.
2. UI shows pending items in real time (polls every 5s).
3. Ivan reviews payload (JSON shown in monospace), edits if needed, clicks Approve or Reject.
4. On Approve: status → `approved`, executor dispatches based on `action_type` → status → `executed` or `failed` with result captured.
5. On Reject: status → `rejected`, no execution. Decision and timestamp logged.

**Action types planned:**
- `qb.categorize_txn` — update transaction category in QB
- `qb.create_invoice` — new tuition invoice
- `qb.record_payment` — mark invoice paid
- `collegeone.import_families` / `_students` / `_payments`
- `record.update_family` / `_student`
- `message.whatsapp_draft` / `message.email_draft` (no execution — Ivan copies manually; "approve" just marks as sent)
- `file.move` / `file.tag` (Phase 2)

**Bulk operations:** "Approve all `qb.categorize_txn` in this session" — critical for monthly bookkeeping (could be 100+ transactions).

**Audit:** Every queue row is a permanent record. Tax-prep auditability comes free.

---

## 8. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Claude hallucinates a QB write | Medium | High (corrupt books) | Approval queue blocks all writes. Schema validation before execution. |
| QB OAuth token expires mid-month | High | Medium | Auto-refresh on 401, notify Ivan if refresh fails. |
| CollegeOne paste parsed wrong | Medium | Medium | Approval-queue preview; Ivan eyeballs before insert. |
| API cost spikes | Low | Low | Prompt caching, token-usage logging, monthly alert at $75. |
| Student data on local Mac (FERPA-ish) | Low | High | Local-only, no cloud sync. Mac FileVault on. Backups encrypted. |
| Ivan loses Mac / SSD fails | Medium | High | Daily SQLite backup to encrypted external drive or Time Machine. |
| Long-running scan: paper docs take forever | High | Medium | Phase 2 only — make scanning frictionless or it won't happen. |
| Feature creep / never-finishing | Medium | High | Strict phase gates. Don't start Phase N+1 until Phase N saves real time. |

---

## 9. Cost Estimate

| Item | Monthly cost | Notes |
|---|---|---|
| Anthropic API (Claude Sonnet 4.6) | $20-50 | With prompt caching. Heaviest cost = OCR (Phase 2). |
| QuickBooks API | $0 | Included in Ivan's existing QBO subscription. |
| WhatsApp | $0 | Manual copy/paste. |
| Hosting | $0 | Local Mac. |
| Domain / SSL | $0 | localhost + Tailscale. |
| **Total** | **$20-50** | Well under $100/mo budget. |

If we ever add Twilio WhatsApp (Phase 5+), add ~$5-15/mo.

---

## 10. Milestones

| Milestone | Target date | Definition of done |
|---|---|---|
| M0: Scaffold runs end-to-end | 2026-05-30 | `uvicorn main:app` boots, chat works, can chat with Claude, queue UI updates |
| M1: Families + students imported | 2026-06-06 | All 72 families + 85 students in SQLite, looked up by name in chat |
| M2: QB connected, read working | 2026-06-13 | "Show me transactions over $100 from last week" returns real QB data |
| M3: Bookkeeper categorization MVP | 2026-06-20 | One month of QB transactions categorized via approval queue end-to-end |
| M4: Tuition reconciliation | 2026-06-27 | "Who hasn't paid May tuition?" returns accurate list. WhatsApp draft generation works. |
| M5: Phase 1 complete | 2026-07-04 | Ivan uses the tool weekly without help, reports time saved |
| M6: Phase 2 OCR working | 2026-07-25 | Photo of a paper doc → searchable in <30s |

Slip is expected. Re-baseline after M3.

---

## 11. Resolved Questions (2026-05-27)

1. **CollegeOne data** — Two CSV exports available:
   - `families_report.csv`: denormalized family+student data. 75 unique families, 93 student rows. Mixed phone formats; Spanish grades (Kinder–Duodecimo); WhatsApp = same as Mobile Phone.
   - `items_balance_report.csv`: per-student billing items. Columns are months Jun–May; values show paid amount per item per month.
   - **Refresh:** automated via Playwright scrape at `https://suite.collegeone.net/signin`. Daily.
2. **QB cleanup** — Full cleanup approved. New school-specific accounts (Spanish names) mirroring CollegeOne items:
   - Income: `MENSUALIDAD`, `MATRICULA`, `Horario Extendido`, `Extendido Diario`, `Cuota de Construccion`, `Seguro Anual`, `Uniformes`, `Campamento Verano` (plus existing `Late Fee Income`)
   - Expense: `Cafetería`, `Transportación Escolar`, `Materiales Educativos`, `Compras Innecesarias` (converted from misclassified long-term liability)
   - Delete: `David a monroig` bank account, `Compras innecesarias` long-term liability
   - Archive: ~120 unused QBO default accounts
   - Cleanup happens as a single approval-queue batch.
3. **Tuition variability** — Multiple item types, mostly flat. Standard MENSUALIDAD $295/mo, with `J` variant ($304) for one specific student. Add-ons (Horario Extendido $100, etc.) per student. Bill items mirror CollegeOne items.
4. **Fiscal year** — Semesters: Aug-Dec and Jan-May. Summer months (Jun/Jul) only MATRICULA expected, no MENSUALIDAD. Reports roll up by semester.
5. **Backup** — Time Machine (already running) + nightly encrypted SQLite snapshot to iCloud Drive via launchd. Script to be written in Phase 1.
6. **iPhone access** — Tailscale already configured (from trading-alerts setup). Reuse same tailnet.

---

## 12. What I'll Do Next (after Ivan confirms plan)

1. Write a CollegeOne paste smoke test using whatever sample Ivan provides.
2. Wire up the first QB read call (transactions, last 30 days) end-to-end.
3. Build the bookkeeper agent + bulk-approve UI.
4. Demo end-of-month categorization session with Ivan.

---

*Push back on anything. Sections that are wrong or missing matter more than sections that read smoothly.*
