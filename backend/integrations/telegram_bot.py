"""Telegram bot — long-polls Telegram and routes messages through the chat engine.

Security: the first user to send /start owns the bot. Subsequent unknown users
are silently rejected. Owner ID persists to data/telegram_owner.json so a
restart doesn't reset ownership.

Conversation state: each user's message history is kept in-memory per chat_id.
This resets if the bot restarts — acceptable for a personal assistant.

Bot commands:
  /start    — claim ownership (one-time) or just say hi if already owned
  /pending  — list pending approval queue items with inline approve/reject buttons
  /reset    — clear conversation history (start fresh)
"""
import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

import approvals
import bookkeeper
import briefing
import chat as chat_engine
import executor
import receipt_ocr

# Defaults for receipt OCR — overridable in .env
DEFAULT_PAYMENT_ACCOUNT_ID = os.getenv("QB_DEFAULT_PAYMENT_ACCOUNT_ID", "19")  # Oriental
DEFAULT_EXPENSE_ACCOUNT_ID = os.getenv("QB_DEFAULT_EXPENSE_ACCOUNT_ID", "2")  # Uncategorized Expense
DEFAULT_EXPENSE_ACCOUNT_NAME = os.getenv("QB_DEFAULT_EXPENSE_ACCOUNT_NAME", "Uncategorized Expense")

BRIEF_TZ = ZoneInfo("America/Puerto_Rico")
BRIEF_HOUR = int(os.getenv("MORNING_BRIEF_HOUR", "7"))  # 7am local
BRIEF_MINUTE = int(os.getenv("MORNING_BRIEF_MINUTE", "0"))

log = logging.getLogger("telegram_bot")

OWNER_PATH = Path(os.getenv("TELEGRAM_OWNER_PATH",
                            Path(__file__).parent.parent.parent / "data" / "telegram_owner.json"))
TELEGRAM_MSG_LIMIT = 4000  # leave a little headroom under the 4096 byte cap

# In-memory conversation history keyed by chat_id
_history: dict[int, list[dict]] = {}


def _load_owner() -> int | None:
    if not OWNER_PATH.exists():
        return None
    try:
        return int(json.loads(OWNER_PATH.read_text())["owner_id"])
    except Exception:
        return None


def _save_owner(user_id: int) -> None:
    OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    OWNER_PATH.write_text(json.dumps({"owner_id": user_id}))


def _is_owner(user_id: int) -> bool:
    owner = _load_owner()
    return owner is not None and owner == user_id


def _reject_unknown(update: Update) -> bool:
    """Return True if message should be ignored (not from the owner)."""
    owner = _load_owner()
    if owner is None:
        return False  # no owner yet — let /start handler claim
    return update.effective_user.id != owner


async def _send_long(update: Update, text: str, **kwargs) -> None:
    """Telegram caps messages at ~4096 chars. Split politely."""
    if not text:
        text = "(sin respuesta)"
    for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
        await update.effective_message.reply_text(text[i:i + TELEGRAM_MSG_LIMIT], **kwargs)


