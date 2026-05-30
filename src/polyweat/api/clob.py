"""Polymarket CLOB API client.

We use the public read-only endpoint to fetch the orderbook for a given
token_id. Live order placement (when LIVE_TRADING=true) is delegated to
``py-clob-client`` if it is installed; otherwise live mode degrades to
a clear error so we never accidentally trade without the proper signer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from polyweat.api._http import get_json
from polyweat.logger import get_logger
from polyweat.models import OrderbookSnapshot

log = get_logger("clob")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ClobClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_orderbook(self, token_id: str) -> Optional[OrderbookSnapshot]:
        """Fetch the L2 orderbook for ``token_id`` and return a snapshot."""
        if not token_id:
            return None
        try:
            data = get_json(
                f"{self.base_url}/book",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
        except Exception as exc:
            log.warning("CLOB book fetch failed for %s: %s", token_id, exc)
            return None

        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []

        # The CLOB returns bids ascending and asks ascending; best bid is the
        # last element in `bids`, best ask is the first element in `asks`.
        # Be defensive: sort explicitly.
        bids = sorted(
            (
                (_f(b.get("price")), _f(b.get("size")))
                for b in bids_raw
                if _f(b.get("price")) is not None
            ),
            key=lambda x: x[0],  # type: ignore[arg-type]
        )
        asks = sorted(
            (
                (_f(a.get("price")), _f(a.get("size")))
                for a in asks_raw
                if _f(a.get("price")) is not None
            ),
            key=lambda x: x[0],  # type: ignore[arg-type]
        )

        best_bid = bids[-1][0] if bids else None
        bid_size = bids[-1][1] or 0.0 if bids else 0.0
        best_ask = asks[0][0] if asks else None
        ask_size = asks[0][1] or 0.0 if asks else 0.0

        spread: Optional[float] = None
        spread_pct: Optional[float] = None
        mid: Optional[float] = None
        if best_bid is not None and best_ask is not None and best_ask > 0:
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
            if mid > 0:
                spread_pct = (spread / mid) * 100.0

        # Liquidity USD = sum(price * size) on both sides.
        liq = 0.0
        for p, s in bids + asks:
            if p is not None and s is not None:
                liq += float(p) * float(s)

        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            spread=spread,
            spread_percent=spread_pct,
            mid=mid,
            liquidity_usd=liq,
            fetched_at=datetime.now(timezone.utc),
        )
