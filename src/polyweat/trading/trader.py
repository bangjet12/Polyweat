"""Trader - executes orders.

Two backends:
  * ``DryRunTrader`` (default): simulates fills, writes orders/positions
    to the local SQLite, never touches the network for trading.
  * ``LiveTrader``: only activated when ``DRY_RUN=false`` AND
    ``LIVE_TRADING=true``. Uses ``py-clob-client`` if installed; if not
    installed it raises a clear error so we never silently misbehave.

The runner uses :func:`build_trader` to pick the right backend.

Crash safety
------------
``LiveTrader._enter`` writes a ``status='pending'`` row to the local
``orders`` table *before* hitting the network, so that a process death
between submission and persistence cannot leave a real Polymarket order
without any local trace. After the exchange replies we update the row
to ``submitted`` (or ``rejected``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from polyweat.config import Config
from polyweat.db import Database
from polyweat.logger import get_logger
from polyweat.models import TradeDecision
from polyweat.trading.risk import pre_trade_checks

log = get_logger("trader")


class TraderError(Exception):
    pass


class BaseTrader:
    """Shared logic for both dry-run and live traders."""

    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg

    # ----- public API -----

    def execute(self, td: TradeDecision) -> Optional[int]:
        """Run pre-trade checks and dispatch to the right action."""
        check = pre_trade_checks(td, self.db, self.cfg)
        if not check.ok:
            log.info(
                "[%s] pre-trade check failed: %s (%s)",
                td.market_id, check.reason, td.title[:60],
            )
            return None

        if td.decision == "ENTER":
            return self._enter(td)
        if td.decision == "PASSIVE":
            return self._passive(td)
        return None

    # ----- to override in subclasses -----

    def _enter(self, td: TradeDecision) -> Optional[int]:
        raise NotImplementedError

    def _passive(self, td: TradeDecision) -> Optional[int]:
        raise NotImplementedError

    def reconcile_passive_orders(self) -> None:
        """Sweep open passive orders: expire & cancel as needed."""
        raise NotImplementedError


# ---------------------------------------------------------------------
# Dry-run trader
# ---------------------------------------------------------------------

class DryRunTrader(BaseTrader):
    """Simulates orders. Marks ENTER orders as immediately filled at the
    proposed price (which equals the ask on a marketable buy)."""

    def _enter(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        price = float(td.proposed_price)
        size_usd = float(td.proposed_size_usd)
        size_shares = size_usd / max(price, 1e-9)

        order_id = self.db.insert_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            side="BUY",
            order_type="LIMIT",
            price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            status="simulated",
            dry_run=True,
            external_order_id=None,
            note="DRY_RUN simulated immediate fill",
        )

        self.db.upsert_position(
            market_id=td.market_id,
            token_id=td.token_id,
            title=td.title,
            outcome=td.outcome,
            entry_price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            status="open",
        )
        self.db.bump_entries_count()
        log.info(
            "[DRY] ENTER %s %s @ %.4f size=$%.2f (%s)",
            td.outcome, td.market_id, price, size_usd, td.title[:60],
        )
        return order_id

    def _passive(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.cfg.passive_order_expire_seconds
        )
        po_id = self.db.insert_passive_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            price=float(td.proposed_price),
            size_usd=float(td.proposed_size_usd),
            expires_at=expires_at,
            external_order_id=None,
            note="DRY_RUN passive limit",
        )
        log.info(
            "[DRY] PASSIVE %s %s @ %.4f expires_in=%ds",
            td.outcome, td.market_id, td.proposed_price,
            self.cfg.passive_order_expire_seconds,
        )
        return po_id

    def reconcile_passive_orders(self) -> None:
        """Cancel passive orders whose timer has elapsed."""
        now = datetime.now(timezone.utc)
        for row in self.db.list_open_passive_orders():
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
            except Exception:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                self.db.update_passive_order_status(
                    int(row["id"]), "expired", "DRY_RUN expiration"
                )
                log.info("[DRY] passive order %s expired", row["id"])

    def reconcile_inflight_order(self, row) -> None:
        """No-op for dry-run trader (no real exchange to query)."""
        # In DRY_RUN there should be no rows where dry_run=0 anyway, but if
        # someone switches modes mid-DB, just close them with a clear note.
        self.db.update_order_status(
            int(row["id"]),
            status="cancelled",
            note="dry-run startup: cancelling stale live order row",
        )


# ---------------------------------------------------------------------
# Live trader
# ---------------------------------------------------------------------

# Live order fill statuses we care about (exchange-side strings).
_FILLED_STATUSES = {"FILLED", "MATCHED", "PARTIALLY_FILLED", "PARTIAL"}
_OPEN_STATUSES = {"OPEN", "LIVE", "PENDING", "ACTIVE"}
_CANCELLED_STATUSES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


class LiveTrader(BaseTrader):
    """Live trader using py-clob-client. Only activated when both
    ``DRY_RUN=false`` AND ``LIVE_TRADING=true``.
    """

    def __init__(self, db: Database, cfg: Config):
        super().__init__(db, cfg)
        try:
            from py_clob_client.client import ClobClient as _Clob  # type: ignore
            from py_clob_client.constants import POLYGON  # type: ignore
            from py_clob_client.clob_types import OrderArgs  # type: ignore
        except ImportError as exc:
            raise TraderError(
                "py-clob-client is not installed. Run "
                "`pip install py-clob-client` to enable live trading."
            ) from exc

        if not (
            cfg.polymarket_private_key
            and cfg.polymarket_api_key
            and cfg.polymarket_api_secret
            and cfg.polymarket_api_passphrase
        ):
            raise TraderError(
                "Live trading requires Polymarket creds in .env: "
                "POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, "
                "POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE "
                "(POLYMARKET_PROXY_ADDRESS required when "
                "POLYMARKET_SIGNATURE_TYPE=2)"
            )
        if cfg.polymarket_signature_type == 2 and not cfg.polymarket_proxy_address:
            raise TraderError(
                "POLYMARKET_SIGNATURE_TYPE=2 (proxy/Magic) requires "
                "POLYMARKET_PROXY_ADDRESS in .env."
            )

        self._POLYGON = POLYGON
        self._OrderArgs = OrderArgs
        try:
            from py_clob_client.clob_types import ApiCreds  # type: ignore
        except ImportError:  # pragma: no cover
            ApiCreds = None  # type: ignore
        creds = ApiCreds(
            api_key=cfg.polymarket_api_key,
            api_secret=cfg.polymarket_api_secret,
            api_passphrase=cfg.polymarket_api_passphrase,
        ) if ApiCreds else None

        client_kwargs: Dict[str, Any] = dict(
            host=cfg.clob_api_base,
            key=cfg.polymarket_private_key,
            chain_id=POLYGON,
            signature_type=cfg.polymarket_signature_type,
            creds=creds,
        )
        if cfg.polymarket_signature_type == 2 and cfg.polymarket_proxy_address:
            client_kwargs["funder"] = cfg.polymarket_proxy_address
        self._client = _Clob(**client_kwargs)
        log.warning(
            "LiveTrader initialized (signature_type=%d) - REAL orders will be placed.",
            cfg.polymarket_signature_type,
        )

    # ----- helpers -----

    def _place_limit_buy(
        self, token_id: str, price: float, size_shares: float
    ) -> Optional[str]:
        try:
            args = self._OrderArgs(
                token_id=token_id,
                price=round(float(price), 4),
                size=round(float(size_shares), 4),
                side="BUY",
            )
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed)
            order_id = (resp or {}).get("orderID") or (resp or {}).get("id")
            if order_id:
                return str(order_id)
            log.error("Live order accepted with no id: %r", resp)
            return None
        except Exception as exc:
            log.exception("Live order failed: %s", exc)
            return None

    def _exchange_order_status(self, ext_id: str) -> str:
        """Return the exchange-side normalized status of an order, or ''."""
        if not ext_id:
            return ""
        try:
            data = self._client.get_order(ext_id)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("get_order(%s) failed: %s", ext_id, exc)
            return ""
        status = ""
        if isinstance(data, dict):
            status = str(
                data.get("status")
                or data.get("orderStatus")
                or data.get("state")
                or ""
            ).upper()
        return status

    def _exchange_filled_size(
        self, ext_id: str
    ) -> "tuple[Optional[float], Optional[float]]":
        """Return (filled_size_usd, filled_avg_price) or (None, None)."""
        if not ext_id:
            return (None, None)
        try:
            data = self._client.get_order(ext_id)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("get_order(%s) for fill size failed: %s", ext_id, exc)
            return (None, None)
        if not isinstance(data, dict):
            return (None, None)

        # Polymarket fills are reported in a few possible field shapes; we
        # try the common ones in order. All values normalised to USD notional.
        size_shares: Optional[float] = None
        for k in ("size_matched", "filled_size", "filled_amount",
                  "matchedAmount", "filled"):
            v = data.get(k)
            if v is None:
                continue
            try:
                size_shares = float(v)
                break
            except (TypeError, ValueError):
                continue

        avg_price: Optional[float] = None
        for k in ("avg_price", "average_price", "price", "matchedPrice"):
            v = data.get(k)
            if v is None:
                continue
            try:
                avg_price = float(v)
                break
            except (TypeError, ValueError):
                continue

        if size_shares is None or avg_price is None or avg_price <= 0:
            return (None, None)
        return (size_shares * avg_price, avg_price)

    def _cancel(self, ext_id: str) -> bool:
        if not ext_id:
            return False
        try:
            self._client.cancel(ext_id)
            return True
        except Exception as exc:
            log.warning("cancel(%s) failed: %s", ext_id, exc)
            return False

    # ----- API -----

    def _enter(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        price = float(td.proposed_price)
        size_usd = float(td.proposed_size_usd)
        size_shares = round(size_usd / max(price, 1e-9), 4)

        # CRASH-SAFE: write a 'pending' row to the local DB BEFORE we
        # actually hit the exchange. If we die between the post and the
        # follow-up update, this row is the breadcrumb that prevents a
        # ghost position.
        order_pk = self.db.insert_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            side="BUY",
            order_type="LIMIT",
            price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            status="pending",
            dry_run=False,
            external_order_id=None,
            note="LIVE pending - awaiting exchange ack",
        )

        ext_id = self._place_limit_buy(td.token_id or "", price, size_shares)
        if ext_id:
            self.db.update_order_status(
                order_pk, status="submitted",
                external_order_id=ext_id,
                note="LIVE submitted",
            )
            self.db.upsert_position(
                market_id=td.market_id,
                token_id=td.token_id,
                title=td.title,
                outcome=td.outcome,
                entry_price=price,
                size_usd=size_usd,
                size_shares=size_shares,
                status="open",
            )
            self.db.bump_entries_count()
            log.info(
                "[LIVE] ENTER %s %s @ %.4f size=$%.2f order=%s",
                td.outcome, td.market_id, price, size_usd, ext_id,
            )
        else:
            self.db.update_order_status(
                order_pk, status="rejected",
                note="LIVE order rejected by exchange",
            )
            log.warning("[LIVE] order rejected for %s", td.market_id)
        return order_pk

    def _passive(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        price = float(td.proposed_price)
        size_usd = float(td.proposed_size_usd)
        size_shares = round(size_usd / max(price, 1e-9), 4)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.cfg.passive_order_expire_seconds
        )

        # Write the passive order BEFORE touching the network so a crash
        # leaves a breadcrumb. We'll reconcile fill status next cycle.
        po_pk = self.db.insert_passive_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            price=price,
            size_usd=size_usd,
            expires_at=expires_at,
            external_order_id=None,
            note="LIVE passive pending",
        )

        ext_id = self._place_limit_buy(td.token_id or "", price, size_shares)
        if ext_id:
            # Persist the external id so reconciliation can find it. Use
            # the public Database API (no reaching into _conn).
            self.db.set_passive_order_external_id(
                po_pk, ext_id, "LIVE passive submitted",
            )
            self.db.bump_entries_count()
            log.info(
                "[LIVE] PASSIVE %s %s @ %.4f expires_in=%ds order=%s",
                td.outcome, td.market_id, price,
                self.cfg.passive_order_expire_seconds, ext_id,
            )
        else:
            self.db.update_passive_order_status(po_pk, "rejected", "LIVE order rejected")
        return po_pk

    def reconcile_passive_orders(self) -> None:
        """Sweep all open passive orders.

        For each open passive:
          1. Ask the exchange for the current status.
          2. If FILLED / partially filled -> mark filled and write a
             position row so caps and PnL are correct.
          3. If still OPEN and the timer has elapsed -> cancel.
          4. If cancelled / rejected on the exchange -> mark cancelled.
        """
        now = datetime.now(timezone.utc)
        for row in self.db.list_open_passive_orders():
            try:
                expires_at = datetime.fromisoformat(row["expires_at"])
            except Exception:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            ext_id = row["external_order_id"] or ""
            status = self._exchange_order_status(ext_id) if ext_id else ""

            # 1) Filled while we were sleeping?
            if status in _FILLED_STATUSES:
                # Try to read the actual filled size from the exchange. If
                # we can't, fall back to the local size_usd, but log it.
                filled_size_usd, filled_price = self._exchange_filled_size(ext_id)
                price = filled_price if filled_price else float(row["price"] or 0.0)
                size_usd = filled_size_usd if filled_size_usd is not None else float(row["size_usd"] or 0.0)
                shares = size_usd / max(price, 1e-9)
                self.db.upsert_position(
                    market_id=row["market_id"],
                    token_id=row["token_id"],
                    title=row["market_id"],
                    outcome=row["outcome"] or "YES",
                    entry_price=price,
                    size_usd=size_usd,
                    size_shares=shares,
                    status="open",
                )
                self.db.update_passive_order_status(
                    int(row["id"]),
                    "filled",
                    f"passive filled (reconciled, size=${size_usd:.4f})",
                )
                log.info(
                    "[LIVE] passive %s FILLED -> position created "
                    "(price=%.4f, size=$%.4f)",
                    row["id"], price, size_usd,
                )
                continue

            # 2) Cancelled / rejected on the exchange already?
            if status in _CANCELLED_STATUSES:
                self.db.update_passive_order_status(
                    int(row["id"]), "cancelled",
                    f"exchange status: {status}",
                )
                log.info("[LIVE] passive %s cancelled by exchange (%s)", row["id"], status)
                continue

            # 3) Still open and expired -> cancel.
            if expires_at <= now:
                cancelled = self._cancel(ext_id) if ext_id else False
                self.db.update_passive_order_status(
                    int(row["id"]),
                    "cancelled" if cancelled else "expired",
                    "passive cancelled" if cancelled else "passive expired",
                )
                log.info(
                    "[LIVE] passive %s -> %s",
                    row["id"], "cancelled" if cancelled else "expired",
                )

    # ----- position reconciliation -----

    def reconcile_positions_via_market(self, gamma) -> int:
        """Close any open positions whose Polymarket market has resolved.

        Returns the number of positions closed.

        We use the Gamma API because it returns the resolved outcome
        prices (1.0 / 0.0) once a market is settled. PnL formula:

            pnl = size_usd * (1 - entry_price)   if our outcome won
            pnl = -size_usd                       if our outcome lost
            pnl = size_usd * (mid - entry_price)  for partial / refund
        """
        return _reconcile_positions(self.db, gamma)

    def reconcile_inflight_order(self, row) -> None:
        """Recover a single ``orders`` row left in pending/submitted from
        a previous process. Called once on startup.

        Logic:
          * If the row has no ``external_order_id`` (we crashed before the
            ack), there is no way to know whether the exchange got the
            order. Mark it ``rejected`` and let the operator review the
            log. We do NOT create a position from a row without an id.
          * If we have an external id, ask the exchange. FILLED / MATCHED
            -> create a position. CANCELLED / REJECTED -> mark rejected.
            OPEN / PENDING -> leave it.
        """
        order_pk = int(row["id"])
        ext_id = row["external_order_id"] or ""
        if not ext_id:
            log.warning(
                "[LIVE] startup: order pk=%d on %s has no external id; "
                "marking rejected (no way to reconcile).",
                order_pk, row["market_id"],
            )
            self.db.update_order_status(
                order_pk, status="rejected",
                note="startup: dropped (no external_order_id; "
                     "exchange status unknown)",
            )
            return

        status = self._exchange_order_status(ext_id)
        if status in _FILLED_STATUSES:
            filled_size_usd, filled_price = self._exchange_filled_size(ext_id)
            price = filled_price or float(row["price"] or 0.0)
            size_usd = filled_size_usd if filled_size_usd is not None else float(row["size_usd"] or 0.0)
            shares = size_usd / max(price, 1e-9)
            self.db.upsert_position(
                market_id=row["market_id"],
                token_id=row["token_id"],
                title=row["market_id"],
                outcome=row["outcome"] or "YES",
                entry_price=price,
                size_usd=size_usd,
                size_shares=shares,
                status="open",
            )
            self.db.update_order_status(
                order_pk, status="filled",
                note=f"startup: reconciled FILLED size=${size_usd:.4f}",
            )
            log.info(
                "[LIVE] startup: order %s FILLED -> position created "
                "(price=%.4f size=$%.4f)",
                ext_id, price, size_usd,
            )
            return
        if status in _CANCELLED_STATUSES:
            self.db.update_order_status(
                order_pk, status="cancelled",
                note=f"startup: exchange status={status}",
            )
            log.info("[LIVE] startup: order %s -> cancelled (%s)", ext_id, status)
            return
        # OPEN / PENDING / unknown - leave it. The next scan will retry
        # via reconcile_passive_orders for passives, or stay pending.
        log.info(
            "[LIVE] startup: order %s still %s; leaving as %s",
            ext_id, status or "unknown", row["status"],
        )


def _reconcile_positions(db: Database, gamma) -> int:
    """Shared closer used by both live and dry-run traders.

    For each open position, check the Gamma market: if it's *fully resolved*
    we compute realized PnL and mark the position closed. Updates daily
    stats so MAX_DAILY_LOSS_USD actually engages.

    Resolution rules (deliberately conservative):
      * We require ``closed=True`` AND a clearly identified winner price
        (>= 0.99 on one side and <= 0.01 on the other). A market that is
        merely ``closed`` but whose prices are still in flight is left
        alone for next cycle.
      * If the market is closed for many days but never resolves cleanly
        (refund/void/dispute), we still leave it open so an operator can
        close it manually instead of silently writing $0 PnL.
      * Mismatched ``outcomes``/``prices`` arrays are treated as
        unresolved, not as a loss.

    Per-position errors are caught so one malformed market can't starve
    the rest of the queue.
    """
    closed = 0
    for row in db.list_open_positions():
        try:
            closed += _reconcile_one_position(db, gamma, row)
        except Exception as exc:  # noqa: BLE001 - log + continue
            log.exception(
                "[reconcile] %s: error during reconciliation: %s",
                row["market_id"], exc,
            )
            continue
    return closed


def _reconcile_one_position(db: Database, gamma, row) -> int:
    market_id = row["market_id"]
    market = gamma.fetch_market(market_id)
    if not market:
        return 0
    if not market.get("closed"):
        # Not even closed yet - give it more time.
        return 0

    nm = gamma.normalize(market)
    outcomes = nm.get("outcomes") or []
    prices = nm.get("prices") or []

    # Identify a winner (>=0.99) AND a loser (<=0.01) on the opposite side.
    # Only this combination indicates a clean settlement.
    if len(outcomes) != len(prices) or not outcomes:
        log.warning(
            "[reconcile] %s closed but outcomes/prices mismatch (%d vs %d) - "
            "leaving open for manual review",
            market_id, len(outcomes), len(prices),
        )
        return 0

    winner_idx = None
    loser_idx = None
    for i, p in enumerate(prices):
        try:
            if p is None:
                continue
            pf = float(p)
        except (TypeError, ValueError):
            continue
        if pf >= 0.99 and winner_idx is None:
            winner_idx = i
        elif pf <= 0.01 and loser_idx is None:
            loser_idx = i

    if winner_idx is None or loser_idx is None or winner_idx == loser_idx:
        log.info(
            "[reconcile] %s closed but no clean winner yet (prices=%s) - "
            "leaving open for next cycle",
            market_id, prices,
        )
        return 0

    my_outcome = (row["outcome"] or "").upper()
    size_usd = float(row["size_usd"] or 0.0)
    entry_price = float(row["entry_price"] or 0.0)
    winner = (outcomes[winner_idx] or "").strip().upper()

    if not winner:
        log.warning(
            "[reconcile] %s has empty winner outcome label - leaving open",
            market_id,
        )
        return 0

    if winner == my_outcome:
        # PnL = shares * 1.0 - cost = (size_usd / entry_price) - size_usd
        # which equals size_usd * (1 / entry - 1).
        shares = size_usd / max(entry_price, 1e-9)
        pnl = shares * 1.0 - size_usd  # gross before exchange fees
    else:
        pnl = -size_usd
    db.close_position(market_id, round(pnl, 4))
    log.info(
        "[reconcile] %s closed (%s won, our %s) pnl=%+.4f",
        market_id, winner, my_outcome, pnl,
    )
    return 1


# ---------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------

def build_trader(db: Database, cfg: Config) -> BaseTrader:
    """Pick the right trader based on config."""
    if cfg.is_live:
        log.warning(
            "*** LIVE TRADING ENABLED: real orders will be placed on Polymarket. ***"
        )
        return LiveTrader(db, cfg)
    log.info("Trader running in DRY_RUN mode (no real orders).")
    return DryRunTrader(db, cfg)


# Public API: position reconciliation helper used by the runner regardless
# of trader backend.
reconcile_positions = _reconcile_positions