# ---- Handlers ---------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    owner = _load_owner()
    if owner is None:
        _save_owner(user_id)
        await update.message.reply_text(
            f"¡Hola! Soy tu asistente. Te registré como dueño (id {user_id}).\n"
            "Pregúntame lo que sea sobre la escuela. Usa /pending para ver aprobaciones, "
            "/reset para empezar conversación nueva."
        )
        return
    if owner != user_id:
        log.warning("rejected unknown telegram user_id=%s", user_id)
        return  # silent rejection
    await update.message.reply_text(
        "Listo, dime qué necesitas. /pending para aprobaciones, /reset para limpiar."
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _reject_unknown(update):
        return
    _history.pop(update.effective_chat.id, None)
    await update.message.reply_text("Conversación reiniciada.")


async def cmd_brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the morning brief on demand."""
    if _reject_unknown(update):
        return
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    brief = await asyncio.to_thread(briefing.build_morning_brief)
    text = briefing.render_brief_text(brief)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _reject_unknown(update):
        return
    items = approvals.list_pending()
    if not items:
        await update.message.reply_text("No hay nada pendiente de aprobación.")
        return
    for item in items[:10]:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Aprobar", callback_data=f"approve:{item['id']}"),
            InlineKeyboardButton("✗ Rechazar", callback_data=f"reject:{item['id']}"),
        ]])
        await update.message.reply_text(
            f"#{item['id']} [{item['action_type']}]\n{item['summary']}",
            reply_markup=keyboard,
        )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _reject_unknown(update):
        return
    query = update.callback_query
    await query.answer()
    action, qid = query.data.split(":")
    qid = int(qid)
    if action == "approve":
        item = approvals.decide(qid, "approved")
        if not item:
            await query.edit_message_text(f"#{qid} ya no existe o no estaba pendiente.")
            return
        success, result = executor.execute(item["action_type"], item["payload"])
        approvals.mark_executed(qid, success, result)
        if success:
            mark = "✅ Ejecutado"
            # Email drafts come back with a Gmail URL — surface it for one-tap open
            if result.get("url"):
                mark += f"\n👉 {result['url']}"
        else:
            mark = f"⚠️ Falló: {result.get('error', '?')}"
        await query.edit_message_text(f"{query.message.text}\n\n{mark}")
    else:
        approvals.decide(qid, "rejected")
        await query.edit_message_text(f"{query.message.text}\n\n❌ Rechazado")


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat photos as receipts: OCR → propose qb.create_expense in queue."""
    if _reject_unknown(update):
        return
    photo = update.message.photo[-1]  # highest resolution
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = bytes(await file.download_as_bytearray())

    try:
        data = await asyncio.to_thread(receipt_ocr.extract_receipt, img_bytes)
    except Exception as e:
        log.exception("OCR failed")
        await update.message.reply_text(f"⚠️ Error en OCR: {e}")
        return

    if "error" in data:
        await update.message.reply_text(
            f"⚠️ No pude leer el recibo ({data.get('error')}). "
            f"Intenta otra foto más clara."
        )
        return

    vendor = data.get("vendor", "Desconocido")
    txn_date = data.get("txn_date") or dt.date.today().isoformat()
    total = float(data.get("total", 0))
    payment_type = data.get("payment_type", "Cash")
    memo = data.get("memo", "")
    confidence = data.get("confidence", 1.0)

    # Look up payee rule for this vendor → use its account if 'always'
    rule = bookkeeper.get_rule(vendor)
    if rule and rule["mode"] == "always" and rule["qb_account_id"]:
        expense_acct_id = rule["qb_account_id"]
        expense_acct_name = rule["qb_account_name"]
    else:
        expense_acct_id = DEFAULT_EXPENSE_ACCOUNT_ID
        expense_acct_name = DEFAULT_EXPENSE_ACCOUNT_NAME

    summary = (
        f"Crear gasto QB: ${total:,.2f} en {vendor} ({txn_date}). "
        f"Pago: {payment_type}. Cuenta: {expense_acct_name}. "
        f"Confianza OCR: {confidence:.0%}"
    )
    qid = approvals.enqueue(
        agent="bookkeeper",
        action_type="qb.create_expense",
        summary=summary,
        payload={
            "txn_date": txn_date,
            "amount": total,
            "payment_account_id": DEFAULT_PAYMENT_ACCOUNT_ID,
            "expense_account_id": expense_acct_id,
            "payment_type": payment_type,
            "payee_name": vendor,
            "memo": memo,
        },
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✓ Aprobar", callback_data=f"approve:{qid}"),
        InlineKeyboardButton("✗ Rechazar", callback_data=f"reject:{qid}"),
    ]])
    await update.message.reply_text(f"#{qid} {summary}", reply_markup=keyboard)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _reject_unknown(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    if not text.strip():
        return
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Run Claude in a thread so we don't block the asyncio loop
    history = _history.get(chat_id, [])
    try:
        turn = await asyncio.to_thread(chat_engine.run_turn, text, history)
    except Exception as e:
        log.exception("chat_engine.run_turn failed")
        await update.message.reply_text(f"⚠️ Error: {e}")
        return

    _history[chat_id] = turn.history
    await _send_long(update, turn.text)

    # For any new approval-queue items, follow up with inline buttons so Ivan
    # can tap to approve without having to call /pending.
    for qid in turn.queued_ids:
        item = approvals.get(qid)
        if not item:
            continue
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Aprobar", callback_data=f"approve:{qid}"),
            InlineKeyboardButton("✗ Rechazar", callback_data=f"reject:{qid}"),
        ]])
        await update.message.reply_text(
            f"#{qid} [{item['action_type']}]\n{item['summary']}",
            reply_markup=keyboard,
        )


# ---- App lifecycle ----------------------------------------------------------

_application: Application | None = None


def build_application() -> Application | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
        return None
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


async def _scheduled_morning_brief(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily 7am brief — only fires if owner is registered."""
    owner = _load_owner()
    if owner is None:
        log.info("morning brief skipped — no owner registered")
        return
    try:
        brief = await asyncio.to_thread(briefing.build_morning_brief)
        text = briefing.render_brief_text(brief)
        await ctx.bot.send_message(chat_id=owner, text=text, parse_mode=ParseMode.MARKDOWN)
        log.info("morning brief sent to owner=%s", owner)
    except Exception:
        log.exception("morning brief failed")


async def start_polling() -> None:
    global _application
    _application = build_application()
    if _application is None:
        return
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling(drop_pending_updates=True)
    # Daily morning brief — JobQueue is created by Application.builder()
    _application.job_queue.run_daily(
        _scheduled_morning_brief,
        time=dt.time(hour=BRIEF_HOUR, minute=BRIEF_MINUTE, tzinfo=BRIEF_TZ),
        name="morning_brief",
    )
    log.info("Telegram bot polling started; morning brief scheduled %02d:%02d %s",
             BRIEF_HOUR, BRIEF_MINUTE, BRIEF_TZ)


async def stop_polling() -> None:
    global _application
    if _application is None:
        return
    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
    _application = None
