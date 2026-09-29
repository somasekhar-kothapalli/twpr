# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TWPR (The Weekly Petroleum Report) — a systematic weekly options setup on MCX
CrudeOil, the first setup in the TradeDesk platform. **Real money trades on this
repo's output.** Reliability and correctness over cleverness.

Options **buyer only**, never a seller. Max 2% of capital on a Grade A trade.

**The repo is mid-rewrite.** `feature/v0.1` currently has the rule engine, the
expiry gate, the currency module, the monitor/journal/scheduler layer, the
PetroCore client, and the entire test suite and docs removed from the working
tree (uncommitted, not staged). What's left is the three Tuesday/Wednesday data
fetchers, the two scrapers, and Telegram delivery. `signal_engine.py` exists as
an empty file and `telegram_bot.py` still expects to read `data/signal.json`
from it — that wiring is currently broken. The GitHub Actions workflows still
invoke `app/market_data.py` and `app/signal_engine.py`, which no longer exist /
no longer do anything. Don't assume the architecture described in old commits
or in `docs/` (also removed) is live; check `git log` and what's actually on
disk before relying on either.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env                     # then fill in the keys

python app/consensus_fetcher.py --crude -1.6 --gasoline 0.5 --distillate -0.3 --previous -2.0
python app/api_monitor.py --crude 1.25 --cushing 0.2 --gasoline 1.0 --distillate 0.5
python app/eia_parser.py --once      # single attempt instead of polling
python app/telegram_bot.py --test    # Telegram connectivity check
python app/telegram_bot.py --prebrief
```

There is currently no `tests/` directory and no `pytest.ini`-driven suite —
both were removed from the working tree. `requirements.txt` and `pytest.ini`
still list/configure pytest; if you reintroduce tests, `pytest.ini` already
excludes anything marked `network` by default (`-m "not network"`).

## Architecture (current, reduced)

Standalone scripts that pass state through JSON files in `data/`. No framework,
no shared process — each script runs alone, reads what it needs, writes one
file, and exits.

```
consensus_fetcher.py  -> data/consensus.json
api_monitor.py        -> data/api_report.json
eia_parser.py         -> data/eia_actual.json
telegram_bot.py       <- data/signal.json (writer no longer exists)
```

- **Shared helpers live in `app/common.py`**: `DATA_DIR`, `setup_logging`,
  `env`, `now_utc`, `now_ist`, `week_ending`, `read_json`, `write_json`.
- Scripts import each other as flat modules (`from common import ...`), which
  works because they all live in `app/` and are run from there.
- **Read every environment variable through `common.env()`, never
  `os.getenv`.** `python-dotenv` keeps an inline `# ...` comment as the value
  when the value is empty, so `EIA_API_KEY=   # free at eia.gov/opendata` sets
  the key to the comment text. `env()` treats blank and `#`-leading values as
  unset, turning a confusing downstream 403 into a clean "not set, falling
  back". A `#` inside a value (not leading) is kept.
- `setup_logging()` forces stdout/stderr to UTF-8 (alerts carry `₹` and emoji;
  a cp1252 Windows console raises `UnicodeEncodeError` without it) and
  silences `httpx`/`httpcore` to WARNING — httpx logs every request URL at
  INFO, and the Telegram Bot API carries the bot token in its path, so leaving
  it on writes the token into every log, including GitHub Actions run logs.
- All stock figures are **million barrels**, negative = draw. The EIA API
  returns thousand barrels; `eia_parser.py` divides by 1000 at the boundary.
- Timezones: `now_utc()` for stored timestamps, `now_ist()` for anything
  schedule- or session-related. Never a naive datetime.
- `week_ending()` returns the Friday the EIA report covers — the previous
  Friday from a Tuesday or Wednesday run.

### Consensus sources

Both scrapers exist and are tried in this order by `consensus_fetcher.py`,
`api_monitor.py` and `eia_parser.py`, after the explicit source each script
prefers (CLI flags, or the EIA API) and before the terminal prompt:

1. `tradingeconomics_scraper.py` — plain HTTP, ~1s. The calendar tables are in
   the served HTML. This is the primary.
