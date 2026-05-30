"""Parse the temperature threshold and the market kind from market text.

Returns a dict::

    {
      "threshold_value": 80.0,
      "unit": "F",                     # "F" or "C"
      "threshold_c": 26.7,             # always normalized to Celsius
      "threshold_f": 80.0,             # always normalized to Fahrenheit
      "market_kind":  "highest_gte" | "highest_lt"
                    | "lowest_lte"   | "lowest_gt"
                    | "exact_temp"   | "unknown",
      "rules_clear": True,
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


# Single number with optional unit. Matches "80°F", "80 F", "80F", "26°C",
# "26 C", "32 degrees", "75 deg".
_TEMP_RE = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*"
    r"(?:°|deg(?:rees)?\s*)?\s*"
    r"(F|C|fahrenheit|celsius)?",
    re.IGNORECASE,
)


# Phrases hinting at "highest" / "max"
_HIGH_HINTS = (
    "high", "highest", "max", "maximum", "peak",
    "warmest", "hottest", "top temperature",
)
# Phrases hinting at "lowest" / "min"
_LOW_HINTS = (
    "low", "lowest", "min", "minimum",
    "coldest", "coolest", "bottom temperature",
)
# Direction operators
_GTE_HINTS = (
    "or higher", "or hotter", "or above", "or warmer",
    "exceed", "exceeds", "exceeding",
    "above", "over", "more than", "greater than", ">=", "≥",
    "reach", "reaches", "hit", "hits",
    "surpass", "surpasses", "at least",
)
_LT_HINTS = (
    "or lower", "or colder", "or below", "or cooler",
    "below", "under", "less than", "fewer than",
    "<=", "≤", "<", "drop to", "drops to", "fall to",
    "stay below", "remain below", "at most",
)
_EXACT_HINTS = (
    "exactly", "be exactly", "equal to", "equal", "=",
)


def _has_any(text: str, words) -> bool:
    t = text.lower()
    return any(w in t for w in words)


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _pick_threshold(text: str) -> Optional[Dict[str, Any]]:
    """Pick the most likely threshold number in the text.

    Heuristic: prefer a number that is followed/preceded by an explicit unit.
    If multiple numbers carry units, keep the one closest to a hint word
    ("high", "low", "above", "below", ...).
    """
    candidates = []
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
        candidates.append((m.start(), val, unit, m.group(0)))

    if not candidates:
        return None

    # Filter to plausible temperature ranges.
    def _plausible(val: float, unit: Optional[str]) -> bool:
        if unit == "F":
            return -60.0 <= val <= 140.0
        if unit == "C":
            return -50.0 <= val <= 60.0
        # Without explicit unit, accept temperatures-shaped values only.
        return -60.0 <= val <= 140.0

    candidates = [c for c in candidates if _plausible(c[1], c[2])]
    if not candidates:
        return None

    with_unit = [c for c in candidates if c[2] is not None]
    chosen = with_unit[0] if with_unit else candidates[0]
    pos, val, unit, _raw = chosen

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


def parse_threshold(title: str, description: str = "") -> Dict[str, Any]:
    text = f"{title}\n{description}"
    low = text.lower()

    info = _pick_threshold(text) or {}
    has_threshold = bool(info)

    is_high = _has_any(low, _HIGH_HINTS)
    is_low = _has_any(low, _LOW_HINTS)
    is_gte = _has_any(low, _GTE_HINTS)
    is_lt = _has_any(low, _LT_HINTS)
    is_exact = _has_any(low, _EXACT_HINTS) and not is_gte and not is_lt

    market_kind = "unknown"
    rules_clear = False

    if is_exact and has_threshold:
        market_kind = "exact_temp"
        rules_clear = True
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
        # "high in NYC of 80F" - imply gte
        market_kind = "highest_gte"
        rules_clear = True
    elif is_low and has_threshold:
        market_kind = "lowest_lte"
        rules_clear = True
    elif is_gte and has_threshold:
        # "Will it be 80F or hotter in NYC?" -> assume daily high comparison
        market_kind = "highest_gte"
        rules_clear = True
    elif is_lt and has_threshold:
        market_kind = "highest_lt"
        rules_clear = True

    out: Dict[str, Any] = {
        "threshold_value": info.get("threshold_value"),
        "unit": info.get("unit"),
        "threshold_c": info.get("threshold_c"),
        "threshold_f": info.get("threshold_f"),
        "market_kind": market_kind,
        "rules_clear": rules_clear and has_threshold,
    }
    return out
