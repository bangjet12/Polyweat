"""Centralized configuration loaded from .env / environment variables.

All thresholds documented in the README live here, so that the rest of the
codebase can read a single typed object instead of touching `os.environ`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is in requirements
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


def _get_str(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    return default if val is None or val == "" else val


_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f"}


def _get_bool(key: str, default: bool = False) -> bool:
    """Return True/False/<default>.

    Crucially, a value that is *set but unrecognized* (typo such as
    ``DRY_RUN=ture``) returns ``default`` so we never silently flip the
    safe default to live mode. A warning is logged in :func:`load_config`.
    """
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    v = val.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return default  # unrecognized -> keep the SAFE default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Strongly-typed config used across the bot."""

    # ----- Polymarket creds -----
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_private_key: str = ""
    polymarket_proxy_address: str = ""
    # 1 = direct EOA signer (raw private key); 2 = proxy/Magic.link.
    polymarket_signature_type: int = 2

    # ----- Mode -----
    dry_run: bool = True
    live_trading: bool = False

    # ----- Weather provider -----
    weather_provider: str = "open_meteo"
    weather_api_key: str = ""

    # ----- Cadence -----
    scan_interval_seconds: int = 60
    fast_scan_interval_seconds: int = 30

    # ----- Entry price band -----
    min_entry_price: float = 0.95
    max_entry_price: float = 0.985

    # ----- Resolution horizons (hours) -----
    max_hours_to_resolution: float = 18.0
    preferred_hours_to_resolution: float = 12.0
    best_hours_to_resolution: float = 6.0

    # ----- Bot probability thresholds -----
    min_bot_probability: float = 0.93
    preferred_bot_probability: float = 0.95

    # ----- Confidence thresholds -----
    min_confidence_score: float = 0.90
    preferred_confidence_score: float = 0.95

    # ----- Forecast distance -----
    min_temp_distance_c: float = 2.0
    preferred_temp_distance_c: float = 3.0
    min_temp_distance_f: float = 3.5
    preferred_temp_distance_f: float = 5.0

    # ----- Liquidity -----
    min_liquidity_usd: float = 250.0
    preferred_liquidity_usd: float = 500.0

    # ----- Spread -----
    max_spread_percent: float = 1.5
    preferred_spread_percent: float = 1.0

    # ----- Risk -----
    max_position_per_market_usd: float = 1.0
    max_open_positions: int = 5
    max_daily_loss_usd: float = 5.0

    # ----- Passive limit orders -----
    allow_passive_limit_orders: bool = True
    passive_order_min_price: float = 0.95
    passive_order_max_price: float = 0.975
    passive_order_expire_seconds: int = 180

    # ----- Safety toggles -----
    allow_longshot: bool = False
    allow_exact_temp_markets: bool = False
    allow_ambiguous_markets: bool = False

    # ----- Reliability -----
    max_consecutive_scan_failures: int = 5
    scanned_markets_retention_days: int = 7

    # ----- Storage -----
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    log_dir: Path = field(default_factory=lambda: Path("./logs"))
    db_path: Path = field(default_factory=lambda: Path("./data/polyweat.db"))
    log_level: str = "INFO"

    # ----- Endpoints -----
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_api_base: str = "https://clob.polymarket.com"
    open_meteo_base: str = "https://api.open-meteo.com/v1"
    open_meteo_geocode_base: str = "https://geocoding-api.open-meteo.com/v1"
    http_timeout_seconds: float = 15.0

    # ----- Derived flags -----
    @property
    def is_live(self) -> bool:
        """True only if BOTH dry_run is False AND live_trading is True."""
        return (not self.dry_run) and self.live_trading

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def load_config(env_file: Optional[str] = None) -> Config:
    """Load .env (if present) and return a populated Config object."""
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        # Try the project root .env, otherwise rely on real environment
        for candidate in (".env", str(Path.cwd() / ".env")):
            if Path(candidate).is_file():
                load_dotenv(candidate, override=False)
                break

    cfg = Config(
        polymarket_api_key=_get_str("POLYMARKET_API_KEY"),
        polymarket_api_secret=_get_str("POLYMARKET_API_SECRET"),
        polymarket_api_passphrase=_get_str("POLYMARKET_API_PASSPHRASE"),
        polymarket_private_key=_get_str("POLYMARKET_PRIVATE_KEY"),
        polymarket_proxy_address=_get_str("POLYMARKET_PROXY_ADDRESS"),
        polymarket_signature_type=_get_int("POLYMARKET_SIGNATURE_TYPE", 2),
        dry_run=_get_bool("DRY_RUN", True),
        live_trading=_get_bool("LIVE_TRADING", False),
        weather_provider=_get_str("WEATHER_PROVIDER", "open_meteo"),
        weather_api_key=_get_str("WEATHER_API_KEY"),
        scan_interval_seconds=_get_int("SCAN_INTERVAL_SECONDS", 60),
        fast_scan_interval_seconds=_get_int("FAST_SCAN_INTERVAL_SECONDS", 30),
        min_entry_price=_get_float("MIN_ENTRY_PRICE", 0.95),
        max_entry_price=_get_float("MAX_ENTRY_PRICE", 0.985),
        max_hours_to_resolution=_get_float("MAX_HOURS_TO_RESOLUTION", 18.0),
        preferred_hours_to_resolution=_get_float("PREFERRED_HOURS_TO_RESOLUTION", 12.0),
        best_hours_to_resolution=_get_float("BEST_HOURS_TO_RESOLUTION", 6.0),
        min_bot_probability=_get_float("MIN_BOT_PROBABILITY", 0.93),
        preferred_bot_probability=_get_float("PREFERRED_BOT_PROBABILITY", 0.95),
        min_confidence_score=_get_float("MIN_CONFIDENCE_SCORE", 0.90),
        preferred_confidence_score=_get_float("PREFERRED_CONFIDENCE_SCORE", 0.95),
        min_temp_distance_c=_get_float("MIN_TEMP_DISTANCE_C", 2.0),
        preferred_temp_distance_c=_get_float("PREFERRED_TEMP_DISTANCE_C", 3.0),
        min_temp_distance_f=_get_float("MIN_TEMP_DISTANCE_F", 3.5),
        preferred_temp_distance_f=_get_float("PREFERRED_TEMP_DISTANCE_F", 5.0),
        min_liquidity_usd=_get_float("MIN_LIQUIDITY_USD", 250.0),
        preferred_liquidity_usd=_get_float("PREFERRED_LIQUIDITY_USD", 500.0),
        max_spread_percent=_get_float("MAX_SPREAD_PERCENT", 1.5),
        preferred_spread_percent=_get_float("PREFERRED_SPREAD_PERCENT", 1.0),
        max_position_per_market_usd=_get_float("MAX_POSITION_PER_MARKET_USD", 1.0),
        max_open_positions=_get_int("MAX_OPEN_POSITIONS", 5),
        max_daily_loss_usd=_get_float("MAX_DAILY_LOSS_USD", 5.0),
        allow_passive_limit_orders=_get_bool("ALLOW_PASSIVE_LIMIT_ORDERS", True),
        passive_order_min_price=_get_float("PASSIVE_ORDER_MIN_PRICE", 0.95),
        passive_order_max_price=_get_float("PASSIVE_ORDER_MAX_PRICE", 0.975),
        passive_order_expire_seconds=_get_int("PASSIVE_ORDER_EXPIRE_SECONDS", 180),
        allow_longshot=_get_bool("ALLOW_LONGSHOT", False),
        allow_exact_temp_markets=_get_bool("ALLOW_EXACT_TEMP_MARKETS", False),
        allow_ambiguous_markets=_get_bool("ALLOW_AMBIGUOUS_MARKETS", False),
        max_consecutive_scan_failures=_get_int("MAX_CONSECUTIVE_SCAN_FAILURES", 5),
        scanned_markets_retention_days=_get_int("SCANNED_MARKETS_RETENTION_DAYS", 7),
        data_dir=Path(_get_str("DATA_DIR", "./data")),
        log_dir=Path(_get_str("LOG_DIR", "./logs")),
        db_path=Path(_get_str("DB_PATH", "./data/polyweat.db")),
        log_level=_get_str("LOG_LEVEL", "INFO"),
        gamma_api_base=_get_str("GAMMA_API_BASE", "https://gamma-api.polymarket.com"),
        clob_api_base=_get_str("CLOB_API_BASE", "https://clob.polymarket.com"),
        open_meteo_base=_get_str("OPEN_METEO_BASE", "https://api.open-meteo.com/v1"),
        open_meteo_geocode_base=_get_str(
            "OPEN_METEO_GEOCODE_BASE", "https://geocoding-api.open-meteo.com/v1"
        ),
        http_timeout_seconds=_get_float("HTTP_TIMEOUT_SECONDS", 15.0),
    )
    cfg.ensure_dirs()

    # Sanity-warn the operator when the live flags are visibly garbled.
    # We don't raise; we just record the fact, and we keep the safe default.
    import logging
    _log = logging.getLogger("polyweat.config")
    for key, default in (("DRY_RUN", True), ("LIVE_TRADING", False)):
        raw = os.environ.get(key)
        if raw is not None and raw.strip() != "" and raw.strip().lower() not in (
            _TRUTHY | _FALSY
        ):
            _log.warning(
                "Unrecognized boolean for %s=%r; using safe default %s",
                key, raw, default,
            )
    return cfg
