"""Top-level filter that decides whether a market is a *weather temperature*
market and runs the full parsing pipeline (city + date + threshold).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from polyweat.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from polyweat.api.open_meteo import OpenMeteoClient
from polyweat.models import ParsedMarket
from polyweat.parser.cities import lookup_city
from polyweat.parser.city_parser import extract_city
from polyweat.parser.date_parser import extract_date
from polyweat.parser.threshold_parser import parse_threshold

log = get_logger("filter")


# A market is a *weather/temperature* candidate if its text matches at least
# one of these patterns.
_WEATHER_KEYWORDS = (
    "temperature", "temp ", "temp,", "temp.",
    "weather", "forecast",
    "°f", "°c", "fahrenheit", "celsius",
    "hottest", "coldest", "warmest", "coolest",
    "heatwave", "heat wave", "cold snap",
    "high in", "low in",
    "highest temperature", "lowest temperature",
)

# Markets we explicitly REJECT even if "weather" appears (sports / politics /
# culture / news / finance / crypto crossovers we never want to touch).
_BLOCKLIST_KEYWORDS = (
    # sports
    "nba", "nfl", "mlb", "nhl", "premier league", "champions league",
    "world cup", "super bowl", "olympics", "tennis", "golf", "ufc",
    "soccer", "football match",
    # politics
    "election", "president", "senate", "congress", "vote", "ballot",
    "trump", "biden", "harris", "putin", "xi jinping",
    # crypto / finance
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
    "stock", "nasdaq", "s&p", "fed rate", "fed decision",
    # culture / entertainment
    "oscar", "grammy", "movie", "film", "tv show", "netflix",
    "song of the year", "billboard",
    # news / world events
    "war", "ceasefire", "treaty",
)


def is_weather_temperature_market(question: str, description: str = "") -> bool:
    blob = f"{question}\n{description}".lower()
    if any(b in blob for b in _BLOCKLIST_KEYWORDS):
        return False
    if not any(k in blob for k in _WEATHER_KEYWORDS):
        return False
    # Must mention temperature or a unit, not just "weather"
    has_temp_signal = (
        "temp" in blob or "°f" in blob or "°c" in blob
        or "fahrenheit" in blob or "celsius" in blob
        or "hottest" in blob or "coldest" in blob
        or "warmest" in blob or "coolest" in blob
        or "high in" in blob or "low in" in blob
        or "highest temperature" in blob or "lowest temperature" in blob
    )
    return has_temp_signal


def parse_market(
    nm: Dict[str, Any],
    *,
    geocoder: "Optional[OpenMeteoClient]" = None,
) -> ParsedMarket:
    """Parse a normalized Gamma market dict into a ParsedMarket.

    ``nm`` is the output of :func:`GammaClient.normalize`.
    """
    title: str = nm.get("question") or ""
    description: str = nm.get("description") or ""

    pm = ParsedMarket(
        market_id=str(nm.get("market_id") or ""),
        title=title,
        description=description,
        end_time=nm.get("end_time"),
        yes_token_id=nm.get("yes_token_id"),
        no_token_id=nm.get("no_token_id"),
        yes_price=nm.get("yes_price"),
        no_price=nm.get("no_price"),
        volume_usd=float(nm.get("volume_usd") or 0.0),
        liquidity_usd=float(nm.get("liquidity_usd") or 0.0),
    )

    # ----- city -----
    city_candidate = extract_city(f"{title}\n{description}")
    if city_candidate:
        known = lookup_city(city_candidate) or lookup_city(city_candidate.lower())
        if known:
            pm.city, pm.city_lat, pm.city_lon, pm.city_tz = known
        elif geocoder is not None:
            geo = geocoder.geocode(city_candidate)
            if geo:
                lat, lon, tz, canonical = geo
                pm.city, pm.city_lat, pm.city_lon, pm.city_tz = (
                    canonical, lat, lon, tz,
                )

    # ----- date -----
    pm.target_date = extract_date(f"{title}\n{description}", fallback=pm.end_time)

    # ----- threshold + market kind -----
    th = parse_threshold(title, description)
    pm.threshold_c = th.get("threshold_c")
    pm.threshold_f = th.get("threshold_f")
    pm.market_kind = th.get("market_kind") or "unknown"
    pm.unit = th.get("unit") or "F"
    pm.rules_clear = bool(th.get("rules_clear"))

    # ----- parse score -----
    score = 0.0
    if pm.city:
        score += 0.3
    if pm.target_date:
        score += 0.2
    if pm.threshold_c is not None:
        score += 0.3
    if pm.market_kind not in ("unknown",):
        score += 0.1
    if pm.rules_clear:
        score += 0.1
    pm.parse_score = round(min(1.0, score), 3)

    return pm
