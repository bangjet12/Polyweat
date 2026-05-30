"""Main scan / decide / trade loop."""

from __future__ import annotations

import signal
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from polyweat.api.clob import ClobClient
from polyweat.api.gamma import GammaClient
from polyweat.api.open_meteo import OpenMeteoClient
from polyweat.config import Config
from polyweat.db import Database
from polyweat.logger import get_logger
from polyweat.models import (
    OrderbookSnapshot,
    ParsedMarket,
    TradeDecision,
    WeatherForecast,
)
from polyweat.parser.filter import is_weather_temperature_market, parse_market
from polyweat.strategy.decision import make_decision
from polyweat.trading.trader import BaseTrader, build_trader

log = get_logger("runner")


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.gamma = GammaClient(cfg.gamma_api_base, cfg.http_timeout_seconds)
        self.clob = ClobClient(cfg.clob_api_base, cfg.http_timeout_seconds)
        self.weather = OpenMeteoClient(
            cfg.open_meteo_base,
            cfg.open_meteo_geocode_base,
            cfg.http_timeout_seconds,
        )
        self.trader: BaseTrader = build_trader(self.db, cfg)
        self._stop = False

        # Per-cycle forecast cache: { (lat,lon, target_day_iso): WeatherForecast }
        self._fc_cache: Dict[Tuple[float, float, str], WeatherForecast] = {}

    # ------------------------------------------------------------------
    # Forecast cache (per cycle)
    # ------------------------------------------------------------------

    def _forecast_for(self, pm: ParsedMarket) -> Optional[WeatherForecast]:
        if pm.city_lat is None or pm.city_lon is None or pm.city is None:
            return None
        day_key = ""
        if pm.target_date is not None:
            day_key = pm.target_date.date().isoformat()
        key = (round(pm.city_lat, 3), round(pm.city_lon, 3), day_key)
        cached = self._fc_cache.get(key)
        if cached is not None:
            return cached
        fc = self.weather.build_forecast(
            pm.city, pm.city_lat, pm.city_lon, pm.city_tz or "auto", pm.target_date
        )
        if fc is not None:
            self._fc_cache[key] = fc
            self.db.insert_forecast(
                city=fc.city,
                lat=fc.lat,
                lon=fc.lon,
                tz=fc.tz,
                daily_high_c=fc.daily_high_c,
                daily_low_c=fc.daily_low_c,
                window_high_c=fc.forecast_window_high_c,
                window_low_c=fc.forecast_window_low_c,
                raw={
                    "hourly_count": len(fc.hourly_temps_c),
                    "fetched_at": fc.fetched_at.isoformat(),
                },
            )
        return fc

    # ------------------------------------------------------------------
    # Single scan cycle
    # ------------------------------------------------------------------

    def scan_once(self) -> Dict[str, int]:
        """One full pass: fetch -> filter -> decide -> trade. Return counters."""
        self._fc_cache.clear()
        counters: Dict[str, int] = {
            "fetched": 0, "weather": 0, "parsed_ok": 0,
            "decisions": 0, "entered": 0, "passive": 0,
            "watch": 0, "skip": 0, "rejected_pre_parse": 0,
            "any_resolve_under_6h": 0,
        }

        try:
            raw_markets = self.gamma.fetch_active_markets()
        except Exception as exc:
            log.error("Failed to fetch active markets: %s", exc)
            return counters
        counters["fetched"] = len(raw_markets)

        candidates: List[ParsedMarket] = []
        for raw in raw_markets:
            try:
                nm = self.gamma.normalize(raw)
            except Exception as exc:
                log.warning("normalize() failed: %s", exc)
                continue

            q = nm.get("question") or ""
            d = nm.get("description") or ""
            is_weather = is_weather_temperature_market(q, d)

            self.db.insert_scanned_market(
                market_id=nm["market_id"],
                title=q,
                end_time=nm.get("end_time"),
                yes_price=nm.get("yes_price"),
                no_price=nm.get("no_price"),
                liquidity_usd=float(nm.get("liquidity_usd") or 0.0),
                volume_usd=float(nm.get("volume_usd") or 0.0),
                is_weather=is_weather,
                parse_score=0.0,
                raw={"slug": nm.get("slug"), "outcomes": nm.get("outcomes")},
            )

            if not is_weather:
                continue
            counters["weather"] += 1

            pm = parse_market(nm, geocoder=self.weather)
            if pm.parse_score >= 0.4:
                counters["parsed_ok"] += 1
                candidates.append(pm)
            else:
                counters["rejected_pre_parse"] += 1
                self.db.insert_rejected(
                    pm.market_id, pm.title,
                    f"low_parse_score_{pm.parse_score:.2f}",
                )

        log.info(
            "scan: fetched=%d weather=%d parsed_ok=%d",
            counters["fetched"], counters["weather"], counters["parsed_ok"],
        )

        # ------- per-candidate decision + execution -------
        for pm in candidates:
            hours_to_res = self._hours_until(pm.end_time)
            if hours_to_res is not None and hours_to_res <= self.cfg.best_hours_to_resolution:
                counters["any_resolve_under_6h"] += 1

            try:
                td = self._decide_and_trade(pm)
            except Exception as exc:
                log.exception("decide_and_trade failed for %s: %s", pm.market_id, exc)
                continue
            if td is None:
                continue
            counters["decisions"] += 1
            if td.decision == "ENTER":
                counters["entered"] += 1
            elif td.decision == "PASSIVE":
                counters["passive"] += 1
            elif td.decision == "WATCH":
                counters["watch"] += 1
            else:
                counters["skip"] += 1

        # ------- maintain passive orders -------
        try:
            self.trader.reconcile_passive_orders()
        except Exception as exc:
            log.exception("reconcile_passive_orders failed: %s", exc)

        return counters

    # ------------------------------------------------------------------
    # Per-market work
    # ------------------------------------------------------------------

    def _decide_and_trade(self, pm: ParsedMarket) -> Optional[TradeDecision]:
        # Forecast (cached)
        fc = self._forecast_for(pm)

        # Orderbook for the relevant side
        book_yes = (
            self.clob.fetch_orderbook(pm.yes_token_id) if pm.yes_token_id else None
        )
        book_no = (
            self.clob.fetch_orderbook(pm.no_token_id) if pm.no_token_id else None
        )

        td = make_decision(
            pm, fc, book_yes, book_no, self.cfg,
            has_open_position=self.db.has_open_position(pm.market_id),
            open_positions_count=self.db.count_open_positions(),
            daily_loss_so_far_usd=self.db.daily_loss_today(),
        )

        # Persist decision
        try:
            self.db.insert_decision(self._decision_to_row(td))
        except Exception as exc:
            log.exception("insert_decision failed: %s", exc)

        # Maintain watchlist
        if td.decision == "WATCH":
            self.db.upsert_watchlist(
                market_id=td.market_id,
                title=td.title,
                city=td.city,
                target_date=td.target_date,
                market_kind=td.market_kind,
                threshold_c=td.threshold_c,
                threshold_f=td.threshold_f,
                outcome=td.outcome,
                bot_probability=td.bot_probability,
                confidence_score=td.confidence_score,
                last_market_price=td.market_price,
                last_spread_percent=td.spread_percent,
                last_liquidity_usd=td.liquidity_usd,
                reason=td.skip_reason or "near-miss",
            )
        elif td.decision in ("ENTER", "PASSIVE"):
            # If it was on the watchlist, remove it.
            self.db.remove_from_watchlist(td.market_id)
            self.trader.execute(td)
        elif td.decision == "SKIP":
            # Drop from watchlist if previously parked there.
            self.db.remove_from_watchlist(td.market_id)

        log.info(
            "decision[%s] %-7s %-3s prob=%s conf=%s d=%sC ask=%s spread=%s liq=%s "
            "h2res=%s reason=%s :: %s",
            td.market_id, td.decision, td.outcome,
            _fmt(td.bot_probability), _fmt(td.confidence_score),
            _fmt(td.temp_distance_c), _fmt(td.market_price),
            _fmt(td.spread_percent), _fmt(td.liquidity_usd),
            _fmt(td.hours_to_resolution), td.skip_reason,
            td.title[:80],
        )
        return td

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        self._install_signal_handlers()
        log.info(
            "Polyweat starting (DRY_RUN=%s LIVE=%s) ...",
            self.cfg.dry_run, self.cfg.live_trading,
        )
        while not self._stop:
            t0 = time.monotonic()
            try:
                counters = self.scan_once()
            except Exception as exc:
                log.exception("scan_once crashed: %s", exc)
                counters = {"any_resolve_under_6h": 0}
            elapsed = time.monotonic() - t0

            interval = self.cfg.scan_interval_seconds
            if counters.get("any_resolve_under_6h", 0) > 0:
                interval = self.cfg.fast_scan_interval_seconds

            sleep_for = max(1.0, interval - elapsed)
            log.info(
                "cycle done in %.2fs, sleep %.1fs (interval=%ds, fast=%s)",
                elapsed, sleep_for, interval,
                bool(counters.get("any_resolve_under_6h", 0)),
            )
            self._sleep(sleep_for)

        log.info("Polyweat stopped.")

    # ------------------------------------------------------------------
    # Signals + sleep
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log.warning("Received signal %s, shutting down...", signum)
            self._stop = True
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):  # pragma: no cover
            pass

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hours_until(end: Optional[datetime]) -> Optional[float]:
        if end is None:
            return None
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - datetime.now(timezone.utc)).total_seconds() / 3600.0

    @staticmethod
    def _decision_to_row(td: TradeDecision) -> Dict[str, Any]:
        return {
            "market_id": td.market_id,
            "title": td.title,
            "city": td.city,
            "target_date": td.target_date.isoformat() if td.target_date else None,
            "market_kind": td.market_kind,
            "threshold_c": td.threshold_c,
            "threshold_f": td.threshold_f,
            "outcome": td.outcome,
            "forecast_value_c": td.forecast_value_c,
            "temp_distance_c": td.temp_distance_c,
            "bot_probability": td.bot_probability,
            "confidence_score": td.confidence_score,
            "market_price": td.market_price,
            "spread_percent": td.spread_percent,
            "liquidity_usd": td.liquidity_usd,
            "decision": td.decision,
            "skip_reason": td.skip_reason,
            "hours_to_resolution": td.hours_to_resolution,
            "proposed_price": td.proposed_price,
            "proposed_size_usd": td.proposed_size_usd,
            "token_id": td.token_id,
            "timestamp": td.timestamp.isoformat(),
        }


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "  -  "
    return f"{v:.3f}" if isinstance(v, float) else str(v)
