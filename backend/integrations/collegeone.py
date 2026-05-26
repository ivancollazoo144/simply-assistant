"""CollegeOne — no API. Ivan pastes data; we parse it with Claude.

Typical pastes: family rosters, student lists, payment histories. Format varies,
so we let Claude normalize into our schema instead of writing brittle regex.
"""
import json

from claude_client import MODEL, get_client


PARSE_PROMPT = """You are parsing data pasted from the CollegeOne school management system. Extract structured records.

Input is unstructured text. Identify whether it's:
- a roster of families (parent names + contacts)
- a roster of students (with family link if visible)
- a payment history
- something else

Return JSON only, matching this shape:
{
  "type": "families" | "students" | "payments" | "unknown",
  "records": [ {...} ],
  "notes": "anything Ivan should know about ambiguous fields"
}

For families: name, primary_contact, phone, email, whatsapp.
For students: name, family_name (to match), grade, enrollment_date.
For payments: family_name, student_name, period (YYYY-MM), amount, paid_at, method.

Use null for missing fields. Do not invent data."""


def parse_paste(text: str) -> dict:
    client = get_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=PARSE_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip()
    # Strip code fences if Claude added them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)
