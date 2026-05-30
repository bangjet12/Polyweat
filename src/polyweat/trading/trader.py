"""Trader - executes orders.

Two backends:
  * ``DryRunTrader`` (default): simulates fills, writes orders/positions
    to the local SQLite, never touches the network for trading.
  * ``LiveTrader``: only activated when ``DRY_RUN=false`` AND
    ``LIVE_TRADING=true``. Uses ``py-clob-client`` if installed; if not
    installed it raises a clear error so we never silently misbehave.

The runner uses :func:`build_trader` to pick the right backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

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
        """Mark any expired passive order as 'expired'. (No real fills in dry mode.)"""
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


# ---------------------------------------------------------------------
# Live trader
# ---------------------------------------------------------------------

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
            and cfg.polymarket_proxy_address
            and cfg.polymarket_api_key
            and cfg.polymarket_api_secret
            and cfg.polymarket_api_passphrase
        ):
            raise TraderError(
                "Live trading requires all Polymarket creds in .env: "
                "POLYMARKET_PRIVATE_KEY, POLYMARKET_PROXY_ADDRESS, "
                "POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                "POLYMARKET_API_PASSPHRASE"
            )

        # py-clob-client constructor signature:
        #   ClobClient(host, key, chain_id, signature_type=2,
        #              funder=proxy_address, creds=ApiCreds(...))
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
        self._client = _Clob(
            host=cfg.clob_api_base,
            key=cfg.polymarket_private_key,
            chain_id=POLYGON,
            signature_type=2,
            funder=cfg.polymarket_proxy_address,
            creds=creds,
        )
        log.warning("LiveTrader initialized - real orders will be placed.")

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

    # ----- API -----

    def _enter(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        price = float(td.proposed_price)
        size_usd = float(td.proposed_size_usd)
        size_shares = round(size_usd / max(price, 1e-9), 4)

        ext_id = self._place_limit_buy(td.token_id or "", price, size_shares)
        status = "submitted" if ext_id else "rejected"
        order_pk = self.db.insert_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            side="BUY",
            order_type="LIMIT",
            price=price,
            size_usd=size_usd,
            size_shares=size_shares,
            status=status,
            dry_run=False,
            external_order_id=ext_id,
            note="LIVE marketable limit",
        )
        if ext_id:
            # Optimistic position; reconciliation can adjust later.
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
            log.info(
                "[LIVE] ENTER %s %s @ %.4f size=$%.2f order=%s",
                td.outcome, td.market_id, price, size_usd, ext_id,
            )
        return order_pk

    def _passive(self, td: TradeDecision) -> Optional[int]:
        assert td.proposed_price is not None and td.proposed_size_usd is not None
        price = float(td.proposed_price)
        size_usd = float(td.proposed_size_usd)
        size_shares = round(size_usd / max(price, 1e-9), 4)

        ext_id = self._place_limit_buy(td.token_id or "", price, size_shares)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.cfg.passive_order_expire_seconds
        )
        po_pk = self.db.insert_passive_order(
            market_id=td.market_id,
            token_id=td.token_id,
            outcome=td.outcome,
            price=price,
            size_usd=size_usd,
            expires_at=expires_at,
            external_order_id=ext_id,
            note="LIVE passive limit",
        )
        if ext_id:
            log.info(
                "[LIVE] PASSIVE %s %s @ %.4f expires_in=%ds order=%s",
                td.outcome, td.market_id, price,
                self.cfg.passive_order_expire_seconds, ext_id,
            )
        else:
            self.db.update_passive_order_status(po_pk, "rejected", "LIVE order rejected")
        return po_pk

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
            if expires_at > now:
                continue
            ext_id = row["external_order_id"]
            cancelled = False
            if ext_id:
                try:
                    self._client.cancel(ext_id)
                    cancelled = True
                except Exception as exc:
                    log.warning("cancel(%s) failed: %s", ext_id, exc)
            self.db.update_passive_order_status(
                int(row["id"]),
                "cancelled" if cancelled else "expired",
                "passive expired" if not cancelled else "passive cancelled",
            )
            log.info(
                "[LIVE] passive order %s -> %s",
                row["id"], "cancelled" if cancelled else "expired",
            )


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
