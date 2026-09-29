"""Telegram bot: send it rental links, it logs them to a Google Sheet.

Flow:
1. User sends a message with a link -> Claude extracts location, price, rooms,
   entry date and commission -> a row is appended to the sheet.
2. Bot asks whether a viewing is already scheduled. The next plain message
   (or a tap on "No viewing yet") answers it; no date means the cell stays blank.
3. Later, replying to the original link (or to the bot's summary) with a
   date/time fills in the viewing columns for that listing.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import extractor
from sheet import ListingSheet, make_ref

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rental_bot")

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Jerusalem"))
URL_RE = re.compile(r"https?://\S+")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
NO_VIEWING = "no_viewing"

HELP_TEXT = (
    "Send me a link to a rental post and I'll add it to the spreadsheet "
    "(location, price, rooms, entry date, commission).\n\n"
    "Tip: for Facebook posts, paste the post text together with the link — "
    "I can't log in to read them.\n\n"
    "To set a viewing time later, reply to the original link (or my summary) "
    "with something like \"Sunday 18:30\"."
)

sheet: ListingSheet  # set in main()


async def run_blocking(func, *args):
    """gspread is synchronous; keep it off the event loop."""
    return await asyncio.to_thread(func, *args)


def listing_summary(listing: dict) -> str:
    lines = [
        ("📍", listing["location"]),
        ("💰", listing["price"]),
        ("🚪", f"{listing['rooms']} rooms" if listing["rooms"] else ""),
        ("📅", f"Entry: {listing['entry_date']}" if listing["entry_date"] else ""),
        ("🤝", f"Commission: {listing['commission']}" if listing["commission"] else ""),
        ("📝", listing["notes"]),
    ]
    return "\n".join(f"{icon} {text}" for icon, text in lines if text) or "(no details found)"


def format_viewing(date: str, time: str) -> str:
    try:
        pretty = datetime.strptime(date, "%Y-%m-%d").strftime("%a %d %b %Y")
    except ValueError:
        pretty = date
    return f"{pretty} {time}".strip()


async def save_viewing(message: Message, row: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse a date/time out of `text` and store it on `row`."""
    try:
        parsed = await extractor.extract_viewing_time(text, datetime.now(TZ))
    except Exception:
        log.exception("Date parsing failed")
        await message.reply_text("⚠️ Couldn't process that, please try again.")
        return

    date, time = parsed["date"], parsed["time"]
    if not parsed["has_datetime"] or not DATE_RE.match(date) or (time and not TIME_RE.match(time)):
        await message.reply_text("OK, no viewing time recorded — leaving it blank.")
        return

    await run_blocking(sheet.set_viewing, row, date, time)
    reply = await message.reply_text(f"✅ Viewing recorded: {format_viewing(date, time)}")
    # Let people reply to this confirmation too, to reschedule.
    await run_blocking(sheet.add_ref, row, make_ref(reply.chat_id, reply.message_id))
    context.chat_data.pop("pending_row_ref", None)


async def add_listing(message: Message, url: str, user_text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await message.reply_text("🔎 Reading the listing…")
    try:
        page_text = await extractor.fetch_page_text(url)
        listing = await extractor.extract_listing(url, page_text, user_text)
        link_ref = make_ref(message.chat_id, message.message_id)
        refs = [link_ref, make_ref(status.chat_id, status.message_id)]
        added = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        await run_blocking(sheet.append_listing, added, url, listing, refs)
    except Exception:
        log.exception("Failed to add listing %s", url)
        await status.edit_text("⚠️ Sorry, I couldn't process that link. Please try again.")
        return

    context.chat_data["pending_row_ref"] = link_ref
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("No viewing yet", callback_data=NO_VIEWING)]])
    await status.edit_text(
        f"Added to the sheet ✅\n\n{listing_summary(listing)}\n\n"
        "Is there a viewing time already? Send the date and time, or tap below.",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = message.text or message.caption or ""

    # 1. A reply to an earlier listing message -> set its viewing time.
    if message.reply_to_message:
        replied = message.reply_to_message
        row = await run_blocking(sheet.find_row, make_ref(replied.chat_id, replied.message_id))
        if row:
            await save_viewing(message, row, text, context)
            return

    # 2. New link(s) -> add listings.
    urls = URL_RE.findall(text)
    if urls:
        extra = URL_RE.sub("", text).strip()
        for url in urls:
            await add_listing(message, url.rstrip(").,"), extra, context)
        return

    # 3. Answer to "is there a viewing time already?"
    pending = context.chat_data.get("pending_row_ref")
    if pending:
        row = await run_blocking(sheet.find_row, pending)
        context.chat_data.pop("pending_row_ref", None)
        if row:
            await save_viewing(message, row, text, context)
            return

    await message.reply_text(HELP_TEXT)


async def on_no_viewing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.chat_data.pop("pending_row_ref", None)
    await query.edit_message_text(
        f"{query.message.text}\n\n— No viewing yet. Reply to this message with a date/time when you have one.",
        disable_web_page_preview=True,
    )


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


def main() -> None:
    global sheet
    sheet = ListingSheet(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        os.environ["SPREADSHEET_ID"],
        os.environ.get("WORKSHEET_NAME", "Rentals"),
    )

    allowed = [int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
    chat_filter = filters.Chat(chat_id=allowed) if allowed else filters.ALL

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler(["start", "help"], on_help, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(on_no_viewing, pattern=f"^{NO_VIEWING}$"))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND & chat_filter, on_message))

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
