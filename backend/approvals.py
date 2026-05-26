"""Approval queue — the spine. No external action runs without approval."""
import json
from datetime import datetime
from typing import Any

from db import connect


def enqueue(agent: str, action_type: str, summary: str, payload: dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO approval_queue (agent, action_type, summary, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (agent, action_type, summary, json.dumps(payload)),
        )
        return cur.lastrowid


def list_pending() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM approval_queue WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_all(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM approval_queue ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get(queue_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM approval_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def decide(queue_id: int, status: str, edited_payload: dict | None = None) -> dict | None:
    assert status in ("approved", "rejected")
    with connect() as conn:
        if edited_payload is not None:
            conn.execute(
                "UPDATE approval_queue SET status = ?, decided_at = ?, payload_json = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, datetime.utcnow().isoformat(), json.dumps(edited_payload), queue_id),
            )
        else:
            conn.execute(
                "UPDATE approval_queue SET status = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, datetime.utcnow().isoformat(), queue_id),
            )
    return get(queue_id)


def mark_executed(queue_id: int, success: bool, result: dict) -> None:
    status = "executed" if success else "failed"
    with connect() as conn:
        conn.execute(
            "UPDATE approval_queue SET status = ?, executed_at = ?, result_json = ? "
            "WHERE id = ?",
            (status, datetime.utcnow().isoformat(), json.dumps(result), queue_id),
        )


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("payload_json"):
        d["payload"] = json.loads(d.pop("payload_json"))
    if d.get("result_json"):
        d["result"] = json.loads(d.pop("result_json"))
    return d
