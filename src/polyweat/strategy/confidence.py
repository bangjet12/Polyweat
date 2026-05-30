"""Confidence scorer.

Confidence is *not* the same as probability. Probability says "how likely
is the outcome to occur". Confidence says "how much do we trust our own
estimate" - a function of data quality, time horizon, and forecast
stability.

Both must clear their own thresholds before the bot enters a trade.
"""

from __future__ import annotations

import statistics
from typing import Optional

from polyweat.config import Config
from polyweat.models import ParsedMarket, WeatherForecast


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _hourly_std_today(fc: WeatherForecast) -> Optional[float]:
    """Return std-dev of the hourly samples that fall in the target window."""
    if not fc.hourly_temps_c:
        return None
    try:
        return statistics.pstdev(fc.hourly_temps_c)
    except statistics.StatisticsError:
        return None


def confidence_score(
    pm: ParsedMarket,
    fc: WeatherForecast,
    *,
    forecast_value_c: Optional[float],
    distance_c: Optional[float],
    hours_to_resolution: Optional[float],
    cfg: Config,
) -> float:
    """Return a confidence score in [0, 1]."""
    if forecast_value_c is None or distance_c is None:
        return 0.0

    # 1) Distance factor - the further forecast is from threshold, the more
    #    confident we are.
    if distance_c >= cfg.preferred_temp_distance_c:
        distance_factor = 1.0
    elif distance_c >= cfg.min_temp_distance_c:
        ratio = (distance_c - cfg.min_temp_distance_c) / max(
            1e-6,
            cfg.preferred_temp_distance_c - cfg.min_temp_distance_c,
        )
        distance_factor = 0.85 + 0.15 * _clip(ratio)
    else:
        # Below minimum -> low confidence (decision will skip anyway).
        distance_factor = 0.5 * (distance_c / max(1e-6, cfg.min_temp_distance_c))

    # 2) Time factor - close-to-resolution forecasts are more reliable.
    if hours_to_resolution is None:
        time_factor = 0.7
    elif hours_to_resolution <= cfg.best_hours_to_resolution:
        time_factor = 1.0
    elif hours_to_resolution <= cfg.preferred_hours_to_resolution:
        time_factor = 0.95
    elif hours_to_resolution <= cfg.max_hours_to_resolution:
        time_factor = 0.88
    else:
        time_factor = 0.7

    # 3) Parse factor - did we extract city/date/threshold cleanly?
    parse_factor = 0.6 + 0.4 * _clip(pm.parse_score)

    # 4) Forecast completeness
    completeness = 0.0
    if fc.forecast_window_high_c is not None:
        completeness += 0.5
    if fc.forecast_window_low_c is not None:
        completeness += 0.5
    completeness = 0.7 + 0.3 * completeness  # keep within 0.7..1.0

    # 5) Forecast stability - large hourly variability slightly reduces trust.
    std = _hourly_std_today(fc)
    if std is None:
        stability_factor = 0.95
    else:
        # std up to ~3C is normal for a day, anything above degrades trust.
        stability_factor = _clip(1.05 - (std / 12.0), 0.7, 1.0)

    score = (
        distance_factor
        * time_factor
        * parse_factor
        * completeness
        * stability_factor
    )
    return round(_clip(score), 5)
