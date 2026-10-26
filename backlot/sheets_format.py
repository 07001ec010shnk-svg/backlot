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
from typing import NamedTuple

# The serial-number epoch: day 0 is 1899-12-30, so day 60 is 1900-02-28 and day 61 1900-03-01.
_EPOCH_DAYS = 693594  # date(1899, 12, 30).toordinal()

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS = tuple(name[:3] for name in WEEKDAY_NAMES)

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
    "TEXT": "text",
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


def _sections(pattern: str) -> list[str]:
    """A pattern's `;`-separated sections, a `;` inside quotes or after a backslash kept."""
    out, cur, quoted, escaped = [], "", False, False
    for ch in pattern:
        if escaped:
            cur, escaped = cur + ch, False
        elif ch == "\\":
            cur, escaped = cur + ch, True
        elif ch == '"':
            cur, quoted = cur + ch, not quoted
        elif ch == ";" and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return [*out, cur]


class _Numeric(NamedTuple):
    prefix: str
    suffix: str
    int_zeros: int  # `0`s before the decimal point: digits always shown
    grouping: bool
    scale: int  # trailing commas, each dividing by 1000
    frac_zeros: int  # `0`s after the point: decimals always shown
    frac_optional: int  # `#`/`?` after the point: shown when not zero
    point: bool
    percent: int
    exp_sign: str | None  # "+" for `E+`, "-" for `E-`, None for no exponent
    exp_zeros: int


def _numeric_section(section: str) -> _Numeric | None:
    """One section read into its parts, or None for one this cannot display -- a condition
    (`[>100]`), a literal between digits, a text placeholder."""
    prefix, suffix, digits_seen, after_digits = [], [], False, False
    int_zeros = frac_zeros = frac_optional = percent = scale = exp_zeros = 0
    grouping = point = False
    exp_sign = None
    part = "int"
    i = 0
    while i < len(section):
        ch = section[i]
        literal = None
        if ch == '"':
            end = section.find('"', i + 1)
            literal, i = (
                section[i + 1 : end if end >= 0 else len(section)],
                (end if end >= 0 else len(section)),
            )
        elif ch == "\\" and i + 1 < len(section):
            literal, i = section[i + 1], i + 1
        elif ch == "_" and i + 1 < len(section):
            literal, i = " ", i + 1
        elif ch == "*" and i + 1 < len(section):
            i += 1
        elif ch == "[":
            end = section.find("]", i)
            if end < 0 or not section[i + 1 : end].isalnum():
                return None  # a condition, not a colour
            i = end
        elif ch in "0#?":
            if after_digits and part != "exp":
                return None
            digits_seen = True
            if part == "int":
                int_zeros += ch == "0"
            elif part == "frac":
                frac_zeros += ch == "0"
                frac_optional += ch != "0"
            else:
                exp_zeros += 1
        elif ch == "," and digits_seen and part == "int":
            if i + 1 < len(section) and section[i + 1] in "0#?":
                grouping = True
            else:
                scale += 1
        elif ch == "." and part == "int" and not point:
            point, part, digits_seen = True, "frac", True
        elif ch in "Ee" and i + 1 < len(section) and section[i + 1] in "+-" and digits_seen:
            exp_sign, part, i = section[i + 1], "exp", i + 1
        elif ch == "@":
            return None
        else:
            percent += ch == "%"
            literal = ch
        if literal is not None:
            if digits_seen:
                after_digits = True
                suffix.append(literal)
            else:
                prefix.append(literal)
        i += 1
    if not digits_seen:
        return None
    return _Numeric(
        "".join(prefix),
        "".join(suffix),
        int_zeros,
        grouping,
        scale,
        frac_zeros,
        frac_optional,
        point,
        percent,
        exp_sign,
        exp_zeros,
    )


def _digits(d: Decimal, spec: _Numeric) -> str:
    """``|d|`` under ``spec``, without its sign."""
    places = spec.frac_zeros + spec.frac_optional
    if spec.exp_sign is not None:
        width = max(spec.int_zeros, 1)
        if d == 0:
            mantissa, exp = Decimal(0), 0
        else:
            exp = d.adjusted() - (width - 1)
            mantissa = d.scaleb(-exp).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            if mantissa >= Decimal(10) ** width:
                exp += 1
                mantissa = d.scaleb(-exp).quantize(
                    Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
                )
        body = _decimal_text(abs(mantissa), spec, places)
        sign = "-" if exp < 0 else ("+" if spec.exp_sign == "+" else "")
        return f"{body}E{sign}{abs(exp):0{max(spec.exp_zeros, 1)}d}"
    return _decimal_text(
        abs(d).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), spec, places
    )


