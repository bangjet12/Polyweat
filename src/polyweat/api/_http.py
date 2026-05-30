"""Tiny HTTP helper with retries, used by all API clients."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

from polyweat.logger import get_logger

log = get_logger("http")


class HttpError(Exception):
    """Raised when an HTTP call fails after retries."""


def get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    retries: int = 3,
    backoff: float = 0.8,
) -> Any:
    """GET ``url`` and return parsed JSON. Retries on transient errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url, params=params, headers=headers or {}, timeout=timeout
            )
            if resp.status_code >= 500:
                raise HttpError(f"{resp.status_code} {resp.reason} from {url}")
            if resp.status_code == 429:
                # rate limited - obey backoff
                wait = backoff * (2 ** (attempt - 1))
                log.warning("Rate limited (429) from %s, sleeping %.2fs", url, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise HttpError(
                    f"HTTP {resp.status_code} {resp.reason} from {url}: "
                    f"{resp.text[:300]}"
                )
            return resp.json()
        except (requests.RequestException, HttpError, ValueError) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            wait = backoff * (2 ** (attempt - 1))
            log.warning(
                "HTTP attempt %d/%d failed for %s: %s (sleep %.2fs)",
                attempt, retries, url, exc, wait,
            )
            time.sleep(wait)
    raise HttpError(f"GET {url} failed after {retries} attempts: {last_exc}")
