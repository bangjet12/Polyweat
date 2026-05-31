"""Polymarket Gamma API client - fetch active markets metadata.

The Gamma API is a public, unauthenticated REST API. It returns market
metadata (question, description, end date, outcomes, prices, volume,
liquidity, CLOB token ids).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from polyweat.api._http import get_json
from polyweat.logger import get_logger

log = get_logger("gamma")


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(v: Any) -> Optional[datetime]:
    if not v or not isinstance(v, str):
        return None
    s = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _split_list(v: Any) -> List[str]:
    """Gamma returns some list-fields as JSON-encoded strings."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            import json as _json
            try:
                arr = _json.loads(s)
                return [str(x) for x in arr]
            except Exception:
                return []
    return []


class GammaClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_active_markets(
        self, *, limit: int = 500, max_pages: int = 6
    ) -> List[Dict[str, Any]]:
        """Return active, non-closed markets ordered by liquidity (desc).

        We page through results because Gamma caps `limit` at ~500.
        """
        markets: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": limit,
                "offset": offset,
                "order": "liquidity",
                "ascending": "false",
            }
            try:
                page = get_json(
                    f"{self.base_url}/markets",
                    params=params,
                    timeout=self.timeout,
                )
            except Exception as exc:
                log.error("Gamma fetch failed at offset %d: %s", offset, exc)
                break
            if not isinstance(page, list) or not page:
                break
            markets.extend(page)
            if len(page) < limit:
                break
            offset += limit
        log.info("Gamma: fetched %d active markets", len(markets))
        return markets

    def fetch_market(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single market by id (works for closed/resolved markets too)."""
        if not market_id:
            return None
        try:
            return get_json(
                f"{self.base_url}/markets/{market_id}",
                timeout=self.timeout,
            )
        except Exception as exc:
            log.warning("fetch_market(%s) failed: %s", market_id, exc)
            return None

    @staticmethod
    def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a raw Gamma market dict into the fields we need."""
        question = (raw.get("question") or raw.get("title") or "").strip()
        description = (raw.get("description") or "").strip()
        end_iso = (
            raw.get("endDate")
            or raw.get("endDateIso")
            or raw.get("end_date_iso")
        )
        end_time = _parse_iso(end_iso)

        outcomes = _split_list(raw.get("outcomes"))
        prices = _split_list(raw.get("outcomePrices"))
        token_ids = _split_list(raw.get("clobTokenIds"))

        yes_idx = None
        no_idx = None
        for i, o in enumerate(outcomes):
            ol = o.strip().lower()
            if ol in ("yes",) and yes_idx is None:
                yes_idx = i
            elif ol in ("no",) and no_idx is None:
                no_idx = i

        def _safe(arr: List[Any], i: Optional[int]) -> Optional[Any]:
            if i is None or i >= len(arr):
                return None
            return arr[i]

        yes_price = _to_float(_safe(prices, yes_idx))
        no_price = _to_float(_safe(prices, no_idx))
        yes_token = _safe(token_ids, yes_idx)
        no_token = _safe(token_ids, no_idx)

        market_id = (
            str(raw.get("id"))
            if raw.get("id") is not None
            else str(raw.get("conditionId") or raw.get("slug") or question)
        )

        # Binary-market sanity check: the bot only trades plain YES/NO
        # markets. If we couldn't identify both outcome legs we mark the
        # market as non-binary and the runner skips it explicitly.
        is_binary = (
            yes_idx is not None and no_idx is not None
            and yes_token is not None and no_token is not None
        )

        return {
            "market_id": market_id,
            "condition_id": raw.get("conditionId"),
            "slug": raw.get("slug"),
            "question": question,
            "description": description,
            "end_time": end_time,
            "outcomes": outcomes,
            "prices": [_to_float(p) for p in prices],
            "token_ids": token_ids,
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_usd": _to_float(raw.get("volume")) or 0.0,
            "liquidity_usd": _to_float(raw.get("liquidity")) or 0.0,
            "active": bool(raw.get("active", True)),
            "closed": bool(raw.get("closed", False)),
            "is_binary": is_binary,
            "raw": raw,
        }
