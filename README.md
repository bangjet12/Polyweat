# Polyweat

A small, opinionated trading bot for **Polymarket weather / temperature markets only**. It targets *high-confidence, low-stakes* entries (95¢–98.5¢) and refuses to touch sports, politics, crypto, finance, culture or news markets.

> **No profit guarantees.** Markets can resolve against you. Forecasts are imperfect. Use small size and read the risk section. The bot is designed to skip far more often than it enters.

---

## 1. Strategy in one page

The bot does **only one thing**: buy `YES` or `NO` shares on Polymarket weather/temperature markets when:

- the market clearly asks about a high or low temperature crossing a threshold,
- the public weather forecast (Open-Meteo) sits *far* from that threshold,
- the orderbook ask is in the **0.95 – 0.985** band, with tight spread and adequate liquidity,
- the resolution is < 18 hours away (preferably < 12h).

If any of that fails, it does **not** trade. It either parks the market on a watchlist or skips it.

| Gate | Minimum | Preferred |
|------|---------|-----------|
| Bot probability | 0.93 | 0.95 |
| Confidence score | 0.90 | 0.95 |
| Forecast distance from threshold | 2.0 °C / 3.5 °F | 3.0 °C / 5.0 °F |
| Time to resolution | ≤ 18h | ≤ 12h (best ≤ 6h) |
| Spread | ≤ 1.5% | ≤ 1.0% |
| Liquidity | ≥ $250 | ≥ $500 |
| Entry price (ask) | ≥ 0.95 | – |
| Max entry price | ≤ 0.985 | – |

Risk caps:

- Max position per market: **$1**
- Max open positions: **5** (counts both filled positions *and* in-flight passive limit orders)
- Max daily realised loss: **$5** — enforced via position reconciliation: each scan cycle the bot fetches resolved markets from Gamma, computes realised PnL, marks the position closed, and that PnL feeds the daily-loss gate
- No martingale / no averaging-down / no all-in / no chase above 0.985
- Range markets ("between 70°F and 80°F") and exact-temperature markets are recognised but **skipped** by default
- Word-boundary parsing - so "Will the LOW be HIGHER than X°F?" is correctly classified as `lowest_gt`, not `highest_gte`

If the ask sits just above 0.985, the bot can post a **passive limit buy** in the 0.95 – 0.975 band that automatically cancels after `PASSIVE_ORDER_EXPIRE_SECONDS` (default 180s).

The probability model is a simple, monotonic distance curve:

    p = 1 - 0.5 * exp(-distance_in_celsius / 1.0)

So 2 °C of forecast cushion ≈ 93%, 3 °C ≈ 97.5%, 5 °C ≈ 99.7% (capped at 0.999). Confidence multiplies that with how clean the parse was, how close to resolution we are, and how stable the hourly forecast is.

The bot **never** uses Claude / OpenAI / any LLM, never uses any paid AI. Only:

- Polymarket Gamma API (markets metadata + closed-market resolution)
- Polymarket CLOB API (orderbook + live orders)
- Open-Meteo (free, keyless forecast + geocoding)
- SQLite (local storage; WAL mode, UTC daily key, 7-day rolling retention)

---

## 2. Install on Ubuntu VPS

The repo ships an installer that creates a system user, a virtualenv and a systemd unit.

```bash
# As root or with sudo:
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/bangjet12/Polyweat.git /opt/polyweat-src
sudo bash /opt/polyweat-src/deploy/install.sh
```

The installer is idempotent. By default it clones `main` into `/opt/polyweat` and installs everything under the `polyweat` system user.

If you prefer to install manually:

```bash
git clone https://github.com/bangjet12/Polyweat.git
cd Polyweat
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
pip install -e .
cp .env.example .env
polyweat init-db
```

---

## 3. Configure `.env`

Copy the example and edit:

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Critical knobs:

```ini
# Default = simulate everything, never place a real order.
DRY_RUN=true
LIVE_TRADING=false

# Open-Meteo needs no key.
WEATHER_PROVIDER=open_meteo
WEATHER_API_KEY=

# Only required for live mode (DRY_RUN=false AND LIVE_TRADING=true).
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
POLYMARKET_PRIVATE_KEY=
POLYMARKET_PROXY_ADDRESS=
```

All other thresholds (entry band, distance, spread, liquidity, risk caps, …) are documented inline in `.env.example`.

---

## 4. Run in DRY_RUN

Dry-run is the **default**. It scans markets, makes decisions, and *simulates* fills into the local SQLite. No order is sent to Polymarket.

```bash
# One-shot: a single scan cycle, prints counters
polyweat scan-once

# Continuous loop:
polyweat run
```

Logs print to stdout *and* to `./logs/polyweat.log` (rotating, 5 MB × 5).

---

## 5. Run in LIVE mode

You must explicitly enable **both** flags and provide credentials:

```ini
DRY_RUN=false
LIVE_TRADING=true
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_PROXY_ADDRESS=0x...
```

Install the optional live dependency:

```bash
pip install py-clob-client
```

(or use the `[live]` extra: `pip install -e .[live]`. The provided `deploy/install.sh` will install it automatically when it sees `LIVE_TRADING=true` in `.env`.)

