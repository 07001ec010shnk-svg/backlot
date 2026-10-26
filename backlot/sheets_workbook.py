"""A spreadsheet as a write sees it: its sheets, each with the properties a write may state, and
the rules for adding one.

Every Sheets write loads a workbook, changes it here in memory, and saves it whole
(`routers.google._sheets_save`), so an operation is a method on :class:`Workbook` rather than a
query. `spreadsheets.create` and `sheets.copyTo` add sheets through :meth:`Workbook.add_sheet`, as
the requests `spreadsheets.batchUpdate` carries would.

Measured against the live Sheets v4 API on 2026-10-26 unless a docstring says otherwise.
"""

from __future__ import annotations

import copy
from typing import NamedTuple

from backlot import synth

# A sheet's grid when nothing states another: measured, a new sheet and one added with only a
# title are both 1000 x 26.
GRID_ROWS = 1000
GRID_COLS = 26

# A new workbook's settings. `locale` and `timeZone` are what Backlot serves for every
# spreadsheet a corpus states; a workbook created through the API may state its own.
DEFAULT_PROPERTIES = {"locale": "en_US", "autoRecalc": "ON_CHANGE", "timeZone": "Etc/GMT"}

# What a sheet is called when nothing names it, and what a copy is called, by the locale of the
# spreadsheet it lands in. Measured on `en_US` and `ko_KR` workbooks; any other locale takes the
# English words, which no measurement has contradicted or confirmed.
_SHEET_WORD = {"ko_KR": "시트"}
_COPY_OF = {"ko_KR": "{title}의 사본"}

# The `SheetProperties` and `GridProperties` fields a sheet keeps, in proto field order, which is
# the order real serves them in: `gridProperties`, then `hidden`, `tabColor`, `rightToLeft`,
# `tabColorStyle`.
SHEET_FIELDS = ("gridProperties", "hidden", "tabColor", "rightToLeft", "tabColorStyle")
GRID_FIELDS = (
    "rowCount",
    "columnCount",
    "frozenRowCount",
    "frozenColumnCount",
    "hideGridlines",
    "rowGroupControlAfter",
    "columnGroupControlAfter",
)


class Sheet(NamedTuple):
    """One sheet. ``properties`` is what a write stated past the id, title and index -- empty for
    a sheet a corpus stated, whose grid then decides its size."""

    sheet_id: int
    index: int
    title: str
    grid: list[list]
    properties: dict = {}

    @property
    def rows(self) -> int:
        """The grid's height, never smaller than the data: as stated, else at least the default."""
        stated = self.properties.get("gridProperties", {}).get("rowCount")
        return max(stated or GRID_ROWS, len(self.grid))

    @property
    def cols(self) -> int:
        stated = self.properties.get("gridProperties", {}).get("columnCount")
        return max(stated or GRID_COLS, max((len(r) for r in self.grid), default=0))

    def served(self) -> dict:
        """The `SheetProperties` `spreadsheets.get` serves, in real's key order."""
        grid = self.properties.get("gridProperties", {})
        out: dict = {
            "sheetId": self.sheet_id,
            "title": self.title,
            "index": self.index,
            "sheetType": "GRID",
            "gridProperties": {
                "rowCount": self.rows,
                "columnCount": self.cols,
                **{k: grid[k] for k in GRID_FIELDS[2:] if grid.get(k)},
            },
        }
        out.update({k: self.properties[k] for k in SHEET_FIELDS[1:] if self.properties.get(k)})
        return out

    def stated(self) -> dict:
        """``properties`` with the grid's size written in, which is what a saved sheet keeps."""
        props = copy.deepcopy(self.properties)
        grid = props.setdefault("gridProperties", {})
        grid["rowCount"], grid["columnCount"] = self.rows, self.cols
        return props


class Workbook:
    """A spreadsheet's sheets in index order and its settings (:data:`DEFAULT_PROPERTIES`)."""

    def __init__(self, file_id: str, sheets: list[Sheet], properties: dict | None = None):
        self.file_id = file_id
        self.sheets = list(sheets)
        self.properties = {**DEFAULT_PROPERTIES, **(properties or {})}

    @property
    def locale(self) -> str:
        return self.properties["locale"]

    def sheet_by_id(self, sheet_id: int) -> Sheet | None:
        return next((s for s in self.sheets if s.sheet_id == sheet_id), None)

    def title_taken(self, title: str) -> bool:
        """Whether a sheet already has this title. Measured: titles collide without regard to
        case, `Same` against `same`."""
        fold = title.casefold()
        return any(s.title.casefold() == fold for s in self.sheets)

    def default_title(self) -> str:
        """The first `Sheet<n>` (in the workbook's language) no sheet has. Measured: the first
        sheet of a new workbook is `Sheet1` under `en_US` and `시트1` under `ko_KR`."""
        word = _SHEET_WORD.get(self.locale, "Sheet")
        n = 1
        while self.title_taken(f"{word}{n}"):
            n += 1
        return f"{word}{n}"

    def copy_title(self, title: str) -> str:
        """What a copy of a sheet titled ``title`` is called here. Measured: `Copy of Other` in an
        `en_US` workbook, then `Copy of Other 1`, `Copy of Other 2`; `Other의 사본` and `Other의 사본
        1` in a `ko_KR` one -- the destination's language, not the source's."""
        base = _COPY_OF.get(self.locale, "Copy of {title}").format(title=title)
        if not self.title_taken(base):
            return base
        n = 1
        while self.title_taken(f"{base} {n}"):
            n += 1
        return f"{base} {n}"

    def new_sheet_id(self, title: str) -> int:
        """A pseudo-random id no sheet here has, as real gives every sheet but a new workbook's
        only one (:func:`synth.sheet_id`)."""
        taken = {s.sheet_id for s in self.sheets}
        salt = ""
        while (sid := synth.sheet_id(self.file_id, title, salt)) in taken:
            salt += "+"
        return sid

    def add_sheet(self, sheet: Sheet, index: int | None = None) -> Sheet:
        """Put ``sheet`` at ``index`` (last when None), renumbering the sheets after it, and return
        it as placed. Its id and title are the caller's to have checked."""
        at = len(self.sheets) if index is None else index
        placed = sheet._replace(index=at)
        self.sheets.insert(at, placed)
        self.sheets = [s._replace(index=i) for i, s in enumerate(self.sheets)]
        return self.sheets[at]
