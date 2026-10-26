"""A spreadsheet's cells: normalising what a corpus stated, and serialising it the way Drive's
export does.

One module because two callers must agree: the importer derives ``gdrive_files.content`` from the
first sheet, and the export route serves TSV from the grid.

HERE rather than inside ``routers.google``, even though only Google reads a spreadsheet, because
the importer is the other caller and the two layers do not import each other -- an importer that
reached into a router would pull the whole web layer into ``backlot import``. A vendor-specific
pure helper that more than one layer needs lives at this level: ``sigv4`` is the S3 router's, and
``synth`` is every source's.
"""

from backlot import sheets_format

# A stored cell. A bare scalar unless a served write made it something a scalar cannot say
# (`sheets_input.apply`): then an object with `v` (the value, absent when empty), `e` (the error
# type) and `nf` (the `numberFormat`). A corpus states scalars alone, so a grid that was never
# written holds nothing else, and a corpus `""` or `None` is a cell nobody wrote.
#
# Two objects are empty and still there, measured 2026-10-26: `{"v": ""}` is a cell written an
# empty string (a RAW `""`, a lone apostrophe), served with a string's format and no value, and
# `{"nf": {}}` a cell whose value a USER_ENTERED `""` took away, served with a format and no
# alignment. A never-written cell is neither: it is served as nothing at all.
Cell = str | int | float | bool | None | dict

# Each error type by the code it displays as.
ERROR_CODES = {
    "N_A": "#N/A",
    "DIVIDE_BY_ZERO": "#DIV/0!",
    "REF": "#REF!",
    "VALUE": "#VALUE!",
    "NAME": "#NAME?",
    "NUM": "#NUM!",
    "NULL_VALUE": "#NULL!",
    "ERROR": "#ERROR!",
}


EMPTY_STRING = {"v": ""}
CLEARED = {"nf": {}}


def cell(value, *, error: str | None = None, number_format: dict | None = None) -> Cell:
    """A cell as stored, from its parts."""
    if error is None and not number_format:
        return value
    out: dict = {}
    if value is not None:
        out["v"] = value
    if error is not None:
        out["e"] = error
    if number_format is not None:
        out["nf"] = number_format
    return out


def cell_value(c: Cell):
    """A cell's value: a string (`""` for an empty string), a number, a boolean, or None for a
    cleared, never-written or error cell."""
    return c.get("v") if isinstance(c, dict) else c


def cell_error(c: Cell) -> str | None:
    return c.get("e") if isinstance(c, dict) else None


def cell_format(c: Cell) -> dict | None:
    """A cell's `numberFormat`, or None for one that has none."""
    return (c.get("nf") or None) if isinstance(c, dict) else None


def written(c: Cell) -> bool:
    """Whether a cell is there to serve: it holds something, or a write left it empty."""
    return isinstance(c, dict) or (c is not None and c != "")


def _scalar(v):
    """One cell as it is stored. An integral float collapses to an int: measured, writing 3.0 to a
    real sheet stores ``numberValue: 3`` and formats as ``"3"``, so keeping the float would serve a
    value the real API cannot hold.

    Narrow to ``float`` on purpose. Widening this to ``(int, float)`` would swallow ``bool``, which
    is a subclass of ``int`` — the hazard ``routers.google._sheets_value`` guards against."""
    if not isinstance(v, float):
        return v
    return int(v) if v.is_integer() else v


collapse = _scalar


def normalise_grid(grid: list[list]) -> list[list[Cell]]:
    """A rectangular grid: short rows padded with empty cells to the width of the widest row.

    Everything downstream may assume rectangularity, which is what lets the serving layer index a
    row by column without a bounds check on every cell."""
    width = max((len(r) for r in grid), default=0)
    return [[_scalar(r[i]) if i < len(r) else None for i in range(width)] for r in grid]


def formatted(c: Cell) -> str:
    """A cell's ``formattedValue`` -- its display string, which is also what export serialises.

    Measured: a boolean displays as TRUE/FALSE whatever its number format, an error as its code,
    an empty cell as the empty string, and a number under its number format or, with none, in
    General (:func:`sheets_format.general`)."""
    if (error := cell_error(c)) is not None:
        return ERROR_CODES[error]
    value = cell_value(c)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return value
    nf = cell_format(c)
    return sheets_format.render(value, nf) if nf else sheets_format.general(value)


def used_extent(grid: list[list[Cell]]) -> tuple[int, int]:
    """``(rows, cols)`` counting only as far as the last row and column holding anything.

    Measured: export pads its rows out to the sheet's last USED column, so an all-empty trailing
    column contributes no field at all."""
    rows = cols = 0
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if formatted(cell) != "":
                rows = r + 1
                cols = max(cols, c + 1)
    return rows, cols


def _rows_used(grid: list[list[Cell]]) -> list[list[str]]:
    rows, cols = used_extent(grid)
    return [[formatted(grid[r][c]) for c in range(cols)] for r in range(rows)]


def _csv_field(v: str) -> str:
    if any(ch in v for ch in (",", '"', "\n")):
        return '"' + v.replace('"', '""') + '"'
    return v


def to_csv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/csv`` serialises it.

    Measured: a field is quoted for a comma, a double quote or a newline but NOT for a tab; an
    embedded newline stays a bare ``\\n`` inside the quotes while rows are separated by
    ``\\r\\n``; leading and trailing spaces are preserved unquoted; and there is no trailing
    newline. Hand-rolled because :mod:`csv` cannot express that dialect.

    Rows are rectangular to the last used column, unlike ``values.get``, which trims each row
    independently and so answers ragged."""
    return "\r\n".join(",".join(_csv_field(v) for v in row) for row in _rows_used(grid))


def to_tsv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/tab-separated-values`` serialises it.

    Measured: TSV has NO quoting mechanism. An embedded newline and an embedded tab each collapse
    to a single space, and a double quote passes through bare -- a lossy conversion, reproduced
    rather than escaped around."""
    return "\r\n".join(
        "\t".join(v.replace("\n", " ").replace("\t", " ") for v in row) for row in _rows_used(grid)
    )
