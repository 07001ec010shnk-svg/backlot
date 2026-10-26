"""How Google Sheets displays a number: its General rendering, and the number formats a
``USER_ENTERED`` write can give a cell.

Every rule here was measured against the live Sheets v4 API on 2026-10-26, on an ``en_US``
spreadsheet. The patterns rendered are exactly the ones :mod:`backlot.sheets_input` infers; a
pattern outside that set is not a number format this server can produce, so it is refused rather
than approximated.

Rounding is half away from zero on the number's shortest decimal spelling, not on its binary
value: ``2.675`` under ``#,##0.00`` displays ``2.68`` where ``round(2.675, 2)`` is ``2.67``.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal

# The serial-number epoch: day 0 is 1899-12-30, so day 60 is 1900-02-28 and day 61 1900-03-01.
_EPOCH_DAYS = 693594  # date(1899, 12, 30).toordinal()

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# The family a number format belongs to, by its `type`. A USER_ENTERED write keeps a cell's format
# when what it infers is of the same family, so this grouping is what decides inheritance
# (`sheets_input.apply`). NUMBER, CURRENCY and SCIENTIFIC are one family: `$5` into a `#,##0`
# cell keeps `#,##0`, and `1,234` into a `"$"#,##0` cell keeps the currency.
FAMILY = {
    "NUMBER": "number",
    "CURRENCY": "number",
    "SCIENTIFIC": "number",
    "PERCENT": "percent",
    "DATE": "date",
    "TIME": "time",
    "DATE_TIME": "date_time",
}

# The types `dateTimeRenderOption=FORMATTED_STRING` renders as their display string.
DATE_TIME_TYPES = frozenset({"DATE", "TIME", "DATE_TIME"})


def _dec(x) -> Decimal:
    """``x`` as the decimal it is spelled as: ``repr`` of a float is its shortest round-trip form."""
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


def _fixed(d: Decimal, places: int) -> str:
    """``d`` rounded half away from zero to ``places`` decimals, a negative zero shown unsigned."""
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return format(q, "f")


def _strip(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _sci(d: Decimal, places: int, *, strip: bool) -> str:
    """Scientific notation with a ``places``-decimal mantissa and an exponent of at least two
    digits — ``1.23E+05``, ``1.00E+100`` — or the mantissa's trailing zeros removed (``1E+15``)."""
    if d == 0:
        mantissa, exp = Decimal(0), 0
    else:
        exp = d.adjusted()
        mantissa = (d.scaleb(-exp)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if abs(mantissa) >= 10:
            mantissa, exp = mantissa / 10, exp + 1
    m = _fixed(mantissa, places)
    if strip:
        m = _strip(m)
    return f"{m}E{'+' if exp >= 0 else '-'}{abs(exp):02d}"


def general(x) -> str:
    """A number's display with no number format.

    Ten significant digits, more when the integer part needs them (``123456789012`` shows whole),
    trailing zeros removed. A magnitude under ``1e-10`` shows ``0``. From the point where
    ``floor(log10(|x|))`` reaches 15 it is scientific with six significant digits
    (``1.23457E+15``, ``1E+21``), and that boundary is the floating-point logarithm's:
    ``999999999999998`` already shows ``1E+15`` while ``999999999999990`` shows whole.
    """
    if x == 0 or abs(x) < 1e-10:
        return "0"
    d = _dec(x)
    exp = math.floor(math.log10(abs(x)))
    if exp >= 15:
        return _sci(d, 5, strip=True)
    return _strip(_fixed(d, max(0, 9 - exp)))


# A numeric pattern: an optional quoted currency before or after a body.
_NUMERIC = re.compile(
    r'(?:"(?P<pre>[^"]+)")?(?P<body>#,##0(?:\.00)?|0(?:\.00)?%|0\.00E\+00)(?:"(?P<suf>[^"]+)")?\Z'
)


def _numeric(x, m: re.Match) -> str:
    body, pre, suf = m["body"], m["pre"] or "", m["suf"] or ""
    d = _dec(x)
    if body == "0.00E+00":
        return _sci(d, 2, strip=False)
    if body.endswith("%"):
        d *= 100
    places = 2 if ".00" in body else 0
    digits = _fixed(abs(d), places)
    if body.startswith("#,##0"):
        whole, _, frac = digits.partition(".")
        digits = f"{int(whole):,}" + (f".{frac}" if frac else "")
    sign = "-" if _fixed(d, places).startswith("-") else ""
    return f"{sign}{pre}{digits}{suf}{'%' if body.endswith('%') else ''}"


# Date and time tokens, longest first so `mmmm` is not read as `mm` twice.
_TOKEN = re.compile(r"\[h\]|am/pm|yyyy|yy|mmmm|mmm|mm|m|ddd|dd|d|h|ss|s")


def _civil(days: int) -> tuple[int, int, int]:
    """``(year, month, day)`` of a day count from 0001-01-01 = 1, past year 9999 as well: real
    displays serial 2958466 as ``10000-01-01`` where :mod:`datetime` stops at 9999."""
    # Howard Hinnant's days_from_civil inverse, shifted from the 1970 epoch to ordinal 1.
    z = days - 719163 + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y), m, d


