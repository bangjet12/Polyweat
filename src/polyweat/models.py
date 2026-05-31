"""Plain dataclasses used to pass structured data between modules.

We deliberately keep these dependency-free so they can be serialized,
logged, or stored in SQLite without surprises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# ---------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------

# Market kind:
#   highest_gte  : "highest temperature >= threshold"  -> YES if max(forecast) >= threshold
#   highest_lt   : "highest temperature <  threshold"  -> YES if max(forecast) <  threshold
#   lowest_lte   : "lowest  temperature <= threshold"  -> YES if min(forecast) <= threshold
#   lowest_gt    : "lowest  temperature >  threshold"  -> YES if min(forecast) >  threshold
#   exact_temp   : "temperature equals X" - SKIPPED by default
#   unknown      : SKIP
MARKET_KINDS = (
    "highest_gte",
    "highest_lt",
    "lowest_lte",
    "lowest_gt",
    "exact_temp",
    "unknown",
)


@dataclass
class ParsedMarket:
    market_id: str
    title: str
    description: str
    end_time: Optional[datetime]
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    yes_price: Optional[float]
    no_price: Optional[float]
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0

    # Parsed weather details (None if not parseable)
    city: Optional[str] = None
    city_lat: Optional[float] = None
    city_lon: Optional[float] = None
    city_tz: Optional[str] = None
    target_date: Optional[datetime] = None  # local-day midnight in city TZ (or specific hour)
    threshold_c: Optional[float] = None
    threshold_f: Optional[float] = None
    market_kind: str = "unknown"
    unit: str = "F"  # "F" or "C" - the unit the market uses

    # Parser quality flags
    rules_clear: bool = False
    parse_score: float = 0.0  # 0..1 quality of parsing


# ---------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------

@dataclass
class WeatherForecast:
    city: str
    lat: float
    lon: float
    tz: str
    fetched_at: datetime
    # All temperatures in CELSIUS internally
    hourly_times: List[datetime] = field(default_factory=list)
    hourly_temps_c: List[float] = field(default_factory=list)
    daily_high_c: Optional[float] = None
    daily_low_c: Optional[float] = None
    forecast_window_high_c: Optional[float] = None  # high during the market's resolve window
    forecast_window_low_c: Optional[float] = None
    raw_provider: str = "open_meteo"


# ---------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------

@dataclass
class OrderbookSnapshot:
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: float
    ask_size: float
    spread: Optional[float]  # absolute (ask - bid)
    spread_percent: Optional[float]  # (ask - bid) / mid * 100
    mid: Optional[float]
    liquidity_usd: float
    fetched_at: datetime


# ---------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------

@dataclass
class TradeDecision:
    market_id: str
    title: str
    city: Optional[str]
    target_date: Optional[datetime]
    market_kind: str
    threshold_c: Optional[float]
    threshold_f: Optional[float]
    outcome: str  # "YES" or "NO" or "SKIP"
    forecast_value_c: Optional[float]
    temp_distance_c: Optional[float]
    bot_probability: Optional[float]
    confidence_score: Optional[float]
    market_price: Optional[float]
    spread_percent: Optional[float]
    liquidity_usd: Optional[float]
    decision: str  # "ENTER", "WATCH", "PASSIVE", "SKIP"
    skip_reason: Optional[str]
    timestamp: datetime
    hours_to_resolution: Optional[float] = None
    proposed_price: Optional[float] = None  # the price we will (or did) try
    proposed_size_usd: Optional[float] = None
    token_id: Optional[str] = None
    # True only when the operator opted in (ALLOW_LONGSHOT=true) AND the
    # decision engine routed the trade as a longshot. Lets the post-decision
    # risk gate know to relax the standard 0.95-0.985 entry band.
    is_longshot: bool = False
