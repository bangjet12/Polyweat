"""Parse the temperature threshold and the market kind from market text.

Returns a dict::

    {
      "threshold_value": 80.0,
      "unit": "F",                     # "F" or "C"
      "threshold_c": 26.7,             # always normalized to Celsius
      "threshold_f": 80.0,             # always normalized to Fahrenheit
      "market_kind":  "highest_gte" | "highest_lt"
                    | "lowest_lte"   | "lowest_gt"
                    | "exact_temp"   | "range"
                    | "unknown",
      "rules_clear": True,
    }

Range markets ("between 70F and 80F") are *recognised* and returned with
``market_kind="range"`` so the decision engine can SKIP them deterministically
instead of misclassifying them as a single-bound market.

All hint matching is anchored on word boundaries so that, e.g., the string
``higher`` does NOT trigger the ``high`` hint and ``hottest`` does NOT
trigger ``hot``. This was a real source of misclassification in earlier
versions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


# Single number with optional unit. Matches "80°F", "80 F", "80F", "26°C",
# "26 C", "32 degrees", "75 deg".
_TEMP_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*"
    r"(?:°|deg(?:rees)?\s*)?\s*"
    r"(F|C|fahrenheit|celsius)?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------
# Hint regexes (word-boundary anchored to avoid substring traps).
# ---------------------------------------------------------------------

# "highest", "high", "max", "maximum", "peak", "hottest", "warmest",
# "top temperature". Note: "high" matches "high" but NOT "higher",
# because of the negative lookahead; "higher" is a *direction* word
# meaning "greater than", not a "high vs low" word.
_HIGH_RE = re.compile(
    r"\b(?:high(?!er)|highest|max(?:imum)?|peak|hot(?:test)?|warm(?:est)?"
    r"|top\s+temperature)\b",
    re.IGNORECASE,
)
# "low", "lowest", "min", "minimum", "coldest", "coolest", "bottom temperature"
# Excludes "lower" (direction word) via negative lookahead.
_LOW_RE = re.compile(
    r"\b(?:low(?!er)|lowest|min(?:imum)?|cold(?:est)?|cool(?:est)?"
    r"|bottom\s+temperature)\b",
    re.IGNORECASE,
)

# Direction operators (>=)
_GTE_RE = re.compile(
    r"\b(?:or\s+(?:higher|hotter|above|warmer|more)"
    r"|exceeds?|exceeding|reaches?|hits?|surpass(?:es)?"
    r"|above|over|greater\s+than|more\s+than|at\s+least|higher\s+than|hotter\s+than|warmer\s+than)\b"
    r"|>=|≥",
    re.IGNORECASE,
)
# Direction operators (<=)
_LT_RE = re.compile(
    r"\b(?:or\s+(?:lower|colder|below|cooler|less)"
    r"|below|under|less\s+than|fewer\s+than|at\s+most"
    r"|drops?\s+to|falls?\s+to|stays?\s+below|remains?\s+below"
    r"|lower\s+than|colder\s+than|cooler\s+than)\b"
    r"|<=|≤",
    re.IGNORECASE,
)
_EXACT_RE = re.compile(
    r"\b(?:exactly|equal\s+to|equals?)\b",
    re.IGNORECASE,
)
# Range / between markets - any of these patterns -> SKIP via market_kind="range".
# Numbers may be immediately followed by an optional unit (F, C, °F, °C, deg).
_RANGE_RE = re.compile(
    r"\bbetween\s+\d{1,3}(?:\.\d+)?\s*(?:°|deg(?:rees)?\s*)?[FC]?\b"
    r".{0,12}\band\s+\d{1,3}(?:\.\d+)?"
    r"|\bfrom\s+\d{1,3}(?:\.\d+)?\s*(?:°|deg(?:rees)?\s*)?[FC]?\b"
    r".{0,12}\bto\s+\d{1,3}(?:\.\d+)?"
    r"|\b\d{1,3}(?:\.\d+)?\s*(?:°|deg)?\s*[FC]?\s*(?:to|–|—|-)\s*\d{1,3}(?:\.\d+)?\s*(?:°|deg)?\s*[FC]\b",
    re.IGNORECASE,
)


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------------
# Threshold extractor
# ---------------------------------------------------------------------

def _candidates(text: str) -> list:
    out = []
    for m in _TEMP_RE.finditer(text):
        try:
            val = float(m.group(1))
        except (TypeError, ValueError):
            continue
        unit_raw = (m.group(2) or "").lower()
        unit: Optional[str] = None
        if unit_raw.startswith("f"):
            unit = "F"
        elif unit_raw.startswith("c"):
            unit = "C"
        out.append((m.start(), val, unit, m.group(0)))
    return out


def _plausible(val: float, unit: Optional[str]) -> bool:
    if unit == "F":
        return -60.0 <= val <= 140.0
    if unit == "C":
        return -50.0 <= val <= 60.0
    # Without unit: shape-only filter.
    return -60.0 <= val <= 140.0


def _hint_positions(text: str) -> list:
    """Return character positions of all directional/superlative hints."""
    positions = []
    for rgx in (_HIGH_RE, _LOW_RE, _GTE_RE, _LT_RE, _EXACT_RE):
        for m in rgx.finditer(text):
            positions.append(m.start())
    return positions


def _pick_threshold(text: str) -> Optional[Dict[str, Any]]:
    """Pick the most likely threshold number.

    Strategy:
      1. Drop implausible values.
      2. Prefer numbers that carry an explicit unit.
      3. Among those, pick the one whose position is closest to a
         directional / superlative hint word, since the operator's
         intent is "<hint> ... <number><unit>".
    """
    cs = [c for c in _candidates(text) if _plausible(c[1], c[2])]
    if not cs:
        return None

    with_unit = [c for c in cs if c[2] is not None]
    pool = with_unit or cs
    hints = _hint_positions(text)
    if hints:
        def _dist(c):
            pos = c[0]
            return min(abs(pos - h) for h in hints)
        pool = sorted(pool, key=_dist)

    pos, val, unit, _raw = pool[0]
    if unit is None:
        # Best-effort guess: F if value > 50, else C.
        unit = "F" if val > 50 else "C"

    if unit == "F":
        threshold_f = val
        threshold_c = _f_to_c(val)
    else:
        threshold_c = val
        threshold_f = _c_to_f(val)

    return {
        "threshold_value": val,
        "unit": unit,
        "threshold_f": round(threshold_f, 4),
        "threshold_c": round(threshold_c, 4),
    }


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def parse_threshold(title: str, description: str = "") -> Dict[str, Any]:
    text = f"{title}\n{description}"

    # Range markets: detected up-front and short-circuited so the rest of
    # the heuristics don't get a chance to mis-label them.
    is_range = bool(_RANGE_RE.search(text))

    info = _pick_threshold(text) or {}
    has_threshold = bool(info)

    is_high = bool(_HIGH_RE.search(text))
    is_low = bool(_LOW_RE.search(text))
    is_gte = bool(_GTE_RE.search(text))
    is_lt = bool(_LT_RE.search(text))
    is_exact = bool(_EXACT_RE.search(text)) and not (is_gte or is_lt)

    market_kind = "unknown"
    rules_clear = False

    if is_range and has_threshold:
        market_kind = "range"
        rules_clear = True  # parsed clearly, but decision engine SKIPs ranges
    elif is_exact and has_threshold:
        market_kind = "exact_temp"
        rules_clear = True
    elif is_high and is_low:
        # Ambiguous (e.g. "Will the daily LOW be HIGHER than 60F" - already
        # caught above by negative-lookahead, but defense in depth).
        market_kind = "unknown"
        rules_clear = False
    elif is_high and is_gte:
        market_kind = "highest_gte"
        rules_clear = True
    elif is_high and is_lt:
        market_kind = "highest_lt"
        rules_clear = True
    elif is_low and is_lt:
        market_kind = "lowest_lte"
        rules_clear = True
    elif is_low and is_gte:
        market_kind = "lowest_gt"
        rules_clear = True
    elif is_high and has_threshold:
        market_kind = "highest_gte"
        rules_clear = True
    elif is_low and has_threshold:
        market_kind = "lowest_lte"
        rules_clear = True
    elif is_gte and has_threshold:
        # "Will it be 80F or hotter in NYC?" -> assume daily-high comparison.
        market_kind = "highest_gte"
        rules_clear = True
    elif is_lt and has_threshold:
        market_kind = "highest_lt"
        rules_clear = True

    return {
        "threshold_value": info.get("threshold_value"),
        "unit": info.get("unit"),
        "threshold_c": info.get("threshold_c"),
        "threshold_f": info.get("threshold_f"),
        "market_kind": market_kind,
        "rules_clear": rules_clear and has_threshold,
        "is_range": is_range,
    }