def _date_time(x, pattern: str) -> str:
    """A serial number under a DATE, TIME or DATE_TIME pattern.

    The value is rounded half up to the whole second first and only then cut to what the pattern
    shows: ``10:30:45`` under ``h:mm`` is ``10:30``, while ``0.999999`` of a day (86399.91 s) is
    ``0:00``. A time of day wraps (``-0.25`` is ``18:00``), and ``[h]`` counts elapsed hours,
    keeping a sign (``-0.5`` is ``-12:00:00``).
    """
    # The product is taken in floating point, as real takes it: `0.937505787037037` of a day is
    # 81000.5 s there and rounds to 10:30:01, where the exact decimal product falls just short.
    total = int(_dec(float(x) * 86400).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    days, secs = divmod(total, 86400)
    year, month, day = _civil(_EPOCH_DAYS + days)
    hour, rem = divmod(secs, 3600)
    minute, second = divmod(rem, 60)
    ampm = "am/pm" in pattern
    out, prev = [], None
    pos = 0
    for tok in _TOKEN.finditer(pattern):
        out.append(pattern[pos : tok.start()])
        pos = tok.end()
        t = tok.group()
        if t == "[h]":
            hours, rest = divmod(abs(total), 3600)
            out.append(("-" if total < 0 else "") + str(hours))
            minute, second = divmod(rest, 60)
        elif t == "h":
            out.append(str((hour % 12 or 12) if ampm else hour))
        elif t in ("mm", "m") and prev in ("h", "[h]"):
            out.append(f"{minute:02d}" if t == "mm" else str(minute))
        elif t in ("ss", "s"):
            out.append(f"{second:02d}" if t == "ss" else str(second))
        elif t == "am/pm":
            out.append("AM" if hour < 12 else "PM")
        elif t == "yyyy":
            out.append(f"{year:04d}")
        elif t == "yy":
            out.append(f"{year % 100:02d}")
        elif t == "mmmm":
            out.append(MONTHS[month - 1])
        elif t == "mmm":
            out.append(MONTHS[month - 1][:3])
        elif t == "mm":
            out.append(f"{month:02d}")
        elif t == "m":
            out.append(str(month))
        elif t == "ddd":
            out.append(WEEKDAYS[(_EPOCH_DAYS + days - 1) % 7])
        elif t == "dd":
            out.append(f"{day:02d}")
        else:  # "d"
            out.append(str(day))
        prev = t
    out.append(pattern[pos:])
    return "".join(out)


def render(x, number_format: dict) -> str:
    """A number's display under one of the number formats a write can give a cell."""
    pattern = number_format["pattern"]
    if number_format["type"] in DATE_TIME_TYPES:
        return _date_time(x, pattern)
    m = _NUMERIC.match(pattern)
    if m is None:
        raise ValueError(f"sheets: no rendering for number format pattern {pattern!r}")
    return _numeric(x, m)
