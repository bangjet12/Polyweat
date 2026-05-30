"""Temperature predictor.

Given a parsed weather market and a fresh forecast, decide which outcome
(YES or NO) the forecast supports, how far the forecast sits from the
threshold, and a calibrated bot probability for that outcome.

Probability model
-----------------
We use a smooth, monotonic model parameterized by the distance ``d``
(in degrees Celsius) between the forecast and the threshold::

    p = 1 - 0.5 * exp(-d / SCALE_C)

With ``SCALE_C = 1.0`` we get:

    d=0  -> 0.500   (no edge, skip)
    d=1  -> 0.816
    d=2  -> 0.932   (just at MIN_BOT_PROBABILITY=0.93)
    d=3  -> 0.975
    d=5  -> 0.997

The model is intentionally conservative: it never returns >0.999 because
forecasts are imperfect and we want the bot to refuse over-confidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from polyweat.logger import get_logger
from polyweat.models import ParsedMarket, WeatherForecast

log = get_logger("predictor")

# Scale for the probability curve, in degrees C. Smaller -> more confident.
SCALE_C = 1.0
# Hard cap so we never report perfect certainty.
MAX_PROB = 0.999


@dataclass
class Prediction:
    outcome: str               # "YES" | "NO" | "SKIP"
    forecast_value_c: Optional[float]  # the forecast value compared
    threshold_c: Optional[float]
    temp_distance_c: Optional[float]   # always >= 0
    bot_probability: Optional[float]   # for the chosen outcome
    skip_reason: Optional[str] = None


def _prob_from_distance(distance_c: float) -> float:
    if distance_c <= 0:
        return 0.5
    p = 1.0 - 0.5 * math.exp(-distance_c / SCALE_C)
    return min(MAX_PROB, max(0.5, p))


def predict(pm: ParsedMarket, fc: WeatherForecast) -> Prediction:
    """Run the predictor for a single market + forecast pair."""
    if pm.threshold_c is None:
        return Prediction("SKIP", None, None, None, None,
                          skip_reason="threshold_unparsed")

    if pm.market_kind in ("unknown",):
        return Prediction("SKIP", None, pm.threshold_c, None, None,
                          skip_reason="market_kind_unknown")

    if pm.market_kind == "exact_temp":
        # Default skip - too risky.
        return Prediction("SKIP", None, pm.threshold_c, None, None,
                          skip_reason="exact_temp_market")

    # Pick the relevant forecast value based on market kind.
    high = fc.forecast_window_high_c
    low = fc.forecast_window_low_c

    if high is None and low is None:
        return Prediction("SKIP", None, pm.threshold_c, None, None,
                          skip_reason="forecast_incomplete")

    threshold = float(pm.threshold_c)
    forecast_value: Optional[float] = None
    distance: Optional[float] = None
    outcome: str = "SKIP"

    if pm.market_kind == "highest_gte":
        # YES if max >= threshold
        forecast_value = high
        if forecast_value is None:
            return Prediction("SKIP", None, threshold, None, None,
                              skip_reason="forecast_high_missing")
        if forecast_value >= threshold:
            outcome = "YES"
            distance = forecast_value - threshold
        else:
            outcome = "NO"
            distance = threshold - forecast_value

    elif pm.market_kind == "highest_lt":
        # YES if max < threshold
        forecast_value = high
        if forecast_value is None:
            return Prediction("SKIP", None, threshold, None, None,
                              skip_reason="forecast_high_missing")
        if forecast_value < threshold:
            outcome = "YES"
            distance = threshold - forecast_value
        else:
            outcome = "NO"
            distance = forecast_value - threshold

    elif pm.market_kind == "lowest_lte":
        # YES if min <= threshold
        forecast_value = low
        if forecast_value is None:
            return Prediction("SKIP", None, threshold, None, None,
                              skip_reason="forecast_low_missing")
        if forecast_value <= threshold:
            outcome = "YES"
            distance = threshold - forecast_value
        else:
            outcome = "NO"
            distance = forecast_value - threshold

    elif pm.market_kind == "lowest_gt":
        # YES if min > threshold
        forecast_value = low
        if forecast_value is None:
            return Prediction("SKIP", None, threshold, None, None,
                              skip_reason="forecast_low_missing")
        if forecast_value > threshold:
            outcome = "YES"
            distance = forecast_value - threshold
        else:
            outcome = "NO"
            distance = threshold - forecast_value

    else:
        return Prediction("SKIP", None, threshold, None, None,
                          skip_reason=f"unsupported_kind:{pm.market_kind}")

    if distance is None or forecast_value is None:
        return Prediction("SKIP", forecast_value, threshold, None, None,
                          skip_reason="distance_unavailable")

    distance = abs(distance)
    prob = _prob_from_distance(distance)
    return Prediction(
        outcome=outcome,
        forecast_value_c=round(forecast_value, 3),
        threshold_c=round(threshold, 3),
        temp_distance_c=round(distance, 3),
        bot_probability=round(prob, 5),
    )
