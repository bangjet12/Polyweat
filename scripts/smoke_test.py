"""Smoke + failure-path tests for the pure-python parts of polyweat.

Does NOT touch the network and does NOT require ``requests`` /
``python-dotenv`` to be installed.

Run from the repo root:

    python scripts/smoke_test.py

Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make src/ importable when running from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _ok(label: str) -> None:
    print(f"  PASS  {label}")


# =====================================================================
# Happy-path tests
# =====================================================================

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
    assert cfg.polymarket_signature_type == 2
    _ok("config defaults are safe (DRY_RUN, $1 cap, 5 max, signature_type=2)")


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
    assert extract_date("Will it be hot today?", fallback=fb) is not None
    out2 = extract_date("Will it be hot tomorrow?", fallback=fb)
    assert out2 is not None and out2.date() == (now + timedelta(days=1)).date()
    assert extract_date("Forecast for May 30", fallback=fb) is not None
    _ok("date parser: today/tomorrow/Month Day")


def test_filter():
    from polyweat.parser.filter import is_weather_temperature_market
    assert is_weather_temperature_market(
        "Will the high in NYC exceed 80°F today?"
    ) is True
    assert is_weather_temperature_market(
        "Highest temperature in Chicago this week?"
    ) is True
    assert is_weather_temperature_market(
        "Will Trump win the election?", "weather forecast for ohio"
    ) is False
    assert is_weather_temperature_market("BTC price by end of week") is False
    assert is_weather_temperature_market("Will the Knicks win tonight?") is False
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


def _build_pm_yes(threshold_c=26.7, threshold_f=80.0, kind="highest_gte"):
    from polyweat.models import ParsedMarket
    return ParsedMarket(
        market_id="m1", title="High in NYC above 80F today?", description="",
        end_time=datetime.now(timezone.utc) + timedelta(hours=8),
        yes_token_id="y", no_token_id="n", yes_price=0.97, no_price=0.03,
        liquidity_usd=600.0, city="New York", city_lat=40.7, city_lon=-74.0,
        city_tz="America/New_York",
        target_date=datetime.now(timezone.utc) + timedelta(hours=8),
        threshold_c=threshold_c, threshold_f=threshold_f,
        market_kind=kind, unit="F",
        rules_clear=True, parse_score=1.0,
    )


def _build_fc(window_high=31.0, window_low=20.0):
    from polyweat.models import WeatherForecast
    return WeatherForecast(
        city="New York", lat=40.7, lon=-74.0, tz="America/New_York",
        fetched_at=datetime.now(timezone.utc),
        hourly_times=[], hourly_temps_c=[window_high],
        daily_high_c=window_high, daily_low_c=window_low,
        forecast_window_high_c=window_high, forecast_window_low_c=window_low,
    )


def _build_book(best_ask=0.97, best_bid=0.96, liquidity=600.0):
    from polyweat.models import OrderbookSnapshot
    spread = best_ask - best_bid if best_ask and best_bid else None
    mid = (best_ask + best_bid) / 2 if best_ask and best_bid else None
    spread_pct = (spread / mid * 100) if (spread is not None and mid) else None
    return OrderbookSnapshot(
        token_id="y", best_bid=best_bid, best_ask=best_ask,
        bid_size=200.0, ask_size=200.0,
        spread=spread, spread_percent=spread_pct, mid=mid,
        liquidity_usd=liquidity, fetched_at=datetime.now(timezone.utc),
    )


def test_predictor_decision():
    from polyweat.strategy.predictor import predict
    pm = _build_pm_yes()
    fc = _build_fc(window_high=31.0)
    pred = predict(pm, fc)
    assert pred.outcome == "YES"
    assert pred.temp_distance_c is not None and pred.temp_distance_c >= 4.0
    assert pred.bot_probability is not None and pred.bot_probability >= 0.98
    _ok(f"predict: outcome={pred.outcome} d={pred.temp_distance_c:.2f}C "
        f"prob={pred.bot_probability:.4f}")


def test_decision_engine_happy_path():
    from polyweat.config import load_config
    from polyweat.strategy.decision import make_decision

    cfg = load_config()
    pm = _build_pm_yes()
    fc = _build_fc(31.0)
    book = _build_book(0.97, 0.96)
    td = make_decision(
        pm, fc, book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "ENTER", f"expected ENTER got {td.decision} ({td.skip_reason})"
    assert td.outcome == "YES"
    assert td.proposed_price == 0.97
    _ok(f"decision: ENTER YES @ {td.proposed_price} prob={td.bot_probability}")

    book2 = _build_book(0.99, 0.985)
    td2 = make_decision(
        pm, fc, book2, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td2.decision == "PASSIVE", f"expected PASSIVE got {td2.decision}"
    assert cfg.passive_order_min_price <= td2.proposed_price <= cfg.passive_order_max_price
    _ok(f"decision: PASSIVE @ {td2.proposed_price}")

    td3 = make_decision(
        pm, _build_fc(27.5), book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
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


# =====================================================================
# Failure-path tests (cover all bug fixes from the pre-deploy review)
# =====================================================================

def test_get_bool_failsafe():
    """C1: typos and empty values must NOT silently flip a True default."""
    from polyweat.config import _get_bool

    for raw in ("ture", "garbage", "", "  ", "TRU", "yess"):
        os.environ["POLYWEAT_TEST_BOOL"] = raw
        assert _get_bool("POLYWEAT_TEST_BOOL", True) is True, raw
        assert _get_bool("POLYWEAT_TEST_BOOL", False) is False, raw
    for raw in ("true", "TRUE", "Yes", "1", "on"):
        os.environ["POLYWEAT_TEST_BOOL"] = raw
        assert _get_bool("POLYWEAT_TEST_BOOL", False) is True, raw
    for raw in ("false", "0", "no", "off"):
        os.environ["POLYWEAT_TEST_BOOL"] = raw
        assert _get_bool("POLYWEAT_TEST_BOOL", True) is False, raw
    os.environ.pop("POLYWEAT_TEST_BOOL", None)
    _ok("C1: _get_bool keeps default on unrecognized/typo values")


def test_threshold_word_boundary():
    """C5: 'higher' must NOT trigger the 'high' hint (and similarly for 'lower')."""
    from polyweat.parser.threshold_parser import parse_threshold
    out = parse_threshold("Will the daily low be higher than 60F today?")
    assert out["market_kind"] == "lowest_gt", out
    _ok("C5: 'low ... higher than 60F' -> lowest_gt (not highest_gte)")
    out = parse_threshold("Will the daily high be lower than 80F today?")
    assert out["market_kind"] == "highest_lt", out
    _ok("C5: 'high ... lower than 80F' -> highest_lt (not lowest_lte)")


def test_threshold_range_market():
    """C6: range markets must be recognized and marked as 'range'."""
    from polyweat.parser.threshold_parser import parse_threshold
    out = parse_threshold("Will the high in NYC be between 70F and 80F today?")
    assert out["market_kind"] == "range", out
    assert out["is_range"] is True
    _ok("C6: 'between 70F and 80F' -> range market")
    out = parse_threshold("Will it be from 70 to 80 degrees today in NYC?")
    assert out["market_kind"] == "range", out
    _ok("C6: 'from 70 to 80 degrees' -> range market")


def test_decision_skips_range():
    """C6 -> decision engine SKIPs range markets even if rules_clear."""
    from polyweat.config import load_config
    from polyweat.strategy.decision import make_decision

    cfg = load_config()
    pm = _build_pm_yes(threshold_c=21.1, threshold_f=70.0, kind="range")
    fc = _build_fc(29.0)
    book = _build_book(0.97, 0.96)
    td = make_decision(
        pm, fc, book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "SKIP" and td.skip_reason == "range_market_skipped"
    _ok("C6: decision engine skips range markets")


def test_decision_passive_cap():
    """C7: open passive orders count toward MAX_OPEN_POSITIONS."""
    from polyweat.config import load_config
    from polyweat.strategy.decision import make_decision

    cfg = load_config()
    td = make_decision(
        _build_pm_yes(), _build_fc(31.0), _build_book(0.97, 0.96), None, cfg,
        has_open_position=False,
        open_positions_count=4,
        open_passive_count=1,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "SKIP"
    assert "max_open_positions" in (td.skip_reason or "")
    _ok("C7: passive orders count toward MAX_OPEN_POSITIONS")


def test_decision_f_distance_gate():
    """I1: F-distance gate is wired up and enforced.

    Note: 2.0C ≈ 3.6F so the C gate is slightly stricter than the F gate.
    We just verify the path doesn't crash, the F-distance is computed,
    and that a comfortable distance (>3°C / >5.4°F) passes both gates.
    """
    from polyweat.config import load_config
    from polyweat.strategy.decision import make_decision

    cfg = load_config()
    pm = _build_pm_yes(threshold_c=26.667, threshold_f=80.0)
    book = _build_book(0.97, 0.96)
    td = make_decision(
        pm, _build_fc(31.0), book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "ENTER", f"expected ENTER got {td.decision} {td.skip_reason}"

    td2 = make_decision(
        pm, _build_fc(28.0), book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td2.decision == "SKIP"
    assert "forecast_too_close_to_threshold" in (td2.skip_reason or "")
    _ok("I1: distance gates enforced (4.33C/7.8F passes; 1.33C/2.4F skips)")


def test_decision_skip_reasons_complete():
    """Verify SKIP reasons cover the new gates added in this fix-pass."""
    from polyweat.config import load_config
    from polyweat.strategy.decision import make_decision

    cfg = load_config()

    td = make_decision(
        _build_pm_yes(), _build_fc(31.0), None, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "SKIP" and td.skip_reason == "no_orderbook"

    td2 = make_decision(
        _build_pm_yes(), _build_fc(31.0), _build_book(0.97, 0.96), None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=cfg.max_daily_loss_usd,
    )
    assert td2.decision == "SKIP" and "daily_loss_limit_hit" in (td2.skip_reason or "")

    td3 = make_decision(
        _build_pm_yes(), _build_fc(31.0), _build_book(0.97, 0.96), None, cfg,
        has_open_position=True, open_positions_count=1,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td3.decision == "SKIP" and td3.skip_reason == "already_have_position_in_market"

    td4 = make_decision(
        _build_pm_yes(), _build_fc(31.0), _build_book(0.99, 0.95), None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td4.decision == "SKIP" and "spread_too_wide" in (td4.skip_reason or "")

    _ok("decision SKIP reasons: no_orderbook / daily_loss / duplicate / spread")


def test_daily_loss_cap():
    """C4: daily loss cap engages once positions are closed at a loss."""
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        db.upsert_position("m1", "tok", "Title", "YES", 0.97, 1.0, 1.03, "open")
        db.close_position("m1", -0.97)
        db.upsert_position("m2", "tok", "Title", "YES", 0.97, 1.0, 1.03, "open")
        db.close_position("m2", -0.97)
        loss = db.daily_loss_today()
        assert abs(loss - 1.94) < 1e-6, loss
        _ok(f"C4: daily_loss_today() = ${loss:.2f} after 2 closed losses")


def test_passive_count_helper():
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        for i in range(3):
            db.insert_passive_order(
                market_id=f"m{i}", token_id="t", outcome="YES",
                price=0.97, size_usd=1.0,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=180),
                external_order_id=None, note="",
            )
        assert db.count_open_passive_orders() == 3
        _ok("C7: count_open_passive_orders() = 3")


def test_purge_old_rows():
    """I4: scanned_markets / forecasts retention works."""
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        db.insert_scanned_market(
            "m1", "title", None, 0.5, 0.5, 100.0, 100.0, True, 0.5, {},
        )
        # Backdate the row to simulate old data
        with db._conn() as c:
            c.execute(
                "UPDATE scanned_markets SET seen_at = ?",
                ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),),
            )
        deleted = db.purge_old_rows(retention_days=7)
        assert deleted == 1, deleted
        _ok(f"I4: purge_old_rows deleted {deleted} stale row(s)")


def test_threshold_picker_proximity():
    """I8: when 2 numbers carry units, picker prefers the one closest to a hint."""
    from polyweat.parser.threshold_parser import parse_threshold
    out = parse_threshold(
        "Forecast says 30°F, will the high actually exceed 80°F today?"
    )
    assert out["market_kind"] == "highest_gte"
    assert out["threshold_value"] == 80.0, out
    _ok("I8: picker selects 80F (near 'exceed') over 30F (near 'forecast')")


# =====================================================================
# v2 regression tests (post-second-pass review)
# =====================================================================

def test_v2c1_longshot_flow():
    """V2-C1: when ALLOW_LONGSHOT=true, decision flags is_longshot AND
    pre_trade_checks accepts the lower price band."""
    from polyweat.config import load_config
    from polyweat.db import Database
    from polyweat.strategy.decision import make_decision
    from polyweat.trading.risk import pre_trade_checks

    cfg = load_config()
    cfg.allow_longshot = True

    pm = _build_pm_yes()
    fc = _build_fc(31.0)
    # Tight spread around 0.40 so spread/liquidity gates pass.
    book = _build_book(best_ask=0.40, best_bid=0.396, liquidity=600.0)
    td = make_decision(
        pm, fc, book, None, cfg,
        has_open_position=False, open_positions_count=0,
        open_passive_count=0,
        daily_loss_so_far_usd=0.0,
    )
    assert td.decision == "ENTER", f"got {td.decision} ({td.skip_reason})"
    assert td.is_longshot is True
    assert td.proposed_price == 0.40

    # And the post-decision risk gate must accept it.
    with tempfile.TemporaryDirectory() as tdir:
        db = Database(Path(tdir) / "test.db")
        chk = pre_trade_checks(td, db, cfg)
        assert chk.ok, f"longshot rejected: {chk.reason}"
    _ok("V2-C1: ALLOW_LONGSHOT=true -> decision ENTER + risk gate accepts")


def test_v2c2_inflight_blocks_duplicate():
    """V2-C2: a leftover live `submitted` order blocks a re-entry on the
    same market."""
    from polyweat.config import load_config
    from polyweat.db import Database
    from polyweat.models import TradeDecision
    from polyweat.trading.risk import pre_trade_checks

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        # Leave a stale live order behind (as if a prior process crashed)
        db.insert_order(
            market_id="m-dup", token_id="tok", outcome="YES",
            side="BUY", order_type="LIMIT",
            price=0.97, size_usd=1.0, size_shares=1.03,
            status="submitted", dry_run=False,
            external_order_id="ext-123", note="legacy",
        )
        td = TradeDecision(
            market_id="m-dup", title="t", city=None, target_date=None,
            market_kind="highest_gte", threshold_c=None, threshold_f=None,
            outcome="YES", forecast_value_c=None, temp_distance_c=None,
            bot_probability=None, confidence_score=None, market_price=None,
            spread_percent=None, liquidity_usd=None,
            decision="ENTER", skip_reason=None,
            timestamp=datetime.now(timezone.utc),
            proposed_price=0.97, proposed_size_usd=1.0, token_id="tok",
        )
        chk = pre_trade_checks(td, db, cfg)
        assert not chk.ok
        assert chk.reason == "inflight_order_exists"
    _ok("V2-C2: inflight live order blocks duplicate via pre_trade_checks")


def test_v2c3_premature_close_skipped():
    """V2-C3: a market with closed=True but no clean winner is NOT
    closed locally; the position stays open."""
    from polyweat.db import Database
    from polyweat.trading.trader import _reconcile_positions

    class _FakeGamma:
        # closed=True but prices are still in flight (0.6/0.4 == not settled)
        def fetch_market(self, _mid):
            return {"closed": True}

        @staticmethod
        def normalize(_raw):
            return {
                "outcomes": ["Yes", "No"],
                "prices": [0.6, 0.4],
            }

    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        db.upsert_position("m1", "t1", "Title", "YES", 0.97, 1.0, 1.03, "open")
        n = _reconcile_positions(db, _FakeGamma())
        assert n == 0, "should leave the position open until prices settle"
        assert db.has_open_position("m1") is True
    _ok("V2-C3: closed=True without 0.99/0.01 prices -> position stays open")


def test_v2c3_clean_settlement_closes():
    """V2-C3 happy-path: clean 1.0/0.0 settlement closes correctly."""
    from polyweat.db import Database
    from polyweat.trading.trader import _reconcile_positions

    class _FakeGamma:
        def fetch_market(self, _mid):
            return {"closed": True}

        @staticmethod
        def normalize(_raw):
            return {
                "outcomes": ["Yes", "No"],
                "prices": [1.0, 0.0],
            }

    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        db.upsert_position("m1", "t1", "Title", "YES", 0.97, 1.0, 1.03, "open")
        n = _reconcile_positions(db, _FakeGamma())
        assert n == 1, f"expected 1 close, got {n}"
        assert db.has_open_position("m1") is False
        # We won so realized PnL is positive ~$0.0309 -> daily loss = 0.
        assert abs(db.daily_loss_today() - 0.0) < 1e-6
    _ok("V2-C3: clean YES win -> position closed, no daily loss recorded")


def test_v2c3_mismatched_arrays_left_open():
    """V2-C3: outcomes/prices array length mismatch leaves the position open."""
    from polyweat.db import Database
    from polyweat.trading.trader import _reconcile_positions

    class _FakeGamma:
        def fetch_market(self, _mid):
            return {"closed": True}

        @staticmethod
        def normalize(_raw):
            # outcomes shorter than prices -> mismatch
            return {"outcomes": ["Yes"], "prices": [1.0, 0.0]}

    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        db.upsert_position("m1", "t1", "Title", "YES", 0.97, 1.0, 1.03, "open")
        n = _reconcile_positions(db, _FakeGamma())
        assert n == 0
        assert db.has_open_position("m1") is True
    _ok("V2-C3: mismatched outcomes/prices arrays leave position open")


def test_v2i2_one_bad_market_doesnt_starve_others():
    """V2-I2: a malformed market for one position must not block the others."""
    from polyweat.db import Database
    from polyweat.trading.trader import _reconcile_positions

    class _FakeGamma:
        def fetch_market(self, market_id):
            return {"closed": True}

        @staticmethod
        def normalize(raw):
            # First call raises (simulates a malformed Gamma row).
            # Second call returns a clean settlement.
            _FakeGamma._calls += 1
            if _FakeGamma._calls == 1:
                raise ValueError("simulated bad market")
            return {"outcomes": ["Yes", "No"], "prices": [1.0, 0.0]}

        _calls = 0

    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        db.upsert_position("m_bad", "t1", "Title", "YES", 0.97, 1.0, 1.03, "open")
        db.upsert_position("m_good", "t2", "Title", "YES", 0.97, 1.0, 1.03, "open")
        n = _reconcile_positions(db, _FakeGamma())
        # The bad one is left open, the good one is closed.
        assert n == 1
        assert db.has_open_position("m_bad") is True
        assert db.has_open_position("m_good") is False
    _ok("V2-I2: bad market doesn't starve good markets in reconciliation")


def test_v2i3_decisions_in_retention():
    """V2-I3: decisions table is in the retention sweep."""
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        # Insert an old decision row.
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        db.insert_decision({
            "market_id": "m-old", "title": "t", "city": "X",
            "target_date": None, "market_kind": "highest_gte",
            "threshold_c": 26.7, "threshold_f": 80.0, "outcome": "YES",
            "forecast_value_c": 31.0, "temp_distance_c": 4.3,
            "bot_probability": 0.97, "confidence_score": 0.95,
            "market_price": 0.97, "spread_percent": 1.0, "liquidity_usd": 500.0,
            "decision": "SKIP", "skip_reason": "test",
            "hours_to_resolution": 5.0, "proposed_price": None,
            "proposed_size_usd": None, "token_id": None,
            "timestamp": old,
        })
        with db._conn() as c:
            c.execute("UPDATE decisions SET timestamp = ?", (old,))
        deleted = db.purge_old_rows(retention_days=7)
        assert deleted >= 1, deleted
    _ok("V2-I3: decisions purged by retention sweep")


def test_v2i4_range_regex_no_time_match():
    """V2-I4: range regex no longer false-matches time strings."""
    from polyweat.parser.threshold_parser import parse_threshold

    # No unit on either number -> not a range
    out = parse_threshold("Heatwave between 7am and 9am tomorrow?")
    assert out["market_kind"] != "range", out
    out = parse_threshold("Forecast for between 1 and 3 PM in NYC")
    assert out["market_kind"] != "range", out
    # Unit on at least one number -> still a range
    out = parse_threshold("Heatwave between 95F and 100F next week?")
    assert out["market_kind"] == "range", out
    _ok("V2-I4: range regex requires explicit temperature unit")


def test_v2i6_entries_count_after_order():
    """V2-I6: SKIP decisions don't bump entries_count; trader bumps it
    explicitly when an order is accepted."""
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        db.insert_decision({
            "market_id": "m1", "title": "t", "city": None,
            "target_date": None, "market_kind": "unknown",
            "threshold_c": None, "threshold_f": None, "outcome": "SKIP",
            "forecast_value_c": None, "temp_distance_c": None,
            "bot_probability": None, "confidence_score": None,
            "market_price": None, "spread_percent": None, "liquidity_usd": None,
            "decision": "SKIP", "skip_reason": "test",
            "hours_to_resolution": None, "proposed_price": None,
            "proposed_size_usd": None, "token_id": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        rows = db.get_daily_stats(days=1)
        assert rows[0]["entries_count"] == 0
        assert rows[0]["skips_count"] == 1
        # An ENTER decision alone does NOT bump entries (it's the trader's job).
        db.insert_decision({
            "market_id": "m2", "title": "t", "city": None,
            "target_date": None, "market_kind": "highest_gte",
            "threshold_c": None, "threshold_f": None, "outcome": "YES",
            "forecast_value_c": None, "temp_distance_c": None,
            "bot_probability": None, "confidence_score": None,
            "market_price": None, "spread_percent": None, "liquidity_usd": None,
            "decision": "ENTER", "skip_reason": None,
            "hours_to_resolution": None, "proposed_price": 0.97,
            "proposed_size_usd": 1.0, "token_id": "tok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        rows = db.get_daily_stats(days=1)
        assert rows[0]["entries_count"] == 0, "entries should be bumped by trader, not insert_decision"
        # Now trader confirms the order - bump.
        db.bump_entries_count()
        rows = db.get_daily_stats(days=1)
        assert rows[0]["entries_count"] == 1
    _ok("V2-I6: entries_count is bumped only when trader confirms order")


def test_v2i7_set_passive_external_id():
    """V2-I7: public API exists for updating passive_orders.external_order_id."""
    from polyweat.db import Database
    with tempfile.TemporaryDirectory() as td_:
        db = Database(Path(td_) / "test.db")
        po_pk = db.insert_passive_order(
            market_id="m1", token_id="t", outcome="YES",
            price=0.97, size_usd=1.0,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=180),
            external_order_id=None, note="",
        )
        db.set_passive_order_external_id(po_pk, "ext-xyz", "live submit")
        row = db.find_passive_order_by_external_id("ext-xyz")
        assert row is not None
        assert row["external_order_id"] == "ext-xyz"
    _ok("V2-I7: set_passive_order_external_id is public")


def test_v2i8_open_passive_count_required():
    """V2-I8: open_passive_count is keyword-only required (not optional)."""
    import inspect
    from polyweat.strategy.decision import make_decision
    sig = inspect.signature(make_decision)
    p = sig.parameters["open_passive_count"]
    assert p.default is inspect.Parameter.empty, (
        "open_passive_count should not have a default value"
    )
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    _ok("V2-I8: open_passive_count is keyword-only required")


# =====================================================================
# Runner
# =====================================================================

def main() -> int:
    print("Polyweat smoke tests")
    print("=" * 50)

    print("\n[Happy-path]")
    print("-" * 50)
    test_config()
    test_threshold_parser()
    test_city_parser()
    test_date_parser()
    test_filter()
    test_predictor_probability_curve()
    test_predictor_decision()
    test_decision_engine_happy_path()
    with tempfile.TemporaryDirectory() as td:
        test_db_roundtrip(Path(td) / "test.db")

    print("\n[Failure-path / regression]")
    print("-" * 50)
    test_get_bool_failsafe()
    test_threshold_word_boundary()
    test_threshold_range_market()
    test_decision_skips_range()
    test_decision_passive_cap()
    test_decision_f_distance_gate()
    test_decision_skip_reasons_complete()
    test_daily_loss_cap()
    test_passive_count_helper()
    test_purge_old_rows()
    test_threshold_picker_proximity()

    print("\n[v2 regressions]")
    print("-" * 50)
    test_v2c1_longshot_flow()
    test_v2c2_inflight_blocks_duplicate()
    test_v2c3_premature_close_skipped()
    test_v2c3_clean_settlement_closes()
    test_v2c3_mismatched_arrays_left_open()
    test_v2i2_one_bad_market_doesnt_starve_others()
    test_v2i3_decisions_in_retention()
    test_v2i4_range_regex_no_time_match()
    test_v2i6_entries_count_after_order()
    test_v2i7_set_passive_external_id()
    test_v2i8_open_passive_count_required()

    print("\n" + "=" * 50)
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
