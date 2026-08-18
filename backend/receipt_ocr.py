"""Receipt OCR via Claude Vision.

Given an image (bytes + mime type), returns structured fields:
  - vendor, txn_date, total, payment_type (Cash/Card), memo
Falls back gracefully on unreadable receipts.
"""
import base64
import json

from claude_client import MODEL, get_client


def extract_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Send image to Claude and parse the JSON it returns."""
    client = get_client()
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = (
        "Extract receipt data from this image. Return ONLY a JSON object with these keys:\n"
        '  - vendor (string): the store/merchant name as it appears on the receipt\n'
        '  - txn_date (string): YYYY-MM-DD; if year not on receipt, assume current year\n'
        '  - total (number): final amount paid including tax/tip\n'
        '  - payment_type (string): "Cash" | "CreditCard" | "Check" — infer from receipt '
        '(e.g., last 4 of card, "CASH", "VISA")\n'
        '  - currency (string): USD by default unless receipt clearly shows another\n'
        '  - memo (string): brief summary in Spanish — e.g. "Compras en Walgreens — varios"\n'
        '  - confidence (number 0-1): how confident you are. <0.5 means the receipt was unreadable\n'
        "If the image is not a receipt or is unreadable, return "
        '{"error": "not_a_receipt"} or {"error": "unreadable"}.'
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": "parse_failed", "raw": text[:500], "exception": str(e)}
    return data
