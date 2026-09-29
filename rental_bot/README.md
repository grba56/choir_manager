# Rental listings Telegram bot

Send the bot a link to a rental post. Claude reads the post and extracts:
**location, price, rooms, entry date, commission** (plus short notes), and the bot
adds a row to a Google Sheet.

The bot then asks whether a viewing is already scheduled:
- send a date/time (any wording, e.g. "tomorrow 18:30", "ראשון ב-11") → saved to *Viewing date / Viewing time*
- tap **No viewing yet** or answer "no" → left blank

Later, **reply to the original link** (or to the bot's summary / confirmation)
with a date/time and the bot fills it into that listing's row. Replying again
reschedules it.

Sheet columns: `Added | Link | Location | Price | Rooms | Entry date | Commission | Viewing date | Viewing time | Notes | Telegram refs`.
The last column holds the Telegram message IDs used to match replies to rows —
leave it in place, but feel free to hide it. Sorting/deleting rows is fine.

## Setup

1. **Telegram bot** – talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
   To use it in a group, also run `/setprivacy` → *Disable* so it can see links.
2. **Anthropic API key** – from <https://platform.claude.com>.
3. **Google service account**
   - In Google Cloud Console create a project, enable the **Google Sheets API** and **Google Drive API**.
   - Create a service account → *Keys* → *Add key* → JSON. Save it as `service_account.json` here.
   - Create a spreadsheet and **share it (Editor) with the service account's email** (`...@...iam.gserviceaccount.com`).
   - Copy the spreadsheet ID from its URL: `docs.google.com/spreadsheets/d/<ID>/edit`.
4. Configure and run:

```bash
cd rental_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values
python bot.py
```

The `Rentals` tab (or `WORKSHEET_NAME`) is created with headers on first run.
Set `ALLOWED_CHAT_IDS` to restrict who can use the bot.

## Notes

- **Facebook posts** usually can't be read without logging in. Paste the post text
  in the same message as the link and Claude will use that instead.
- Yad2 / Madlan / Komo and most sites are fetched directly; the page text is sent to Claude.
- Model: `claude-opus-5-5` with server-side refusal fallback enabled
  (`fallbacks: "default"`), so a rare safety-classifier decline is retried on
  another model automatically.
- Pending "is there a viewing?" questions live in memory; after a restart,
  just reply to the listing message instead.
