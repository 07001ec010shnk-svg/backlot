"""What a value written through the Sheets ``values`` API stores: ``valueInputOption=RAW`` keeps it
as sent, and ``USER_ENTERED`` reads a string the way the Sheets UI reads typing.

Every rule here was measured against the live Sheets v4 API on 2026-10-26, on an ``en_US``
spreadsheet, by writing each input into a cell and reading back its ``userEnteredValue``,
``formattedValue`` and ``effectiveFormat.numberFormat``. Inputs outside what was measured fall
through to a string, which is what real does for anything it cannot read.

A formula is stored as the text typed: evaluating one needs a formula engine, and storing its
text keeps a later read honest about what was written.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from backlot import sheets_format, sheets_grid

# Error literals a USER_ENTERED string is read as, by their displayed code, matched without
# regard to case and after trimming: `#n/a` and `#N/A ` are both N_A, `#N/A!` is text.
ERRORS = {code: kind for kind, code in sheets_grid.ERROR_CODES.items()}

# What trimming removes before a number, a date or an error is read. A boolean is NOT trimmed:
# `TRUE ` and ` FALSE` stay text.
_SPACE = " \t\n\r"

_CURRENCIES = "$€"

# The digits of a number: an integer part that may group by commas (each group after a comma at
# least three digits, so `1,2345` reads and `12,34` does not, and `,123` reads with nothing before
# the comma), an optional fraction, an optional exponent. ASCII digits only: `٣` and `１２` are text.
_DIGITS = re.compile(
    r"(?P<int>\d*(?:,\d{3,})+|\d+)?(?P<frac>\.\d*)?(?P<exp>[eE][+-]?\d+)?\Z", re.ASCII
)


def _nf(type_: str, pattern: str) -> dict:
    return {"type": type_, "pattern": pattern}


def _truncate15(text: str) -> Decimal:
    """A decimal kept to 15 significant digits by truncation: `0.1234567890123456789` stores
    `0.123456789012345` and `1234567890123456.5` stores `1234567890123450`."""
    d = Decimal(text)
    if d == 0:
        return Decimal(0)
    exp = d.adjusted()
    q = Decimal(1).scaleb(exp - 14)
    return d.quantize(q, rounding="ROUND_DOWN") if exp - 14 < 0 else (d // q) * q


def _number(text: str):
    """``(value, number_format)`` for a numeric spelling, or None."""
    s, neg, cur, cur_after, pct = text, False, None, False, False
    if s[:1] == "(" and s[-1:] == ")":
        s, neg = s[1:-1].strip(_SPACE), True
    if s[:1] == "-":
        s, neg = s[1:].lstrip(" "), not neg
    if s and s[0] in _CURRENCIES:
        cur, s = s[0], s[1:].lstrip(" ")
        if s[:1] == "-" and not neg:
            s, neg = s[1:], True
        if s[:1] == "(" and s[-1:] == ")":
            s, neg = s[1:-1], not neg
    if s[-1:] == "%":
        s, pct = s[:-1], True
    if cur is None and s and s[-1] in _CURRENCIES:
        cur, cur_after, s = s[-1], True, s[:-1].rstrip(" ")
    m = _DIGITS.match(s)
    if not s or m is None or not re.search(r"[0-9]", (m["int"] or "") + (m["frac"] or "")):
        return None
    whole, frac, exp = m["int"] or "", m["frac"] or "", m["exp"]
    if exp and cur:
        return None
    digits = whole.replace(",", "")
    if not frac and not exp and len(digits.lstrip("0")) > 15:
        return None  # a long integer stays text, so an id is not rounded
    try:
        d = _truncate15((digits or "0") + (frac if frac != "." else "") + (exp or ""))
    except InvalidOperation:
        return None
    if exp and not Decimal("-1e308") < d < Decimal("1e308"):
        return None
    if pct:
        d /= 100
    if neg:
        d = -d
    value = float(d)
    value = int(value) if value.is_integer() and abs(value) < 2**53 else value
    places = ".00" if frac else ""
    if exp:
        fmt = _nf("SCIENTIFIC", "0.00E+00")
    elif cur:
        body = f"#,##0{places}"
        fmt = _nf("CURRENCY", f'{body}"{cur}"' if cur_after else f'"{cur}"{body}')
    elif pct:
        fmt = _nf("PERCENT", f"0{places}%")
    elif "," in whole:
        fmt = _nf("NUMBER", f"#,##0{places}")
    else:
        fmt = None
    return value, fmt


def _month(word: str):
    """``(month, token)`` for a month name: its three-letter abbreviation or its full name, any
    case. `Sept` is neither."""
    w = word.casefold()
    for i, name in enumerate(sheets_format.MONTHS):
        if w == name.casefold():
            return i + 1, "mmmm"
        if w == name[:3].casefold():
            return i + 1, "mmm"
    return None


def _year(text: str, today: date) -> tuple[int, str]:
    """A typed year and its token: two digits are `yy`, anything else `yyyy`. A year under 100,
    however many digits spell it, pivots at 30 — `29` is 2029, `30` is 1930, and `0024` 2024 —
    while `124` is the year 124."""
    y = int(text)
    if y < 100:
        y += 2000 if y < 30 else 1900
    return y, ("yy" if len(text) == 2 else "yyyy")


def _serial(y: int, m: int, d: int):
    try:
        return date(y, m, d).toordinal() - sheets_format._EPOCH_DAYS
    except ValueError:
        return None


_NUMERIC_DATE = re.compile(r"(\d+)([/-])(\d+)(?:\2(\d+))?\Z", re.ASCII)


def _numeric_date(s: str, today: date):
    """`2024-01-15`, `2024/1`, `1/15/2024`, `1/15/24`, `1/15`, `1/2024`, `01-15-2024`: ``(serial,
    pattern)`` or None. Month and day are zero-padded in the pattern when EITHER was typed with a
    leading zero (`1/05/2024` is `mm/dd/yyyy`). A dot is not a date separator."""
    m = _NUMERIC_DATE.match(s)
    if m is None:
        return None
    a, sep, b, c = m[1], m[2], m[3], m[4]
    if len(a) == 4:  # year first: y-m-d or y-m
        y, ytok = int(a), "yyyy"
        mo, dd = b, c
        if dd is None:
            serial = _serial(y, int(mo), 1)
            mtok = "mm" if mo.startswith("0") else "m"
            return (serial, f"yyyy{sep}{mtok}") if serial is not None else None
        pad = mo.startswith("0") or dd.startswith("0")
        serial = _serial(y, int(mo), int(dd))
        return (
            (serial, f"yyyy{sep}{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}")
            if serial is not None
            else None
        )
    if c is None and len(b) == 4:  # m/yyyy
        serial = _serial(int(b), int(a), 1)
        return (
            (serial, f"{'mm' if a.startswith('0') else 'm'}{sep}yyyy")
            if serial is not None
            else None
        )
    if c is None:  # m/d, this year
        serial = _serial(today.year, int(a), int(b))
        pad = a.startswith("0") or b.startswith("0")
        return (
            (serial, f"{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}")
            if serial is not None
            else None
        )
    y, ytok = _year(c, today)
    pad = a.startswith("0") or b.startswith("0")
    serial = _serial(y, int(a), int(b))
    if serial is None:
        return None
    return serial, f"{'mm' if pad else 'm'}{sep}{'dd' if pad else 'd'}{sep}{ytok}"


_WEEKDAYS = {w.casefold() for w in sheets_format.WEEKDAYS}
_WORDY = re.compile(r"[A-Za-z]+\.?|\d+|[ ,\-]+", re.ASCII)


def _wordy_date(s: str, today: date):
    """A date with a month name — `Jan 15, 2024`, `15 Jan 2024`, `January 2024`, `Jan15`,
    `2024 Jan 15`, `Mon Jan 15 2024` — as ``(serial, pattern)`` or None.

    The pattern is the input with each part replaced by its token and every separator kept as
    typed: `Jan. 15, 2024` is `mmm. d, yyyy` and `Jan 15,2024` is `mmm d,yyyy`."""
    parts = _WORDY.findall(s)
    if "".join(parts) != s:
        return None
    month = day = year = None
    pattern = []
    for p in parts:
        if p[0].isalpha():
            word, dot = (p[:-1], ".") if p.endswith(".") else (p, "")
            got = _month(word)
            if got is not None and month is None:
                month = got[0]
                pattern.append(got[1] + dot)
            elif not pattern and not dot and word.casefold() in _WEEKDAYS:
                pattern.append("ddd")
            else:
                return None
        elif p[0].isdigit():
            if len(p) >= 3 and year is None:
                year = _year(p, today)
                pattern.append(year[1])
            elif day is None and int(p) <= 31 and (month is not None or year is None):
                day = p
                pattern.append("dd" if p.startswith("0") else "d")
            elif year is None:
                year = _year(p, today)
                pattern.append(year[1])
            else:
                return None
        else:
            pattern.append(p)
    if month is None or (day is None and year is None):
        return None
    serial = _serial(year[0] if year else today.year, month, int(day) if day else 1)
    if serial is None:
        return None
    return serial, "".join(pattern)


_TIME = re.compile(
    r"(?P<sign>-)?(?P<h>\d+)(?::(?P<m>\d{2})(?::(?P<s>\d{2})(?P<f>\.\d+)?)?)?"
    r"(?:\s*(?P<ampm>[AaPp][Mm]))?\Z",
    re.ASCII,
)


def _time(s: str):
    """``(day_fraction, pattern)`` for a time of day or a duration, or None.

    Minutes and seconds are two digits and may overflow (`10:60` is 11:00); an hour alone needs
    AM/PM (`10 PM`). A total of a day or more, or a negative one, is a duration shown as
    `[h]:mm:ss`. With AM/PM the hour is 1 to 12."""
    m = _TIME.match(s)
    if m is None or (m["m"] is None and m["ampm"] is None):
        return None
    h = int(m["h"])
    if m["ampm"]:
        if m["sign"] or not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m["ampm"].casefold() == "pm" else 0)
    secs = Decimal(h * 3600 + int(m["m"] or 0) * 60 + int(m["s"] or 0)) + Decimal(m["f"] or 0)
    if m["sign"]:
        secs = -secs
    value = secs / 86400
    if m["ampm"]:
        pattern = "h:mm:ss am/pm" if m["s"] else "h:mm am/pm"
    elif value >= 1 or value < 0:
        pattern = "[h]:mm:ss"
    else:
        pattern = "h:mm:ss" if m["s"] else "h:mm"
    return value, pattern


def _date_or_time(s: str, today: date):
    """``(value, number_format)`` for a date, a time or both, or None.

    A date and a time are split at a space, tried from the right so `2024-01-15 10:30 PM` keeps
    `10:30 PM` whole, or at the `T` of `2024-01-15T10:30`; the pattern joins the two with a space
    either way."""
    t = _time(s)
    if t is not None:
        return t[0], _nf("TIME", t[1])
    d = _numeric_date(s, today) or _wordy_date(s, today)
    if d is not None:
        return d[0], _nf("DATE", d[1])
    splits = [i for i in range(len(s) - 1, 0, -1) if s[i] == " "]
    iso = s.find("T")
    if iso > 0 and re.fullmatch(r"[0-9]{4}-[0-9]+-[0-9]+", s[:iso]):
        splits.append(iso)
    for i in splits:
        d = _numeric_date(s[:i], today) or _wordy_date(s[:i], today)
        t = _time(s[i + 1 :])
        if d is not None and t is not None and "[h]" not in t[1]:
            return float(d[0] + t[0]), _nf("DATE_TIME", f"{d[1]} {t[1]}")
    return None


def _number_value(v):
    v = float(v)
    return int(v) if v.is_integer() and abs(v) < 2**53 else v


def read(text: str, today: date):
    """What USER_ENTERED makes of a string: ``(kind, value, number_format)``.

    ``kind`` is how the result takes a cell's existing format (:func:`apply`): ``"empty"``,
    ``"apostrophe"``, ``"formula"``, ``"error"`` (``value`` is the error type), ``"bool"``,
    ``"number"`` or ``"string"``."""
    if text == "":
        return "empty", None, None
    if text[0] == "'":
        return "apostrophe", (text[1:] or None), None
    if text[0] in "=+":
        return "formula", text, None
    if text.casefold() in ("true", "false"):
        return "bool", text.casefold() == "true", None
    s = text.strip(_SPACE)
    if s.upper() in ERRORS:
        return "error", ERRORS[s.upper()], None
    if s:
        num = _number(s)
        if num is not None:
            return "number", num[0], num[1]
        dt = _date_or_time(s, today)
        if dt is not None:
            return "number", _number_value(dt[0]), dt[1]
    return "string", text, None


def _family(number_format: dict | None):
    return number_format and sheets_format.FAMILY[number_format["type"]]


def apply(cell, written, option: str, today: date):
    """The cell ``written`` leaves behind over ``cell`` under ``option``.

    Measured, a write keeps the cell's number format in these cases and otherwise replaces or
    drops it:

    * a JSON number or boolean, under either option, keeps it;
    * RAW: any string, ``""`` included, drops it, and ``""`` leaves an empty string
      (:data:`sheets_grid.EMPTY_STRING`), as a lone apostrophe does;
    * USER_ENTERED: ``""`` clears the value and keeps it -- a cell that had none keeps an empty
      format (:data:`sheets_grid.CLEARED`), and one never written stays so; an apostrophe drops
      it; a formula keeps
      it; an input that infers a format keeps the existing one when the two are of one family
      (:data:`sheets_format.FAMILY`) and takes the inferred one otherwise; a plain number, a
      boolean, an error or text keeps a number-family format and drops any other.
    """
    current = sheets_grid.cell_format(cell)
    if isinstance(written, bool) or isinstance(written, (int, float)):
        return sheets_grid.cell(sheets_grid.collapse(written), number_format=current)
    if option == "RAW":
        return sheets_grid.cell(written) if written != "" else dict(sheets_grid.EMPTY_STRING)
    kind, value, inferred = read(written, today)
    if kind == "empty":
        if current:
            return sheets_grid.cell(None, number_format=current)
        return dict(sheets_grid.CLEARED) if sheets_grid.written(cell) else None
    if kind == "apostrophe":
        return sheets_grid.cell(value) if value is not None else dict(sheets_grid.EMPTY_STRING)
    if kind == "formula":
        return sheets_grid.cell(value, number_format=current)
    if inferred is not None:
        keep = _family(current) == _family(inferred)
        return sheets_grid.cell(value, number_format=current if keep else inferred)
    keep = current if _family(current) == "number" else None
    if kind == "error":
        return sheets_grid.cell(None, error=value, number_format=keep)
    return sheets_grid.cell(value, number_format=keep)
