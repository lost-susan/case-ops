"""Deterministic date/time finder used to turn extracted text into timeline events.

Nothing here guesses: a string only yields a datetime if it matches an explicit pattern and forms a real calendar
date. Numeric dates such as 03/04/2026 are genuinely ambiguous, so they are read with the configured `date_order`
("mdy" or "dmy") unless one part is greater than 12, which settles the order. Dates without a time of day are placed
at 12:00 in the case's timezone so the calendar day cannot shift when the instant is shown in another timezone."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_MON = r"(?P<mon>january|february|march|april|may|june|july|august|september|october|november|december|" \
       r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
_ISO = re.compile(r"(?<![\d-])(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)")
_NAMED_MDY = re.compile(rf"\b{_MON}\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I)
_NAMED_DMY = re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?\s+(?P<y>\d{{4}})\b", re.I)
_NUMERIC = re.compile(r"(?<![\d/.])(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{4}|\d{2})(?![\d/])")
_CLOCK = r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?:\s*(?P<ap>[AaPp])\.?[Mm]\.?)?"
_TIME_AFTER = re.compile(rf"^(?:\s*(?:,|at|@|-|–|T)\s*|\s+){_CLOCK}", re.I)
CLOCK_RE = re.compile(rf"^{_CLOCK}$")


def to_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def expand_year(raw: str) -> int:
    n = int(raw)
    if len(raw) == 4:
        return n
    return 2000 + n if n < 70 else 1900 + n


def numeric_date(a: int, b: int, year: str, order: str) -> date | None:
    """a/b/year with the order made explicit; a part greater than 12 can only be the day."""
    y = expand_year(year)
    if a > 12 and b <= 12:
        return to_date(y, b, a)
    if b > 12 and a <= 12:
        return to_date(y, a, b)
    if a <= 12 and b <= 12:
        return to_date(y, a, b) if order == "mdy" else to_date(y, b, a)
    return None


def parse_clock(h: int, mi: int, s: int | None = None, ampm: str | None = None) -> time | None:
    s = s or 0
    if ampm:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ampm.lower() == "p" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None
    return time(h, mi, s)


def parse_clock_text(text: str) -> time | None:
    m = CLOCK_RE.match(text.strip())
    if not m:
        return None
    return parse_clock(int(m["h"]), int(m["mi"]), int(m["s"]) if m["s"] else None, m["ap"])


def localize(d: date, t: time | None, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, t or time(12, 0), tzinfo=tz)


def _time_after(text: str, pos: int) -> time | None:
    m = _TIME_AFTER.match(text[pos:pos + 40])
    if not m:
        return None
    return parse_clock(int(m["h"]), int(m["mi"]), int(m["s"]) if m["s"] else None, m["ap"])


def find_first(text: str, tz: ZoneInfo, date_order: str = "mdy") -> datetime | None:
    """The earliest explicit calendar date in `text` (with a time of day if one directly follows), else None."""
    hits: list[tuple[int, date, time | None]] = []
    for m in _ISO.finditer(text):
        d = to_date(int(m["y"]), int(m["m"]), int(m["d"]))
        if d:
            hits.append((m.start(), d, _time_after(text, m.end())))
    for rx in (_NAMED_MDY, _NAMED_DMY):
        for m in rx.finditer(text):
            d = to_date(int(m["y"]), _MONTH_NAMES[m["mon"].lower()], int(m["d"]))
            if d:
                hits.append((m.start(), d, _time_after(text, m.end())))
    for m in _NUMERIC.finditer(text):
        d = numeric_date(int(m["a"]), int(m["b"]), m["y"], date_order)
        if d:
            hits.append((m.start(), d, _time_after(text, m.end())))
    if not hits:
        return None
    _, d, t = min(hits, key=lambda h: h[0])
    return localize(d, t, tz)