2. `investing_scraper.py` — headless Chromium via Playwright, ~15s.
   investing.com returns 403 to plain HTTP on every route including its JSON
   endpoints, and Cloudflare challenges every page after the first, so it
   loads the calendar once and calls the calendar's own data service from
   inside that session.

Both scrapers expose `fetch_consensus(week=None)`, `fetch_eia_actuals(week=None)`
and `fetch_api_report(week=None)`, and both return `None` rather than a partial
dict. Two traps the parsers exist to avoid:

- **Blank cells are positional.** An empty `Actual` marks an unreleased row. A
  parser that filters blanks shifts Previous into Actual and reads last week's
  number as this week's.
- **Trading Economics' API summary table is undated.** Its "Last" column is
  always the newest release, so `fetch_api_report` refuses any week that is
  not the latest released one.

`investing_scraper.fetch_api_report()` always returns `None`: the US calendar
carries only the crude leg of the API report, so it cannot fill the four
fields `api_monitor.py` requires. Trading Economics is the only source for
that report. `investing_scraper.fetch_api_crude_only()` is for cross-checking
only, not part of the fetcher contract.

Neither scraper carries the refinery utilization percentage, so
`refinery_util_pct` is null on a scraped week.

`eia_parser.py` keeps the EIA API first when `EIA_API_KEY` is set — it is the
publisher — and falls back to the scrapers otherwise, so a Wednesday can run
without a key. The API derives stock *changes* from two weekly levels in
thousand barrels (`STOCK_SERIES` in `eia_parser.py`); the scrapers publish the
changes directly in million barrels.

Running the scrapers needs `python -m playwright install chromium` once; the
consensus and api_monitor workflows do it in CI.

### Telegram delivery

`telegram_bot.py` is also the shared notifier — `send_message` / `send_error`
are imported by other scripts rather than talking to the Bot API directly.
Messages are sent as plain text (no `parse_mode`) so premium figures and
underscores never need escaping. `format_signal()` renders a `signal.json`
dict (grade/direction/confidence/expiry/strike/drivers/risks) into the
Wednesday alert, including a skip-week and an expiry-skip variant — but
nothing in the current tree writes `data/signal.json`.

### GitHub Actions

Four workflows in `.github/workflows/` run on cron and **commit `data/` back to
the repo** — the runner is ephemeral, so committed JSON is how Wednesday's
signal path sees Tuesday's consensus. `data/active_trade.json`,
`journal.json` and `journal_export.csv` stay local and gitignored (their
producers, `monitor.py`/`journal.py`, are currently removed).

- `twpr_consensus.yml` — Tue 19:00 IST — `consensus_fetcher.py --no-prompt`
- `twpr_api_monitor.yml` — Wed 01:45 IST — `api_monitor.py --no-prompt`
- `twpr_eia_signal.yml` — Wed 20:00 IST — prebrief, `eia_parser.py`,
  `signal_engine.py` (missing), `telegram_bot.py`
- `twpr_market_data.yml` — 09:00 IST Mon–Fri — `market_data.py` (missing)

All four use `concurrency: group: twpr-data` so they can't race on the `data/`
commit.

## What's gone from the working tree (uncommitted)

Present in `git log` but deleted locally and not yet restored or replaced:

- `app/signal_engine.py` (emptied, not deleted — the 5-step rule engine),
  `app/expiry.py` (days-to-expiry gate), `app/currency.py` (INR context),
  `app/journal.py`, `app/market_data.py`, `app/monitor.py`,
  `app/petrocore_client.py`, `app/scheduler.py`
- `docs/twpr_setup_spec.md`, `docs/manual_testing.md`,
  `docs/claude_code_twpr_prompt.md`
- everything under `tests/`
- `README.md` content (file still exists, now empty)

`data/reference_week.json` still holds the pinned reference week (week ending
2026-09-04: crude deviation +1.209 mb → Grade B bearish, confidence 55,
1-OTM put, size 1.5%) — useful if the engine is rebuilt, since it's the number
that must reproduce exactly.
