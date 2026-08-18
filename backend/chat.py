"""Chat turn logic — shared by the HTTP /chat endpoint and the Telegram bot."""
import json
from dataclasses import dataclass, field

from agents.tools import TOOLS, dispatch
from claude_client import MODEL, cached_system_block, get_client
from db import connect

MAX_TOOL_ITERATIONS = 6


@dataclass
class TurnResult:
    text: str
    history: list[dict]
    queued_ids: list[int] = field(default_factory=list)  # new approval-queue IDs from this turn


def run_turn(message: str, history: list[dict] | None = None) -> TurnResult:
    """One full user→assistant turn including the tool-use loop."""
    client = get_client()
    messages = list(history or []) + [{"role": "user", "content": message}]
    queued_ids: list[int] = []

    final_text = ""
    for _ in range(MAX_TOOL_ITERATIONS):
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
            if isinstance(result, dict) and result.get("queued") and result.get("queue_id"):
                queued_ids.append(result["queue_id"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_results})

    with connect() as conn:
        conn.execute("INSERT INTO chat_messages (role, content) VALUES (?, ?)",
                     ("user", message))
        conn.execute("INSERT INTO chat_messages (role, content) VALUES (?, ?)",
                     ("assistant", final_text))

    return TurnResult(text=final_text, history=messages, queued_ids=queued_ids)
