"""SQLite schema + connection helper."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "../data/simply.db")).resolve()

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    primary_contact TEXT,
    phone TEXT,
    email TEXT,
    whatsapp TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER REFERENCES families(id),
    name TEXT NOT NULL,
    grade TEXT,
    enrollment_date TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tuition_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER REFERENCES families(id),
    student_id INTEGER REFERENCES students(id),
    period TEXT NOT NULL,          -- "YYYY-MM"
    amount REAL NOT NULL,
    due_date TEXT,
    paid_at TEXT,
    payment_method TEXT,
    qb_invoice_id TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,            -- user|assistant
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON approval_queue(status);
CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tuition_period ON tuition_charges(period);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


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
