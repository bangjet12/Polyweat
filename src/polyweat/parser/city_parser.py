"""Extract a city name from a market title/description.

Strategy:
  1. Try to match aliases from KNOWN_CITIES (fast, deterministic).
  2. Fall back to common phrases like "in <City>" / "at <City>".
The caller is responsible for geocoding any unknown match via Open-Meteo.
"""

from __future__ import annotations

import re
from typing import Optional

from polyweat.parser.cities import KNOWN_CITIES, lookup_city


# Sort longer aliases first so "new york city" wins over "new york".
_ALIASES_SORTED = sorted(KNOWN_CITIES.keys(), key=len, reverse=True)
_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ALIASES_SORTED) + r")\b",
    re.IGNORECASE,
)

# "in New York", "at Chicago", "for Tokyo"
_PREP_RE = re.compile(
    r"\b(?:in|at|for)\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})",
    re.UNICODE,
)


def extract_city(text: str) -> Optional[str]:
    """Return a city alias (lower-case) likely referenced in ``text``.

    Returns the *canonical* city name from KNOWN_CITIES if found, otherwise
    a free-form candidate string the caller can pass to a geocoder, or None.
    """
    if not text:
        return None
    blob = text.replace("\n", " ")

    m = _ALIAS_RE.search(blob)
    if m:
        canonical = lookup_city(m.group(1))
        if canonical:
            return canonical[0]

    # Fallback: capitalised noun phrase after a preposition.
    m = _PREP_RE.search(blob)
    if m:
        cand = m.group(1).strip(" .,?!")
        if 2 <= len(cand) <= 40:
            return cand

    return None