def _decimal_text(d: Decimal, spec: _Numeric, places: int) -> str:
    whole, _, frac = format(d, "f").partition(".")
    whole = whole.lstrip("0")
    whole = whole.rjust(spec.int_zeros, "0")
    if spec.grouping and whole:
        whole = f"{int(whole):,}".rjust(len(whole), "0") if whole.lstrip("0") else whole
    frac = frac.ljust(places, "0")[:places]
    keep = spec.frac_zeros + len(frac[spec.frac_zeros :].rstrip("0"))
    frac = frac[:keep]
    return whole + ("." + frac if spec.point and (frac or spec.frac_optional) else "")


def _numeric(x, pattern: str) -> str:
    """A number under a numeric pattern: digit placeholders (`0` always shown, `#` when not zero),
    grouping, a point, `%`, an exponent (`E+00`), literals around them (quoted, escaped, or a
    plain `$`), and up to three sections, positive;negative;zero. Measured patterns:
    `0.0`, `0.000`, `#,##0.0`, `0`, `0.0%`, `0.000E+00`, `0E+0`, `$#,##0`, `#,##0;(#,##0)` and
    `0.00"kg"` beside the patterns a write infers. A pattern this cannot read displays as General."""
    sections = _sections(pattern)
    d = _dec(x)
    negative_section = len(sections) > 1 and d < 0
    if len(sections) > 2 and d == 0:
        section = sections[2]
    elif negative_section:
        section = sections[1]
    else:
        section = sections[0]
    spec = _numeric_section(section)
    if spec is None:
        return general(x)
    d = d * Decimal(100) ** spec.percent / Decimal(1000) ** spec.scale
    text = _digits(d, spec)
    shown_negative = d < 0 and not negative_section and any(c in "123456789" for c in text)
    return f"{'-' if shown_negative else ''}{spec.prefix}{text}{spec.suffix}"


# Date and time tokens, longest first so `mmmm` is not read as `mm` twice.
_TOKEN = re.compile(r'"[^"]*"|\[h\]|am/pm|yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s', re.I)


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
    ampm = "am/pm" in pattern.lower()
    out, prev = [], None
    pos = 0
    for tok in _TOKEN.finditer(pattern):
        out.append(pattern[pos : tok.start()])
        pos = tok.end()
        t = tok.group()
        if t.startswith('"'):
            out.append(t[1:-1])
            continue
        t = t.lower()
        if t == "[h]":
            hours, rest = divmod(abs(total), 3600)
            out.append(("-" if total < 0 else "") + str(hours))
            minute, second = divmod(rest, 60)
        elif t in ("h", "hh"):
            shown = (hour % 12 or 12) if ampm else hour
            out.append(f"{shown:02d}" if t == "hh" else str(shown))
        elif t in ("mm", "m") and prev in ("h", "hh", "[h]"):
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
        elif t == "dddd":
            out.append(WEEKDAY_NAMES[(_EPOCH_DAYS + days - 1) % 7])
        elif t == "ddd":
            out.append(WEEKDAYS[(_EPOCH_DAYS + days - 1) % 7])
        elif t == "dd":
            out.append(f"{day:02d}")
        else:  # "d"
            out.append(str(day))
        prev = t
    out.append(pattern[pos:])
    return "".join(out)


# The pattern a number format that names a type and no pattern displays with. Measured on an
# `en_US` spreadsheet: CURRENCY as dollars to the cent, DATE, TIME and DATE_TIME as these; NUMBER,
# SCIENTIFIC and TEXT as General, and PERCENT as General a hundred times over with a `%`.
_TYPE_PATTERNS = {
    "CURRENCY": '"$"#,##0.00',
    "DATE": "m/d/yyyy",
    "TIME": "h:mm:ss am/pm",
    "DATE_TIME": "m/d/yyyy h:mm:ss",
}


def render(x, number_format: dict) -> str:
    """A number's display under a number format: its pattern, or its type's when it states none."""
    kind = number_format["type"]
    pattern = number_format.get("pattern") or _TYPE_PATTERNS.get(kind)
    if not pattern:
        return general(_dec(x) * 100) + "%" if kind == "PERCENT" else general(x)
    if kind in DATE_TIME_TYPES:
        return _date_time(x, pattern)
    return _numeric(x, pattern)
