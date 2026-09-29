"""Fetch a rental post and use Claude to pull out the structured fields."""

import json
import logging
from datetime import datetime

import httpx
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Scraped pages are mostly boilerplate; cap what we send so one huge page
# can't blow up cost. Listing details are near the top / in meta tags.
MAX_PAGE_CHARS = 60_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_client = AsyncAnthropic()

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
        "price": {"type": "string"},
        "rooms": {"type": "string"},
        "entry_date": {"type": "string"},
        "commission": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["location", "price", "rooms", "entry_date", "commission", "notes"],
    "additionalProperties": False,
}

LISTING_SYSTEM = """You extract apartment rental details from a listing (a web page and/or text a user pasted).
Posts may be in Hebrew, English or Russian; always answer in English, but keep street and neighborhood names recognizable.

Fields:
- location: street + neighborhood + city, as specific as the post allows.
- price: monthly rent with currency, e.g. "6,500 ₪". Mention extras if stated (e.g. "6,500 ₪ + 300 ₪ vaad").
- rooms: number of rooms as written in the post (Israeli count incl. living room), e.g. "3" or "2.5".
- entry_date: move-in date, e.g. "2026-11-01", "Immediate", "Flexible", or "November".
- commission: broker fee, e.g. "No commission", "1 month rent", "Yes (amount not stated)".
- notes: one short line with notable details (floor, elevator, parking, balcony, pets, furnished, contact name/phone).

Use an empty string for any field the post does not state. Never guess."""

DATETIME_SCHEMA = {
    "type": "object",
    "properties": {
        "has_datetime": {"type": "boolean"},
        "date": {"type": "string"},
        "time": {"type": "string"},
    },
    "required": ["has_datetime", "date", "time"],
    "additionalProperties": False,
}

DATETIME_SYSTEM = """You read a short chat message about an apartment viewing and extract when the viewing is.
Return date as YYYY-MM-DD and time as HH:MM (24h). Resolve relative expressions ("tomorrow", "Sunday", "מחר ב-6")
against the current date given below, choosing the next upcoming occurrence.
If the message says there is no viewing yet (e.g. "no", "not yet", "לא"), or gives no date/time, set has_datetime=false.
If only a date is given, leave time empty; if only a time is given, assume the nearest upcoming day and fill the date."""


class ExtractionError(Exception):
    pass


async def fetch_page_text(url: str) -> str:
    """Download a page and reduce it to readable text. Returns "" on failure."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "he,en;q=0.8"},
        ) as http:
            resp = await http.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Fetching %s failed: %s", url, e)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    parts = []

    if soup.title and soup.title.string:
        parts.append(f"Title: {soup.title.string.strip()}")
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or ""
        if key in ("og:title", "og:description", "description", "twitter:description"):
            if meta.get("content"):
                parts.append(f"{key}: {meta['content'].strip()}")

    # Many listing sites (e.g. Yad2) render client-side; the data sits in JSON blobs.
    for script in soup.find_all("script"):
        if script.get("type") == "application/ld+json" or script.get("id") == "__NEXT_DATA__":
            if script.string:
                parts.append(script.string.strip())
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    body = soup.get_text(" ", strip=True)
    if body:
        parts.append(body)

    return "\n".join(parts)[:MAX_PAGE_CHARS]


async def _structured_call(system: str, content: str, schema: dict, effort: str) -> dict:
    response = await _client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("Claude declined to process this message.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("Claude's response was cut off.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ExtractionError("Claude returned no answer.")
    return json.loads(text)


async def extract_listing(url: str, page_text: str, user_text: str) -> dict:
    content = f"Listing URL: {url}\n"
    if user_text:
        content += f"\nText the user sent with the link:\n{user_text}\n"
    if page_text:
        content += f"\nPage content:\n{page_text}\n"
    else:
        content += "\n(The page could not be downloaded; use only the URL and the user's text.)\n"
    return await _structured_call(LISTING_SYSTEM, content, LISTING_SCHEMA, effort="medium")


async def extract_viewing_time(message: str, now: datetime) -> dict:
    content = (
        f"Current date and time: {now:%A, %Y-%m-%d %H:%M} ({now.tzinfo})\n\n"
        f"Message:\n{message}"
    )
    return await _structured_call(DATETIME_SYSTEM, content, DATETIME_SCHEMA, effort="low")
