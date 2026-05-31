"""Pre-trade risk gates that complement the decision engine.

These checks are applied a second time *just before* an order is sent,
because state (open positions, daily PnL, ...) may have moved since
``decision.make_decision`` ran.
"""

from __future__ import annotations

from dataclasses import dataclass

from polyweat.config import Config
from polyweat.db import Database
from polyweat.logger import get_logger
from polyweat.models import TradeDecision

log = get_logger("risk")


@dataclass
class RiskCheck:
    ok: bool
    reason: str = ""


def pre_trade_checks(td: TradeDecision, db: Database, cfg: Config) -> RiskCheck:
    """Final go/no-go gate before the trader places an order."""
    if td.decision not in ("ENTER", "PASSIVE"):
        return RiskCheck(False, f"decision_not_actionable_{td.decision}")

    if td.token_id is None or td.proposed_price is None:
        return RiskCheck(False, "missing_token_or_price")

    if td.proposed_size_usd is None or td.proposed_size_usd <= 0:
        return RiskCheck(False, "invalid_size")

    # Always reject duplicates (the cap above doesn't catch a same-market re-entry).
    if db.has_open_position(td.market_id):
        return RiskCheck(False, "duplicate_market_position")

    # Re-check size cap (defense-in-depth).
    if td.proposed_size_usd > cfg.max_position_per_market_usd:
        return RiskCheck(
            False,
            f"size_above_cap_{td.proposed_size_usd}>{cfg.max_position_per_market_usd}",
        )

    # Re-check open position count INCLUDING in-flight passive orders.
    in_flight = db.count_open_positions() + db.count_open_passive_orders()
    if in_flight >= cfg.max_open_positions:
        return RiskCheck(False, f"max_open_positions_reached_{in_flight}")

    # Re-check daily loss cap.
    if db.daily_loss_today() >= cfg.max_daily_loss_usd:
        return RiskCheck(False, "daily_loss_limit_hit")

    # Price band re-check.
    p = float(td.proposed_price)
    if td.decision == "ENTER":
        if td.is_longshot:
            # Long-shot opt-in (ALLOW_LONGSHOT=true): require 0 < p < min_entry.
            if p <= 0 or p >= cfg.min_entry_price:
                return RiskCheck(False, f"longshot_price_out_of_band_{p}")
        elif p < cfg.min_entry_price or p > cfg.max_entry_price:
            return RiskCheck(False, f"enter_price_out_of_band_{p}")
    elif td.decision == "PASSIVE":
        if p < cfg.passive_order_min_price or p > cfg.passive_order_max_price:
            return RiskCheck(False, f"passive_price_out_of_band_{p}")

    # Defense-in-depth: refuse if there is already an open ``orders`` row
    # for this market - that means a previous live submit is still
    # in-flight (or got stuck). Without this, a process restart in the
    # middle of a live submit could let a duplicate order slip through.
    if db.has_inflight_order(td.market_id):
        return RiskCheck(False, "inflight_order_exists")

    return RiskCheck(True)
