"""Extract a target date / time from market text.

If the title doesn't carry an explicit date, we fall back to the market's
``end_time`` (close time) which Polymarket already provides. That timestamp
is the resolution moment for almost every weather market.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from dateutil import parser as du_parser
except ImportError:  # pragma: no cover
    du_parser = None  # type: ignore


_TODAY_RE = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.IGNORECASE)
_TONIGHT_RE = re.compile(r"\btonight\b", re.IGNORECASE)

# "May 30", "May 30, 2026", "May 30th"
_MD_RE = re.compile(
    r"\b("
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?"
    r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def extract_date(text: str, *, fallback: Optional[datetime] = None) -> Optional[datetime]:
    """Return a tz-aware UTC datetime referenced by ``text``, or ``fallback``.

    The returned datetime points at the *resolution moment*. For "today" /
    "tomorrow" we resolve to 23:59 UTC of that day to be conservative.
    """
    if not text:
        return fallback

    now = datetime.now(timezone.utc)

    if _TONIGHT_RE.search(text):
        return now.replace(hour=23, minute=59, second=0, microsecond=0)
    if _TODAY_RE.search(text):
        return now.replace(hour=23, minute=59, second=0, microsecond=0)
    if _TOMORROW_RE.search(text):
        d = now + timedelta(days=1)
        return d.replace(hour=23, minute=59, second=0, microsecond=0)

    m = _MD_RE.search(text)
    if m and du_parser is not None:
        try:
            dt = du_parser.parse(m.group(0), default=now)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc, hour=23, minute=59, second=0)
            # If the parsed date is in the past relative to now (no year given),
            # bump it to next year.
            if dt < now - timedelta(days=2) and m.group(3) is None:
                dt = dt.replace(year=now.year + 1)
            return dt
        except (ValueError, OverflowError):
            pass

    return fallback
