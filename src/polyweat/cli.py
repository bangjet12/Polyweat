"""Command-line interface for Polyweat.

Subcommands:
    run             Main scan/decide/trade loop (default)
    scan-once       Run a single scan cycle and exit
    watchlist       Print the current watchlist
    positions       Print open positions
    decisions       Print recent decisions
    pnl             Print daily PnL/stats
    logs            Tail the polyweat log file
    init-db         Initialise the SQLite database and exit
    status          Print config + DB summary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Sequence

from polyweat import __version__
from polyweat.config import Config, load_config
from polyweat.db import Database
from polyweat.logger import setup_logging


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _table(rows: List[List[Any]], headers: Sequence[str]) -> str:
    if not rows:
        return f"({', '.join(headers)})\n  no rows"
    cols = list(headers)
    widths = [len(h) for h in cols]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(cols))
    sep = "  ".join("-" * widths[i] for i in range(len(cols)))
    body = "\n".join(
        "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r))
        for r in rows
    )
    return f"{line}\n{sep}\n{body}"


def _f(v: Any, fmt: str = "{:.4f}") -> str:
    if v is None or v == "":
        return "-"
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

def cmd_run(cfg: Config, _: argparse.Namespace) -> int:
    from polyweat.runner import Runner
    Runner(cfg).run_forever()
    return 0


def cmd_scan_once(cfg: Config, _: argparse.Namespace) -> int:
    from polyweat.runner import Runner
    counters = Runner(cfg).scan_once()
    print("\nscan_once results:")
    for k, v in counters.items():
        print(f"  {k:<24} {v}")
    return 0


def cmd_watchlist(cfg: Config, _: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    rows = db.list_watchlist()
    table = _table(
        [
            [
                r["market_id"][:14],
                (r["city"] or "-")[:14],
                (r["market_kind"] or "-")[:12],
                _f(r["threshold_f"], "{:.1f}F") + "/" + _f(r["threshold_c"], "{:.1f}C"),
                r["outcome"] or "-",
                _f(r["bot_probability"], "{:.3f}"),
                _f(r["confidence_score"], "{:.3f}"),
                _f(r["last_market_price"], "{:.3f}"),
                _f(r["last_spread_percent"], "{:.2f}%"),
                _f(r["last_liquidity_usd"], "${:.0f}"),
                (r["reason"] or "")[:40],
            ]
            for r in rows
        ],
        headers=[
            "market", "city", "kind", "threshold", "out",
            "prob", "conf", "price", "spread", "liq", "reason",
        ],
    )
    print(f"Watchlist ({len(rows)} markets)")
    print(table)
    return 0


def cmd_positions(cfg: Config, _: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    rows = db.list_open_positions()
    table = _table(
        [
            [
                r["market_id"][:14],
                (r["title"] or "-")[:50],
                r["outcome"] or "-",
                _f(r["entry_price"], "{:.4f}"),
                _f(r["size_usd"], "${:.2f}"),
                _f(r["size_shares"], "{:.2f}"),
                r["status"],
                r["opened_at"],
            ]
            for r in rows
        ],
        headers=[
            "market", "title", "out", "entry", "size$", "shares",
            "status", "opened_at",
        ],
    )
    print(f"Open positions ({len(rows)})")
    print(table)
    return 0


def cmd_decisions(cfg: Config, args: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    rows = db.list_decisions(limit=args.limit)
    table = _table(
        [
            [
                r["timestamp"][:19],
                r["market_id"][:12],
                (r["city"] or "-")[:12],
                r["decision"] or "-",
                r["outcome"] or "-",
                _f(r["bot_probability"], "{:.3f}"),
                _f(r["confidence_score"], "{:.3f}"),
                _f(r["market_price"], "{:.3f}"),
                _f(r["temp_distance_c"], "{:.2f}"),
                (r["skip_reason"] or "")[:34],
            ]
            for r in rows
        ],
        headers=[
            "ts", "market", "city", "dec", "out", "prob",
            "conf", "price", "dC", "reason",
        ],
    )
    print(f"Recent decisions (last {args.limit})")
    print(table)
    return 0


def cmd_pnl(cfg: Config, _: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    rows = db.get_daily_stats(days=14)
    table = _table(
        [
            [
                r["day"],
                r["decisions_count"],
                r["entries_count"],
                r["skips_count"],
                _f(r["realized_pnl_usd"], "${:+.2f}"),
                _f(r["fees_paid_usd"], "${:.2f}"),
            ]
            for r in rows
        ],
        headers=["day", "decisions", "entries", "skips", "pnl", "fees"],
    )
    print("Daily stats (most recent 14 days)")
    print(table)
    print(f"\nDaily loss limit (env): ${cfg.max_daily_loss_usd:.2f}")
    print(f"Today's loss so far:    ${db.daily_loss_today():.2f}")
    return 0


def cmd_logs(cfg: Config, args: argparse.Namespace) -> int:
    log_path = Path(cfg.log_dir) / "polyweat.log"
    if not log_path.exists():
        print(f"No log file at {log_path}")
        return 1
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    tail = lines[-args.lines:] if args.lines > 0 else lines
    sys.stdout.write("".join(tail))
    return 0


def cmd_init_db(cfg: Config, _: argparse.Namespace) -> int:
    Database(cfg.db_path)
    print(f"Database initialised at {cfg.db_path}")
    return 0


def cmd_status(cfg: Config, _: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    print(f"Polyweat v{__version__}")
    print(f"  mode               : {'LIVE' if cfg.is_live else 'DRY_RUN'} "
          f"(dry_run={cfg.dry_run} live_trading={cfg.live_trading})")
    print(f"  weather provider   : {cfg.weather_provider}")
    print(f"  scan interval      : {cfg.scan_interval_seconds}s "
          f"(fast: {cfg.fast_scan_interval_seconds}s)")
    print(f"  entry price band   : {cfg.min_entry_price:.3f} .. "
          f"{cfg.max_entry_price:.3f}")
    print(f"  resolve horizon    : <= {cfg.max_hours_to_resolution:.0f}h "
          f"(best <= {cfg.best_hours_to_resolution:.0f}h)")
    print(f"  prob/conf min      : {cfg.min_bot_probability:.2f} / "
          f"{cfg.min_confidence_score:.2f}")
    print(f"  temp distance min  : {cfg.min_temp_distance_c:.1f}C / "
          f"{cfg.min_temp_distance_f:.1f}F")
    print(f"  liquidity min      : ${cfg.min_liquidity_usd:.0f}")
    print(f"  max spread         : {cfg.max_spread_percent:.2f}%")
    print(f"  size cap           : ${cfg.max_position_per_market_usd:.2f} "
          f"per market, max {cfg.max_open_positions} open positions")
    print(f"  daily loss cap     : ${cfg.max_daily_loss_usd:.2f}")
    print(f"  passive limits     : {'on' if cfg.allow_passive_limit_orders else 'off'} "
          f"({cfg.passive_order_min_price:.3f}..{cfg.passive_order_max_price:.3f}, "
          f"expire {cfg.passive_order_expire_seconds}s)")
    print(f"  data dir           : {cfg.data_dir}")
    print(f"  db                 : {cfg.db_path}")
    print(f"  log dir            : {cfg.log_dir}")
    print(f"  open positions     : {db.count_open_positions()}")
    print(f"  watchlist size     : {len(db.list_watchlist())}")
    print(f"  today's loss       : ${db.daily_loss_today():.2f}")
    return 0


# ---------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polyweat",
        description=(
            "Polymarket weather/temperature trading bot - high-confidence, "
            "low-stakes. Defaults to DRY_RUN."
        ),
    )
    p.add_argument("--version", action="version", version=f"polyweat {__version__}")
    p.add_argument("--env", default=None, help="Path to .env file (optional)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Main scan/decide/trade loop")
    sub.add_parser("scan-once", help="Run a single scan cycle and exit")
    sub.add_parser("watchlist", help="Print the current watchlist")
    sub.add_parser("positions", help="Print open positions")
    sp_dec = sub.add_parser("decisions", help="Print recent decisions")
    sp_dec.add_argument("--limit", type=int, default=30)
    sub.add_parser("pnl", help="Print daily PnL/stats")
    sp_logs = sub.add_parser("logs", help="Tail the polyweat log file")
    sp_logs.add_argument("--lines", type=int, default=200)
    sub.add_parser("init-db", help="Initialise the SQLite database and exit")
    sub.add_parser("status", help="Print config + DB summary")
    return p


HANDLERS = {
    "run":         cmd_run,
    "scan-once":   cmd_scan_once,
    "watchlist":   cmd_watchlist,
    "positions":   cmd_positions,
    "decisions":   cmd_decisions,
    "pnl":         cmd_pnl,
    "logs":        cmd_logs,
    "init-db":     cmd_init_db,
    "status":      cmd_status,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    cfg = load_config(env_file=args.env)
    setup_logging(cfg.log_dir, cfg.log_level)
    handler = HANDLERS.get(cmd)
    if handler is None:
        parser.print_help()
        return 2
    return handler(cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
