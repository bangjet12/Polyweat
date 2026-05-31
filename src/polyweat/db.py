"""SQLite persistence layer for Polyweat.

Schema is created idempotently on first call. All tables are append-only
except `positions`, `watchlist`, `passive_orders`, and `daily_stats`.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from polyweat.logger import get_logger

log = get_logger("db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned_markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    title TEXT,
    end_time TEXT,
    yes_price REAL,
    no_price REAL,
    liquidity_usd REAL,
    volume_usd REAL,
    is_weather INTEGER,
    parse_score REAL,
    raw_json TEXT,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scanned_market_id ON scanned_markets(market_id);
CREATE INDEX IF NOT EXISTS idx_scanned_seen_at ON scanned_markets(seen_at);

CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT,
    lat REAL,
    lon REAL,
    tz TEXT,
    daily_high_c REAL,
    daily_low_c REAL,
    window_high_c REAL,
    window_low_c REAL,
    raw_json TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_city ON forecasts(city);

CREATE TABLE IF NOT EXISTS watchlist (
    market_id TEXT PRIMARY KEY,
    title TEXT,
    city TEXT,
    target_date TEXT,
    market_kind TEXT,
    threshold_c REAL,
    threshold_f REAL,
    outcome TEXT,
    bot_probability REAL,
    confidence_score REAL,
    last_market_price REAL,
    last_spread_percent REAL,
    last_liquidity_usd REAL,
    reason TEXT,
    added_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    title TEXT,
    city TEXT,
    target_date TEXT,
    market_kind TEXT,
    threshold_c REAL,
    threshold_f REAL,
    outcome TEXT,
    forecast_value_c REAL,
    temp_distance_c REAL,
    bot_probability REAL,
    confidence_score REAL,
    market_price REAL,
    spread_percent REAL,
    liquidity_usd REAL,
    decision TEXT NOT NULL,
    skip_reason TEXT,
    hours_to_resolution REAL,
    proposed_price REAL,
    proposed_size_usd REAL,
    token_id TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_market ON decisions(market_id);
CREATE INDEX IF NOT EXISTS idx_decision_ts ON decisions(timestamp);

CREATE TABLE IF NOT EXISTS passive_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT,
    outcome TEXT,
    price REAL,
    size_usd REAL,
    status TEXT NOT NULL, -- 'open' | 'filled' | 'expired' | 'cancelled'
    placed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    external_order_id TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_passive_status ON passive_orders(status);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT,
    outcome TEXT,
    side TEXT NOT NULL, -- 'BUY' | 'SELL'
    order_type TEXT NOT NULL, -- 'LIMIT' | 'MARKET' | 'PASSIVE'
    price REAL,
    size_usd REAL,
    size_shares REAL,
    status TEXT NOT NULL, -- 'submitted' | 'filled' | 'partial' | 'cancelled' | 'rejected' | 'simulated'
    external_order_id TEXT,
    dry_run INTEGER NOT NULL,
    placed_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL UNIQUE,
    token_id TEXT,
    title TEXT,
    outcome TEXT,
    entry_price REAL,
    size_usd REAL,
    size_shares REAL,
    status TEXT NOT NULL, -- 'open' | 'closed'
    pnl_usd REAL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT PRIMARY KEY,
    decisions_count INTEGER DEFAULT 0,
    entries_count INTEGER DEFAULT 0,
    skips_count INTEGER DEFAULT 0,
    realized_pnl_usd REAL DEFAULT 0.0,
    fees_paid_usd REAL DEFAULT 0.0,
    last_update TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    title TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejected_market ON rejected_markets(market_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_str() -> str:
    """Return today's date as ISO YYYY-MM-DD in UTC.

    UTC is intentional: daily stats / loss caps must reset at the same wall
    clock for every operator regardless of VPS timezone. systemd also
    pins ``TZ=UTC``.
    """
    return datetime.now(timezone.utc).date().isoformat()


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ----- internal -----

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL allows the CLI (`polyweat decisions`, `polyweat positions`) to
        # read while the runner is writing without hitting SQLITE_BUSY.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError:  # pragma: no cover
            pass
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ----- scanned markets -----

    def insert_scanned_market(
        self,
        market_id: str,
        title: str,
        end_time: Optional[datetime],
        yes_price: Optional[float],
        no_price: Optional[float],
        liquidity_usd: float,
        volume_usd: float,
        is_weather: bool,
        parse_score: float,
        raw: Dict[str, Any],
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO scanned_markets
                   (market_id, title, end_time, yes_price, no_price,
                    liquidity_usd, volume_usd, is_weather, parse_score,
                    raw_json, seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    market_id, title, _to_iso(end_time), yes_price, no_price,
                    liquidity_usd, volume_usd, 1 if is_weather else 0,
                    parse_score, json.dumps(raw)[:200_000], _now_iso(),
                ),
            )

    # ----- forecasts -----

    def insert_forecast(
        self,
        city: str,
        lat: float,
        lon: float,
        tz: str,
        daily_high_c: Optional[float],
        daily_low_c: Optional[float],
        window_high_c: Optional[float],
        window_low_c: Optional[float],
        raw: Dict[str, Any],
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO forecasts
                   (city, lat, lon, tz, daily_high_c, daily_low_c,
                    window_high_c, window_low_c, raw_json, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    city, lat, lon, tz, daily_high_c, daily_low_c,
                    window_high_c, window_low_c, json.dumps(raw)[:200_000],
                    _now_iso(),
                ),
            )

    # ----- watchlist -----

    def upsert_watchlist(
        self,
        market_id: str,
        title: str,
        city: Optional[str],
        target_date: Optional[datetime],
        market_kind: str,
        threshold_c: Optional[float],
        threshold_f: Optional[float],
        outcome: Optional[str],
        bot_probability: Optional[float],
        confidence_score: Optional[float],
        last_market_price: Optional[float],
        last_spread_percent: Optional[float],
        last_liquidity_usd: Optional[float],
        reason: str,
    ) -> None:
        now = _now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO watchlist (
                       market_id, title, city, target_date, market_kind,
                       threshold_c, threshold_f, outcome, bot_probability,
                       confidence_score, last_market_price, last_spread_percent,
                       last_liquidity_usd, reason, added_at, last_checked_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                       title=excluded.title,
                       city=excluded.city,
                       target_date=excluded.target_date,
                       market_kind=excluded.market_kind,
                       threshold_c=excluded.threshold_c,
                       threshold_f=excluded.threshold_f,
                       outcome=excluded.outcome,
                       bot_probability=excluded.bot_probability,
                       confidence_score=excluded.confidence_score,
                       last_market_price=excluded.last_market_price,
                       last_spread_percent=excluded.last_spread_percent,
                       last_liquidity_usd=excluded.last_liquidity_usd,
                       reason=excluded.reason,
                       last_checked_at=excluded.last_checked_at""",
                (
                    market_id, title, city, _to_iso(target_date), market_kind,
                    threshold_c, threshold_f, outcome, bot_probability,
                    confidence_score, last_market_price, last_spread_percent,
                    last_liquidity_usd, reason, now, now,
                ),
            )

    def list_watchlist(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT * FROM watchlist ORDER BY last_checked_at DESC"
                )
            )

    def remove_from_watchlist(self, market_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM watchlist WHERE market_id = ?", (market_id,))

    # ----- decisions -----

    def insert_decision(self, d: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO decisions (
                       market_id, title, city, target_date, market_kind,
                       threshold_c, threshold_f, outcome, forecast_value_c,
                       temp_distance_c, bot_probability, confidence_score,
                       market_price, spread_percent, liquidity_usd,
                       decision, skip_reason, hours_to_resolution,
                       proposed_price, proposed_size_usd, token_id, timestamp
                   ) VALUES (
                       :market_id,:title,:city,:target_date,:market_kind,
                       :threshold_c,:threshold_f,:outcome,:forecast_value_c,
                       :temp_distance_c,:bot_probability,:confidence_score,
                       :market_price,:spread_percent,:liquidity_usd,
                       :decision,:skip_reason,:hours_to_resolution,
                       :proposed_price,:proposed_size_usd,:token_id,:timestamp
                   )""",
                d,
            )
        # bump daily counters
        self._bump_daily(decisions=1, entries=1 if d.get("decision") == "ENTER" else 0,
                        skips=1 if d.get("decision") == "SKIP" else 0)

    def list_decisions(self, limit: int = 50) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT * FROM decisions ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

    # ----- passive orders -----

    def insert_passive_order(
        self,
        market_id: str,
        token_id: Optional[str],
        outcome: str,
        price: float,
        size_usd: float,
        expires_at: datetime,
        external_order_id: Optional[str],
        note: str = "",
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO passive_orders
                   (market_id, token_id, outcome, price, size_usd, status,
                    placed_at, expires_at, external_order_id, note)
                   VALUES (?,?,?,?,?,'open',?,?,?,?)""",
                (
                    market_id, token_id, outcome, price, size_usd,
                    _now_iso(), _to_iso(expires_at), external_order_id, note,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_open_passive_orders(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute("SELECT * FROM passive_orders WHERE status = 'open'")
            )

    def count_open_passive_orders(self) -> int:
        """Number of passive orders that are still pending. They obligate
        capital and a fill makes them effective positions, so we count them
        toward MAX_OPEN_POSITIONS as well."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM passive_orders WHERE status = 'open'"
            ).fetchone()
            return int(row["n"] if row else 0)

    def update_passive_order_status(
        self, order_pk: int, status: str, note: str = ""
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE passive_orders
                   SET status = ?, closed_at = ?, note = COALESCE(NULLIF(?,''), note)
                   WHERE id = ?""",
                (status, _now_iso(), note, order_pk),
            )

    # ----- orders -----

    def insert_order(
        self,
        market_id: str,
        token_id: Optional[str],
        outcome: str,
        side: str,
        order_type: str,
        price: float,
        size_usd: float,
        size_shares: float,
        status: str,
        dry_run: bool,
        external_order_id: Optional[str] = None,
        note: str = "",
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO orders
                   (market_id, token_id, outcome, side, order_type,
                    price, size_usd, size_shares, status, external_order_id,
                    dry_run, placed_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    market_id, token_id, outcome, side, order_type, price,
                    size_usd, size_shares, status, external_order_id,
                    1 if dry_run else 0, _now_iso(), note,
                ),
            )
            return int(cur.lastrowid or 0)

    # ----- positions -----

    def upsert_position(
        self,
        market_id: str,
        token_id: Optional[str],
        title: str,
        outcome: str,
        entry_price: float,
        size_usd: float,
        size_shares: float,
        status: str = "open",
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO positions
                   (market_id, token_id, title, outcome, entry_price,
                    size_usd, size_shares, status, opened_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                       outcome = excluded.outcome,
                       entry_price = excluded.entry_price,
                       size_usd = excluded.size_usd,
                       size_shares = excluded.size_shares,
                       status = excluded.status""",
                (
                    market_id, token_id, title, outcome, entry_price,
                    size_usd, size_shares, status, _now_iso(),
                ),
            )

    def has_open_position(self, market_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM positions WHERE market_id = ? AND status = 'open'",
                (market_id,),
            ).fetchone()
            return row is not None

    def count_open_positions(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE status = 'open'"
            ).fetchone()
            return int(row["n"] if row else 0)

    def list_open_positions(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
                )
            )

    def get_position(self, market_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE market_id = ?", (market_id,)
            ).fetchone()

    def find_passive_order_by_external_id(
        self, external_order_id: str
    ) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM passive_orders WHERE external_order_id = ?",
                (external_order_id,),
            ).fetchone()

    def update_order_status(
        self,
        order_pk: int,
        *,
        status: str,
        external_order_id: Optional[str] = None,
        note: str = "",
    ) -> None:
        """Update a row in ``orders``. Used to flip 'pending' -> 'submitted'
        once the live exchange has acknowledged the order."""
        with self._conn() as c:
            c.execute(
                """UPDATE orders
                   SET status = ?,
                       external_order_id = COALESCE(?, external_order_id),
                       note = COALESCE(NULLIF(?, ''), note),
                       closed_at = CASE WHEN ? IN ('filled','cancelled','rejected')
                                        THEN ? ELSE closed_at END
                   WHERE id = ?""",
                (status, external_order_id, note, status, _now_iso(), order_pk),
            )

    def close_position(self, market_id: str, pnl_usd: float) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE positions
                   SET status='closed', pnl_usd=?, closed_at=?
                   WHERE market_id = ?""",
                (pnl_usd, _now_iso(), market_id),
            )
        self._bump_daily(realized_pnl=pnl_usd)

    # ----- daily stats -----

    def _bump_daily(
        self,
        decisions: int = 0,
        entries: int = 0,
        skips: int = 0,
        realized_pnl: float = 0.0,
        fees: float = 0.0,
    ) -> None:
        day = _today_str()
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_stats(day, decisions_count, entries_count,
                                           skips_count, realized_pnl_usd,
                                           fees_paid_usd, last_update)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET
                       decisions_count = decisions_count + excluded.decisions_count,
                       entries_count   = entries_count   + excluded.entries_count,
                       skips_count     = skips_count     + excluded.skips_count,
                       realized_pnl_usd= realized_pnl_usd+ excluded.realized_pnl_usd,
                       fees_paid_usd   = fees_paid_usd   + excluded.fees_paid_usd,
                       last_update     = excluded.last_update""",
                (day, decisions, entries, skips, realized_pnl, fees, _now_iso()),
            )

    def daily_loss_today(self) -> float:
        """Return today's *loss* magnitude (positive = loss). 0 if profit."""
        with self._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM daily_stats WHERE day = ?",
                (_today_str(),),
            ).fetchone()
            if not row:
                return 0.0
            pnl = float(row["realized_pnl_usd"] or 0.0)
            return max(0.0, -pnl)

    def get_daily_stats(self, days: int = 7) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(
                c.execute(
                    "SELECT * FROM daily_stats ORDER BY day DESC LIMIT ?",
                    (days,),
                )
            )

    # ----- rejected -----

    def insert_rejected(self, market_id: str, title: str, reason: str) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO rejected_markets(market_id, title, reason, created_at)
                   VALUES (?,?,?,?)""",
                (market_id, title, reason, _now_iso()),
            )

    # ----- maintenance -----

    def purge_old_rows(self, retention_days: int) -> int:
        """Delete rows older than ``retention_days`` from high-churn tables.

        Keeps the SQLite file from growing without bound when the bot has
        been running for a while.
        Returns the total number of deleted rows.
        """
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        deleted = 0
        with self._conn() as c:
            for table, ts_col in (
                ("scanned_markets", "seen_at"),
                ("forecasts", "fetched_at"),
                ("rejected_markets", "created_at"),
            ):
                cur = c.execute(
                    f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
                )
                deleted += cur.rowcount or 0
        return deleted