Then start as usual:

```bash
polyweat run
```

The bot logs a loud warning on startup when it is actually live. If `py-clob-client` is missing, any required credential is empty, or `POLYMARKET_SIGNATURE_TYPE=2` is set without a `POLYMARKET_PROXY_ADDRESS`, it refuses to start (it will not silently fall back to dry-run).

> Crash-safe live ordering: each live order writes a `pending` row to local SQLite *before* hitting Polymarket. If the process dies between submission and the follow-up update, you will still have a breadcrumb that prevents double-entry.

> Start with the smallest possible capital. The defaults (`$1` per market, `$5` daily loss cap) are intentionally tiny.

---

## 6. Inspect the watchlist

Markets that *almost* qualify (right type, right horizon, but price too high or one gate just missed) are parked here for the next scan:

```bash
polyweat watchlist
```

---

## 7. Inspect open positions

```bash
polyweat positions
```

In dry-run, positions are simulated immediately at the proposed entry price.

---

## 8. Inspect PnL / daily stats

```bash
polyweat pnl
```

Shows decisions / entries / skips / realised PnL per day for the last 14 days, plus today's loss vs the daily cap.

---

## 9. Inspect logs

```bash
# Last 200 lines from the rotating file:
polyweat logs

# Or 1000 lines:
polyweat logs --lines 1000

# Or follow live with journalctl when running under systemd:
sudo journalctl -u polyweat -f
```

`polyweat decisions --limit 50` prints the most recent decision rows directly from SQLite.

---

## 10. Stop the bot

Foreground:

```bash
# Ctrl-C - the bot finishes the current cycle and exits cleanly.
```

Systemd:

```bash
sudo systemctl stop polyweat
```

---

## 11. Uninstall

```bash
sudo systemctl disable --now polyweat
sudo rm -f /etc/systemd/system/polyweat.service
sudo systemctl daemon-reload
sudo rm -rf /opt/polyweat
sudo userdel polyweat 2>/dev/null || true
```

The local SQLite + logs are gone after `rm -rf /opt/polyweat`.

---

## 12. 24/7 with systemd

The installer already drops `deploy/polyweat.service` into `/etc/systemd/system/`. Activate with:

```bash
sudo systemctl enable --now polyweat
sudo systemctl status polyweat
sudo journalctl -u polyweat -f
```

The unit:

- Runs as the unprivileged `polyweat` user
- Loads `/opt/polyweat/.env`
- Restarts on failure (`Restart=on-failure`, 10 s back-off, max 10 restarts/min)
- Runs with `NoNewPrivileges`, `ProtectSystem=strict`, only `data/` and `logs/` writable.

---

## Project layout

```
Polyweat/
├── deploy/
│   ├── install.sh             Ubuntu VPS installer
│   └── polyweat.service       systemd unit
├── src/
│   └── polyweat/
│       ├── __init__.py        version
│       ├── __main__.py        `python -m polyweat ...`
│       ├── cli.py             argparse subcommands
│       ├── config.py          typed Config from .env
│       ├── db.py              SQLite schema + helpers
│       ├── logger.py          rotating file logger
│       ├── models.py          dataclasses
│       ├── runner.py          scan/decide/trade loop
│       ├── api/
│       │   ├── _http.py       GET helper with retries
│       │   ├── gamma.py       Polymarket Gamma client
│       │   ├── clob.py        Polymarket CLOB orderbook client
│       │   └── open_meteo.py  Open-Meteo forecast + geocoding
│       ├── parser/
│       │   ├── cities.py      static alias -> (lat,lon,tz)
│       │   ├── city_parser.py extract_city() from text
│       │   ├── date_parser.py extract_date() from text
│       │   ├── threshold_parser.py threshold + market kind
│       │   └── filter.py      is_weather + parse_market
│       ├── strategy/
│       │   ├── predictor.py   probability model
│       │   ├── confidence.py  confidence scoring
│       │   └── decision.py    full decision engine
│       └── trading/
│           ├── risk.py        pre-trade gates
│           └── trader.py      DryRunTrader / LiveTrader
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## CLI quick reference

| Command | What it does |
|---------|--------------|
| `polyweat run` | main loop (default) |
| `polyweat scan-once` | one scan cycle, prints counters |
| `polyweat watchlist` | shows near-miss markets |
| `polyweat positions` | shows open positions |
| `polyweat decisions [--limit 30]` | recent decisions |
| `polyweat pnl` | daily stats / PnL |
| `polyweat logs [--lines 200]` | tails the log file |
| `polyweat init-db` | creates SQLite schema |
| `polyweat status` | prints config + DB summary |

All commands accept `--env /path/to/.env` to override the default `.env` lookup.

---

## Final notes

- The bot intentionally **skips** more than it enters. This is by design.
- "High confidence" does not mean guaranteed — it means the forecast is far from the threshold and several quality gates passed.
- Never rely on a single bot for size. Start tiny, watch the logs, and increase only after seeing many cycles behave the way you expect.
- Past performance, simulated or otherwise, does not predict future results.
