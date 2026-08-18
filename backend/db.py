"""SQLite schema + connection helper."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "../data/simply.db")).resolve()

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collegeone_family_id TEXT UNIQUE,
    name TEXT NOT NULL,
    primary_contact TEXT,
    phone TEXT,
    email TEXT,
    whatsapp TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collegeone_student_no TEXT UNIQUE,
    family_id INTEGER REFERENCES families(id),
    name TEXT NOT NULL,
    grade TEXT,
    student_group TEXT,
    teacher TEXT,
    gender TEXT,
    dob TEXT,
    enrollment_date TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tuition_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER REFERENCES students(id),
    collegeone_invoice_id TEXT,
    item_name TEXT NOT NULL,       -- "MENSUALIDAD 2025-2026", "Horario Extendido", etc.
    item_price REAL NOT NULL,
    period TEXT NOT NULL,          -- "YYYY-MM"
    amount_paid REAL DEFAULT 0,
    invoice_status TEXT,           -- Paid|Past Due|Partially Paid|cancelled
    paid_at TEXT,
    qb_invoice_id TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(student_id, item_name, period)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qb_txn_id TEXT UNIQUE,
    txn_date TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    vendor TEXT,
    category TEXT,
    source TEXT DEFAULT 'qb',      -- qb|manual
    status TEXT DEFAULT 'uncategorized',  -- uncategorized|proposed|approved
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approval_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,           -- bookkeeper|payments|...
    action_type TEXT NOT NULL,     -- qb.categorize|qb.create_invoice|message.draft|...
    summary TEXT NOT NULL,         -- human-readable
    payload_json TEXT NOT NULL,    -- structured action data
    status TEXT DEFAULT 'pending', -- pending|approved|rejected|executed|failed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    executed_at TEXT,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS payments_received (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collegeone_payment_id TEXT UNIQUE NOT NULL,
    payment_date TEXT NOT NULL,
    customer_name TEXT,
    student_id INTEGER REFERENCES students(id),
    family_id INTEGER REFERENCES families(id),
    payment_method TEXT,           -- Cash, Bank Account, Card, etc.
    invoice_id TEXT,
    transaction_id TEXT,
    status TEXT,                    -- Processed, In Process, Failed
    amount REAL NOT NULL,
    payout_id TEXT,
    payout_date TEXT,
    section TEXT,                   -- 'Regular Invoices' or other
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS debt_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    student_id INTEGER REFERENCES students(id),
    family_id INTEGER REFERENCES families(id),
    invoice_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    item_amount REAL NOT NULL,         -- gross billed for this item
    tax REAL DEFAULT 0,
    item_type TEXT,                    -- Family, Student
    invoice_date TEXT,
    due_date TEXT,
    overdue_days INTEGER,
    school_year TEXT,
    -- Invoice-level totals (denormalized — same value for all items of one invoice):
    invoice_subtotal REAL,
    invoice_payments_applied REAL,
    invoice_balance_due REAL,          -- THIS is what the family actually owes on this invoice
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_date ON payments_received(payment_date);
CREATE INDEX IF NOT EXISTS idx_payments_student ON payments_received(student_id);
CREATE INDEX IF NOT EXISTS idx_debt_snapshot_date ON debt_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_debt_student ON debt_snapshots(student_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,            -- user|assistant
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payee_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payee_norm TEXT UNIQUE NOT NULL,       -- normalized payee name (lowercase, trimmed)
    payee_display TEXT NOT NULL,           -- original casing for display
    qb_account_id TEXT,                    -- null when mode='ask' or 'skip'
    qb_account_name TEXT,
    mode TEXT NOT NULL DEFAULT 'always',   -- always | ask | skip
    times_applied INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON approval_queue(status);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tuition_period ON tuition_charges(period);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """Add columns to existing tables (CREATE TABLE IF NOT EXISTS won't)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(debt_snapshots)").fetchall()}
    for col, ddl in [
        ("invoice_subtotal", "REAL"),
        ("invoice_payments_applied", "REAL"),
        ("invoice_balance_due", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE debt_snapshots ADD COLUMN {col} {ddl}")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
