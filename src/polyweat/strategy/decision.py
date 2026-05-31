"""Decision engine.

Combines parsing + prediction + confidence + orderbook + risk into a
single ``TradeDecision``.

Decision values:
    ENTER    - all gates pass, place a buy order
    PASSIVE  - gates pass but ask is above MAX_ENTRY_PRICE; place passive
               limit buy in the [PASSIVE_ORDER_MIN_PRICE,
               PASSIVE_ORDER_MAX_PRICE] band
    WATCH    - almost passes; keep on watchlist for the next scan
    SKIP     - hard reject (logged with reason)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from polyweat.config import Config
from polyweat.logger import get_logger
from polyweat.models import OrderbookSnapshot, ParsedMarket, TradeDecision, WeatherForecast
from polyweat.strategy.confidence import confidence_score
from polyweat.strategy.predictor import Prediction, predict

log = get_logger("decision")


# 1 °C = 1.8 °F. Used to gate distance in both units when the market is
# stated in F.
_C_PER_F = 5.0 / 9.0
_F_PER_C = 9.0 / 5.0


def _hours_until(end: Optional[datetime]) -> Optional[float]:
    if end is None:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = end - datetime.now(timezone.utc)
    return delta.total_seconds() / 3600.0


def make_decision(
    pm: ParsedMarket,
    fc: Optional[WeatherForecast],
    book_yes: Optional[OrderbookSnapshot],
    book_no: Optional[OrderbookSnapshot],
    cfg: Config,
    *,
    has_open_position: bool,
    open_positions_count: int,
    daily_loss_so_far_usd: float,
    open_passive_count: int = 0,
) -> TradeDecision:
    """Return a TradeDecision for one market.

    ``open_passive_count`` is included so that a stack of passive limit
    orders can't sneak past the MAX_OPEN_POSITIONS cap if they all fill.
    """
    now = datetime.now(timezone.utc)
    hours_to_res = _hours_until(pm.end_time)

    base = TradeDecision(
        market_id=pm.market_id,
        title=pm.title,
        city=pm.city,
        target_date=pm.target_date,
        market_kind=pm.market_kind,
        threshold_c=pm.threshold_c,
        threshold_f=pm.threshold_f,
        outcome="SKIP",
        forecast_value_c=None,
        temp_distance_c=None,
        bot_probability=None,
        confidence_score=None,
        market_price=None,
        spread_percent=None,
        liquidity_usd=None,
        decision="SKIP",
        skip_reason=None,
        hours_to_resolution=hours_to_res,
        timestamp=now,
        token_id=None,
        proposed_price=None,
        proposed_size_usd=None,
    )

    # ---------- hard gates: parsing ----------
    if pm.market_kind == "unknown":
        base.skip_reason = "market_kind_unknown"
        return base
    if pm.market_kind == "range":
        # Range markets are recognised but always SKIPPED - they have two
        # bounds and our predictor only knows how to compare against one.
        base.skip_reason = "range_market_skipped"
        return base
    if pm.market_kind == "exact_temp" and not cfg.allow_exact_temp_markets:
        base.skip_reason = "exact_temp_disabled"
        return base
    if not pm.rules_clear and not cfg.allow_ambiguous_markets:
        base.skip_reason = "rules_ambiguous"
        return base
    if not pm.city or pm.city_lat is None:
        base.skip_reason = "city_unparsed"
        return base
    if pm.target_date is None:
        base.skip_reason = "date_unparsed"
        return base
    if pm.threshold_c is None:
        base.skip_reason = "threshold_unparsed"
        return base

    # ---------- hard gates: time horizon ----------
    if hours_to_res is None or hours_to_res <= 0:
        base.skip_reason = "market_already_resolved_or_no_end_time"
        return base
    if hours_to_res > cfg.max_hours_to_resolution:
        base.skip_reason = f"hours_to_resolution_too_far_{hours_to_res:.1f}"
        return base

    # ---------- forecast ----------
    if fc is None:
        base.skip_reason = "forecast_unavailable"
        return base

    # ---------- predictor ----------
    pred: Prediction = predict(pm, fc)
    if pred.outcome == "SKIP":
        base.skip_reason = pred.skip_reason or "predictor_skip"
        base.forecast_value_c = pred.forecast_value_c
        base.temp_distance_c = pred.temp_distance_c
        base.bot_probability = pred.bot_probability
        return base

    base.outcome = pred.outcome
    base.forecast_value_c = pred.forecast_value_c
    base.temp_distance_c = pred.temp_distance_c
    base.bot_probability = pred.bot_probability

    # ---------- distance gate (BOTH units when applicable) ----------
    if pred.temp_distance_c is None or pred.temp_distance_c < cfg.min_temp_distance_c:
        base.skip_reason = (
            f"forecast_too_close_to_threshold_C_{pred.temp_distance_c}"
        )
        return base

    distance_f = pred.temp_distance_c * _F_PER_C
    if pm.unit == "F" and distance_f < cfg.min_temp_distance_f:
        base.skip_reason = (
            f"forecast_too_close_to_threshold_F_{distance_f:.2f}"
        )
        return base

    # ---------- probability gate ----------
    if pred.bot_probability is None or pred.bot_probability < cfg.min_bot_probability:
        base.skip_reason = f"low_bot_probability_{pred.bot_probability}"
        return base

    # ---------- confidence ----------
    conf = confidence_score(
        pm, fc,
        forecast_value_c=pred.forecast_value_c,
        distance_c=pred.temp_distance_c,
        hours_to_resolution=hours_to_res,
        cfg=cfg,
    )
    base.confidence_score = conf
    if conf < cfg.min_confidence_score:
        base.skip_reason = f"low_confidence_{conf}"
        return base

    # ---------- orderbook ----------
    book = book_yes if base.outcome == "YES" else book_no
    token_id = pm.yes_token_id if base.outcome == "YES" else pm.no_token_id
    base.token_id = token_id

    if not token_id:
        base.skip_reason = "no_token_id_for_outcome"
        return base
    if book is None or book.best_ask is None:
        base.skip_reason = "no_orderbook"
        return base

    base.market_price = book.best_ask
    base.spread_percent = book.spread_percent
    base.liquidity_usd = book.liquidity_usd

    if book.spread_percent is None or book.spread_percent > cfg.max_spread_percent:
        base.skip_reason = f"spread_too_wide_{book.spread_percent}"
        return base

    if book.liquidity_usd < cfg.min_liquidity_usd:
        base.skip_reason = f"liquidity_too_low_{book.liquidity_usd:.0f}"
        return base

    # ---------- risk gates ----------
    if has_open_position:
        base.skip_reason = "already_have_position_in_market"
        return base

    # Count both filled positions AND in-flight passive orders against the
    # cap, because a passive can fill any time and become a position.
    in_flight = open_positions_count + open_passive_count
    if in_flight >= cfg.max_open_positions:
        base.skip_reason = (
            f"max_open_positions_reached_{open_positions_count}+passive{open_passive_count}"
        )
        return base
    if daily_loss_so_far_usd >= cfg.max_daily_loss_usd:
        base.skip_reason = f"daily_loss_limit_hit_{daily_loss_so_far_usd:.2f}"
        return base

    # ---------- price band ----------
    ask = float(book.best_ask)

    # Below MIN_ENTRY_PRICE means the market disagrees with us strongly -
    # this is a long-shot bet; default-disabled.
    if ask < cfg.min_entry_price:
        if cfg.allow_longshot:
            # Operator opted in: enter (still capped by all earlier gates).
            base.decision = "ENTER"
            base.proposed_price = ask
            base.proposed_size_usd = cfg.max_position_per_market_usd
            return base
        base.skip_reason = f"price_below_min_entry_{ask:.4f}"
        return base

    if ask <= cfg.max_entry_price:
        # Direct ENTER - place a marketable buy at the ask.
        base.decision = "ENTER"
        base.proposed_price = ask
        base.proposed_size_usd = cfg.max_position_per_market_usd
        return base

    # Ask too high (> max_entry_price). If passive limits allowed and the
    # ask is within reach of the passive band, queue a passive limit instead.
    if cfg.allow_passive_limit_orders and (ask - cfg.passive_order_max_price) <= 0.03:
        # Place passive limit at the lower end of the passive band.
        passive_price = round(min(
            max(cfg.passive_order_min_price, ask - 0.01),
            cfg.passive_order_max_price,
        ), 4)
        base.decision = "PASSIVE"
        base.proposed_price = passive_price
        base.proposed_size_usd = cfg.max_position_per_market_usd
        return base

    # Otherwise watch it for the next scan.
    base.decision = "WATCH"
    base.skip_reason = f"price_above_max_entry_{ask:.4f}"
    return base
