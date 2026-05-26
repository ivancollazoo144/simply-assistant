"""Claude API wrapper with prompt caching."""
import os

from anthropic import Anthropic

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = """You are the administrative assistant for a small bilingual (Spanish/English) school in Puerto Rico with 85 students and 72 families. Your principal user is Ivan, the school's owner/administrator.

Your job: help him manage bookkeeping (QuickBooks), tuition payments, family/student records, and routine administrative work. Respond in whichever language he uses.

**Critical rule:** any action that touches an external system — creating/editing a QuickBooks transaction, drafting a message for a family, modifying records — MUST go through the approval queue using the `propose_action` tool. Never claim you did something external; only claim you proposed it. Ivan reviews and approves before anything executes.

For lookup tools (read-only), call them directly and report results.

Be concise. Lead with the answer or the proposed action, not preamble. Use plain text — no markdown headers in chat replies."""


def get_client() -> Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    return Anthropic(api_key=api_key)


def cached_system_block() -> list[dict]:
    """System prompt with cache_control — caches across requests for ~5min TTL."""
    return [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
