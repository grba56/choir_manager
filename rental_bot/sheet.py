"""Google Sheets storage for listings.

Rows are located by the Telegram message IDs stored in the last column
(not by row number), so sorting or deleting rows in the sheet is safe.
"""

import re

import gspread
from gspread.utils import rowcol_to_a1

HEADERS = [
    "Added",
    "Link",
    "Location",
    "Price",
    "Rooms",
    "Entry date",
    "Commission",
    "Viewing date",
    "Viewing time",
    "Notes",
    "Telegram refs",
]
VIEWING_DATE_COL = HEADERS.index("Viewing date") + 1
REFS_COL = HEADERS.index("Telegram refs") + 1


def make_ref(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


class ListingSheet:
    def __init__(self, service_account_file: str, spreadsheet_id: str, worksheet_name: str):
        gc = gspread.service_account(filename=service_account_file)
        spreadsheet = gc.open_by_key(spreadsheet_id)
        try:
            self.ws = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            self.ws = spreadsheet.add_worksheet(worksheet_name, rows=1000, cols=len(HEADERS))
        if not any(self.ws.row_values(1)):
            self.ws.update([HEADERS], "A1")
            self.ws.freeze(rows=1)

    def append_listing(self, added: str, link: str, listing: dict, refs: list[str]) -> int:
        row = [
            added,
            link,
            listing["location"],
            listing["price"],
            listing["rooms"],
            listing["entry_date"],
            listing["commission"],
            "",
            "",
            listing["notes"],
            ",".join(refs),
        ]
        # RAW so scraped text starting with "=" or "+" is never run as a formula.
        result = self.ws.append_row(row, value_input_option="RAW", table_range="A1")
        updated = result["updates"]["updatedRange"]  # e.g. "Rentals!A7:K7"
        return int(re.search(r"![A-Z]+(\d+)", updated).group(1))

    def find_row(self, ref: str) -> int | None:
        for i, cell in enumerate(self.ws.col_values(REFS_COL), start=1):
            if i > 1 and ref in cell.split(","):
                return i
        return None

    def add_ref(self, row: int, ref: str) -> None:
        current = self.ws.cell(row, REFS_COL).value or ""
        refs = [r for r in current.split(",") if r]
        if ref not in refs:
            refs.append(ref)
            self.ws.update_cell(row, REFS_COL, ",".join(refs))

    def set_viewing(self, row: int, date: str, time: str) -> None:
        # USER_ENTERED so Sheets stores real date/time values (inputs are
        # validated YYYY-MM-DD / HH:MM strings produced by the bot).
        self.ws.update(
            [[date, time]],
            rowcol_to_a1(row, VIEWING_DATE_COL),
            value_input_option="USER_ENTERED",
        )

    def get_link(self, row: int) -> str:
        return self.ws.cell(row, HEADERS.index("Link") + 1).value or ""
