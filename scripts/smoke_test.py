"""Quick smoke test of the pure-python parts of polyweat.

Does NOT touch the network and does NOT require `requests` /
`python-dotenv` to be installed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make src/ importable when running from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _ok(label: str) -> None:
    print(f"  PASS  {label}")


def test_config():
    from polyweat.config import load_config
    cfg = load_config()
    assert cfg.dry_run is True
    assert cfg.live_trading is False
    assert cfg.min_entry_price == 0.95
    assert cfg.max_entry_price == 0.985
    assert cfg.min_bot_probability == 0.93
    assert cfg.max_position_per_market_usd == 1.0
    assert cfg.max_open_positions == 5
    assert cfg.max_daily_loss_usd == 5.0
    assert cfg.is_live is False
    _ok("config defaults are safe (DRY_RUN, $1 cap, 5 max)")


def test_threshold_parser():
    from polyweat.parser.threshold_parser import parse_threshold

    cases = [
        ("Will the high in NYC exceed 80°F today?", "highest_gte", 80.0, "F"),
        ("Highest temperature in Chicago above 95F today?", "highest_gte", 95.0, "F"),
        ("Will the low in London drop below 5°C tomorrow?", "lowest_lte", 5.0, "C"),
        ("Will the high in Tokyo stay below 25°C?", "highest_lt", 25.0, "C"),
        ("Lowest temperature in Boston above 32°F overnight?", "lowest_gt", 32.0, "F"),
        ("Will it be exactly 70F at noon?", "exact_temp", 70.0, "F"),
        ("Trump wins the election", "unknown", None, None),
    ]
    for text, kind, val, unit in cases:
        out = parse_threshold(text)
        assert out["market_kind"] == kind, f"{text!r} -> got {out['market_kind']}"
        if val is not None:
            assert out["threshold_value"] == val, f"{text!r} -> {out['threshold_value']}"
            assert out["unit"] == unit
        _ok(f"threshold parse: {text!r} -> {kind}")


def test_city_parser():
    from polyweat.parser.city_parser import extract_city
    assert extract_city("High temperature in NYC tomorrow") == "New York"
    assert extract_city("Will Chicago hit 90F?") == "Chicago"
    assert extract_city("LA forecast for Saturday") == "Los Angeles"
    assert extract_city("temperature in Tokyo") == "Tokyo"
    assert extract_city("nothing here") is None
    _ok("city parser: NYC/Chicago/LA/Tokyo")


def test_date_parser():
    from polyweat.parser.date_parser import extract_date
    now = datetime.now(timezone.utc)
    fb = now + timedelta(hours=10)
    out = extract_date("Will it be hot today?", fallback=fb)
    assert out is not None
    out2 = extract_date("Will it be hot tomorrow?", fallback=fb)
    assert out2 is not None and out2.date() == (now + timedelta(days=1)).date()
    out3 = extract_date("Forecast for May 30", fallback=fb)
    assert out3 is not None
    _ok("date parser: today/tomorrow/Month Day")


def test_filter():
    from polyweat.parser.filter import is_weather_temperature_market
    assert is_weather_temperature_market(
        "Will the high in NYC exceed 80°F today?"
    ) is True
    assert is_weather_temperature_market(
        "Highest temperature in Chicago this week?"
    ) is True
    # Block sports/politics/crypto even if they accidentally mention weather
    assert is_weather_temperature_market(
        "Will Trump win the election?", "weather forecast for ohio"
    ) is False
    assert is_weather_temperature_market(
        "BTC price by end of week"
    ) is False
    assert is_weather_temperature_market(
        "Will the Knicks win tonight?"
    ) is False
    _ok("filter: accepts weather, rejects sports/politics/crypto")


def test_predictor_probability_curve():
    from polyweat.strategy.predictor import _prob_from_distance
    p2 = _prob_from_distance(2.0)
    p3 = _prob_from_distance(3.0)
    p5 = _prob_from_distance(5.0)
    assert 0.92 < p2 < 0.94, p2
    assert 0.96 < p3 < 0.98, p3
    assert 0.99 < p5 < 0.999, p5
    _ok(f"prob curve: d=2 -> {p2:.4f}, d=3 -> {p3:.4f}, d=5 -> {p5:.4f}")


def test_predictor_decision():
    from polyweat.models import ParsedMarket, WeatherForecast
    from polyweat.strategy.predictor import predict

    pm = ParsedMarket(
        market_id="m1", title="High in NYC above 80F today?", description="",
        end_time=datetime.now(timezone.utc) + timedelta(hours=10),
        yes_token_id="y", no_token_id="n", yes_price=0.97, no_price=0.03,
        liquidity_usd=600.0, city="New York", city_lat=40.7, city_lon=-74.0,
        city_tz="America/New_York",
        target_date=datetime.now(timezone.utc) + timedelta(hours=10),
        threshold_c=26.7, threshold_f=80.0,
        market_kind="highest_gte", unit="F",
        rules_clear=True, parse_score=1.0,
    )
    fc = WeatherForecast(
        city="New York", lat=40.7, lon=-74.0, tz="America/New_York",
        fetched_at=datetime.now(timezone.utc),
        hourly_times=[], hourly_temps_c=[28.0, 29.0, 30.0, 31.0],
        daily_high_c=31.0, daily_low_c=20.0,
        forecast_window_high_c=31.0, forecast_window_low_c=20.0,
    )
    pred = predict(pm, fc)
    assert pred.outcome == "YES"
    assert pred.temp_distance_c is not None and pred.temp_distance_c >= 4.0
    assert pred.bot_probability is not None and pred.bot_probability >= 0.98
    _ok(f"predict: outcome={pred.outcome} d={pred.temp_distance_c:.2f}C "
        f"prob={pred.bot_probability:.4f}")


def test_decision_engine():
    from datetime import datetime, timedelta, timezone
    from polyweat.config import load_config
    from polyweat.models import (
        OrderbookSnapshot, ParsedMarket, WeatherForecast,
    )
    from polyweat.strategy.decision import make_decision

    cfg = load_config()
    pm = ParsedMarket(
        market_id="m1", title="High in NYC above 80F today?", description="",
        end_time=datetime.now(timezone.utc) + timedelta(hours=8),
        yes_token_id="yes-tok", no_token_id="no-tok",
        yes_price=0.97, no_price=0.03,
        liquidity_usd=600.0, city="New York", city_lat=40.7, city_lon=-74.0,
        city_tz="America/New_York",
        target_date=datetime.now(timezone.utc) + timedelta(hours=8),
        threshold_c=26.7, threshold_f=80.0,
        market_kind="highest_gte", unit="F",
        rules_clear=True, parse_score=1.0,
    )
    fc = WeatherForecast(
        city="New York", lat=40.7, lon=-74.0, tz="America/New_York",
        fetched_at=datetime.now(timezone.utc),
        hourly_times=[], hourly_temps_c=[28.0, 29.0, 30.0, 31.0],
        daily_high_c=31.0, daily_low_c=20.0,
        forecast_window_high_c=31.0, forecast_window_low_c=20.0,
    )
    book_yes = OrderbookSnapshot(
        token_id="yes-tok", best_bid=0.96, best_ask=0.97,
        bid_size=200.0, ask_size=200.0,
        spread=0.01, spread_percent=1.04, mid=0.965,
        liquidity_usd=600.0, fetched_at=datetime.now(timezone.utc),
    )
    td = make_decision(
        pm, fc, book_yes, None, cfg,
        has_open_position=False,
        open_positions_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "ENTER", f"expected ENTER got {td.decision} ({td.skip_reason})"
    assert td.outcome == "YES"
    assert td.proposed_price == 0.97
    _ok(f"decision: ENTER YES @ {td.proposed_price} prob={td.bot_probability}")

    # Now break it: ask too high -> PASSIVE
    book_yes_high = OrderbookSnapshot(
        token_id="yes-tok", best_bid=0.985, best_ask=0.99,
        bid_size=200.0, ask_size=200.0,
        spread=0.005, spread_percent=0.51, mid=0.9875,
        liquidity_usd=600.0, fetched_at=datetime.now(timezone.utc),
    )
    td2 = make_decision(
        pm, fc, book_yes_high, None, cfg,
        has_open_position=False,
        open_positions_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td2.decision == "PASSIVE", f"expected PASSIVE got {td2.decision}"
    assert (
        cfg.passive_order_min_price <= td2.proposed_price <= cfg.passive_order_max_price
    )
    _ok(f"decision: PASSIVE @ {td2.proposed_price}")

    # Now: forecast right at threshold -> SKIP
    fc_close = WeatherForecast(
        city="New York", lat=40.7, lon=-74.0, tz="America/New_York",
        fetched_at=datetime.now(timezone.utc),
        hourly_times=[], hourly_temps_c=[26.5, 27.0, 27.5],
        daily_high_c=27.5, daily_low_c=20.0,
        forecast_window_high_c=27.5, forecast_window_low_c=20.0,
    )
    td3 = make_decision(
        pm, fc_close, book_yes, None, cfg,
        has_open_position=False, open_positions_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td3.decision == "SKIP"
    assert "forecast_too_close" in (td3.skip_reason or "")
    _ok(f"decision: SKIP near-threshold ({td3.skip_reason})")


def test_db_roundtrip(tmp_db: Path):
    from polyweat.db import Database
    db = Database(tmp_db)
    db.insert_decision({
        "market_id": "m1", "title": "t", "city": "NY",
        "target_date": None, "market_kind": "highest_gte",
        "threshold_c": 26.7, "threshold_f": 80.0, "outcome": "YES",
        "forecast_value_c": 31.0, "temp_distance_c": 4.3,
        "bot_probability": 0.97, "confidence_score": 0.95,
        "market_price": 0.97, "spread_percent": 1.0, "liquidity_usd": 500.0,
        "decision": "ENTER", "skip_reason": None,
        "hours_to_resolution": 5.0, "proposed_price": 0.97,
        "proposed_size_usd": 1.0, "token_id": "tok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    db.upsert_position("m1", "tok", "t", "YES", 0.97, 1.0, 1.03, "open")
    assert db.has_open_position("m1") is True
    assert db.count_open_positions() == 1

    db.upsert_watchlist(
        "m2", "t2", "Chicago", None, "highest_gte", 30.0, 86.0, "YES",
        0.94, 0.92, 0.99, 0.6, 400.0, "price slightly high",
    )
    assert len(db.list_watchlist()) == 1

    db.close_position("m1", -0.50)
    assert db.daily_loss_today() == 0.50
    _ok("db: insert decision/position/watchlist/close round-trips")


def main() -> int:
    print("Polyweat smoke test")
    print("-" * 40)
    test_config()
    test_threshold_parser()
    test_city_parser()
    test_date_parser()
    test_filter()
    test_predictor_probability_curve()
    test_predictor_decision()
    test_decision_engine()

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        test_db_roundtrip(Path(td) / "test.db")
    print("-" * 40)
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
