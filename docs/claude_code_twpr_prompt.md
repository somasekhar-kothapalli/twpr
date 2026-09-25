# Claude Code — TWPR Build Prompt

## Context

You are building **TWPR (The Weekly Petroleum Report)** — the first setup
in **TradeDesk**, a personal systematic trading platform for MCX Crude Oil
options. You are working inside the `twpr` GitHub repository.

This is a production system. Real money trades on its output.
Build for reliability, clarity, and correctness over cleverness.

---

## System Identity

| Component | Name |
|---|---|
| Platform | TradeDesk |
| This repo | twpr (public) |
| Backend | PetroCore (private repo: petrocore) |
| Database | PetroStore (Turso SQLite) |
| Setup code | TWPR |

---

## Instrument

- **Exchange:** MCX (Multi Commodity Exchange of India)
- **Contract:** CrudeOil (near-month)
- **Lot size:** 100 barrels
- **Role:** Options BUYER only — never seller
- **Capital rule:** Max 2% of trading capital per Grade A trade

---

## What TWPR Does

Three data releases → one directional signal → one options trade.
Runs every Wednesday. 52 times per year.

```
Tuesday  19:00 IST  → consensus_fetcher.py   Reuters/Bloomberg survey
Wednesday 02:00 IST → api_monitor.py         API private inventory
Wednesday 20:00 IST → eia_parser.py          EIA WPSR actuals
                    → signal_engine.py       Signal via Groq AI
                    → telegram_bot.py        Telegram alert
```

---

## Repository Structure (current state)

```
twpr/
├── .github/
│   └── workflows/
│       ├── twpr_consensus.yml       ✅ exists
│       ├── twpr_api_monitor.yml     ✅ exists
│       ├── twpr_eia_signal.yml      ✅ exists
│       └── twpr_market_data.yml     ✅ exists
├── scripts/
│   ├── consensus_fetcher.py         ✅ exists — needs PetroCore integration
│   ├── api_monitor.py               ✅ exists — needs PetroCore integration
│   ├── eia_parser.py                ✅ exists — needs PetroCore integration
│   ├── signal_engine.py             ✅ exists — needs PetroCore integration
│   ├── telegram_bot.py              ✅ exists — needs PetroCore integration
│   ├── investing_scraper.py         ✅ exists — do not modify
│   ├── tradingeconomics_scraper.py  ✅ exists — do not modify
│   ├── petrocore_client.py          ❌ missing — build first
│   ├── market_data.py               ❌ missing — build
│   ├── scheduler.py                 ❌ missing — build
│   ├── monitor.py                   ❌ missing — build
│   └── journal.py                   ❌ missing — build
├── docs/
│   └── twpr_setup_spec.md           ✅ exists — source of truth
├── data/
│   ├── reference_week.json          ✅ exists
│   └── .gitkeep                     ✅ exists
├── tests/
│   └── test_signal_engine.py        ❌ missing — build
├── .env.example                     ✅ exists
├── .gitignore                       ✅ exists
├── requirements.txt                 ✅ exists — may need updating
└── README.md                        ✅ exists
```

---

## Environment Variables

All secrets via environment variables. Never hardcode.
Read with `os.getenv("VAR_NAME")` throughout.

```bash
# EIA API
EIA_API_KEY=                    # free at eia.gov/opendata

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# AI
GROQ_API_KEY=                   # free at console.groq.com
GROQ_MODEL=llama-3.3-70b-versatile
MODEL_MODE=groq                 # groq | rule_based | ollama
OLLAMA_URL=http://localhost:11434

# PetroCore
PETROCORE_URL=                  # optional — falls back to JSON files
PETROCORE_API_KEY=

# Angel One (future)
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_TOTP_SECRET=
```

---

## Build Task 1 — petrocore_client.py (shared module)

Build `scripts/petrocore_client.py` first. All other scripts import from it.

### Purpose
Shared HTTP client for posting data to PetroCore backend.
If `PETROCORE_URL` is not set, silently skip the POST and log a warning.
All scripts must work standalone without PetroCore (fallback: JSON file).

### Interface

```python
from petrocore_client import PetroCoreClient

client = PetroCoreClient()            # reads env vars

# Each method POSTs to the corresponding endpoint
client.post_consensus(payload: dict)  → POST /api/v1/twpr/consensus
client.post_api_report(payload: dict) → POST /api/v1/twpr/api-report
client.post_eia_report(payload: dict) → POST /api/v1/twpr/eia-report
client.post_signal(payload: dict)     → POST /api/v1/twpr/signal
client.post_market_data(payload: dict)→ POST /api/v1/market/daily
client.post_trade(payload: dict)      → POST /api/v1/twpr/trade
```

### Requirements
- Auth header: `X-API-Key: {PETROCORE_API_KEY}`
- Content-Type: `application/json`
- Timeout: 10 seconds
- Retry: 2 retries with 2-second backoff on 5xx errors
- On failure: log the error, do NOT raise — script continues
- Log every POST: URL, status code, response time
- Use `httpx` for HTTP (already in requirements.txt)

---

## Build Task 2 — market_data.py

### Purpose
Fetch daily market data from yfinance and EIA API.
Run daily at 09:00 IST on weekdays via GitHub Actions.

### Data to fetch

**From yfinance:**

| Ticker | Column | Description |
|---|---|---|
| `CL=F` | `wti_close` | WTI front month close |
| `BZ=F` | `brent_close` | Brent crude close |
| `DX-Y.NYB` | `dxy_close` | US Dollar Index |
| `INR=X` | `usd_inr_close` | USD/INR spot rate |
| `RB=F` | `rbob_close` | RBOB Gasoline ($/gallon) |
| `HO=F` | `heating_oil_close` | Heating Oil ($/gallon) |

**From EIA API v2:**

| Series | Column | Description |
|---|---|---|
| `RCLC1` | `wti_m1_price` | WTI front month (continuous) |
| `RCLC2` | `wti_m2_price` | WTI second month |

**Calculated:**
```python
wti_m1m2_spread  = wti_m1_price - wti_m2_price
                   # positive = backwardation (tight market)
                   # negative = contango (oversupplied)

crack_321        = (2 × rbob_close × 42 + 1 × heating_oil_close × 42
                    - 3 × wti_close) / 3
                   # 42 gallons per barrel conversion

brent_wti_spread = brent_close - wti_close

mcx_close        = wti_close × usd_inr_close   # approximate
mcx_source       = 'calculated'
```

### Logic
1. Fetch last 5 trading days from yfinance (catches missed days)
2. Fetch same period from EIA API RCLC1/RCLC2
3. Merge on date
4. Calculate derived columns
5. Save to `data/market_data.json`
6. POST to PetroCore via `petrocore_client.post_market_data()`

### EIA API endpoint
```
GET https://api.eia.gov/v2/petroleum/pri/fut/data/
    ?api_key={EIA_API_KEY}
    &frequency=daily
    &data[0]=value
    &facets[series][]=RCLC1
    &facets[series][]=RCLC2
    &start={start_date}
    &sort[0][column]=period
    &sort[0][direction]=desc
```

### Output JSON structure
```json
{
  "date": "2026-09-24",
  "wti_close": 71.45,
  "wti_m1_price": 71.45,
  "wti_m2_price": 70.89,
  "wti_m1m2_spread": 0.56,
  "brent_close": 75.12,
  "brent_wti_spread": 3.67,
  "dxy_close": 104.23,
  "usd_inr_close": 83.95,
  "crack_321": 18.45,
  "mcx_close": 5996.0,
  "mcx_source": "calculated",
  "source": "yfinance+eia_api",
  "fetched_at": "2026-09-25T03:32:00Z"
}
```

---

## Build Task 3 — scheduler.py

### Purpose
Local APScheduler-based scheduler that runs the complete TWPR Wednesday
pipeline in sequence. For local development and testing without
GitHub Actions.

### Schedule (all IST — UTC+5:30)

| Day | Time IST | Job |
|---|---|---|
| Mon–Fri | 09:00 | market_data.py |
| Tuesday | 19:00 | consensus_fetcher.py |
| Wednesday | 01:45 | api_monitor.py (starts polling) |
| Wednesday | 19:30 | telegram pre-brief |
| Wednesday | 20:00 | eia_parser.py (starts polling) |

### Requirements
- Use `APScheduler` with `BlockingScheduler`
- Timezone: `Asia/Kolkata`
- Each job runs the corresponding script as a subprocess
- Log start/end time and exit code for every job
- On job failure: send Telegram error alert, continue scheduler
- Print next 5 scheduled runs on startup

### Usage
```bash
python scripts/scheduler.py          # runs forever
python scripts/scheduler.py --test   # runs market_data immediately, exits
```

---

## Build Task 4 — monitor.py

### Purpose
Post-signal monitoring loop. Runs after `telegram_bot.py` sends the
signal alert. Polls every 60 seconds. Enforces the hard close at
22:30 IST. Sends Telegram alerts for stop/target/hard close events.

### Logic

```
On start:
  1. Read signal from data/signal.json
  2. If grade == 'skip' → exit immediately
  3. Ask user to confirm trade entry (y/n)
     - If n → exit, log as 'no_trade'
     - If y → ask for: lots, entry_premium, option_type, strike
  4. Save trade entry to data/active_trade.json
  5. Start monitoring loop

Monitoring loop (every 60 seconds):
  1. Get current MCX CrudeOil options premium (placeholder: manual input)
  2. Calculate P&L:
       pnl_pct = (current_premium - entry_premium) / entry_premium × 100
  3. Check exit conditions:
       pnl_pct <= -40       → STOP LOSS → exit all → send alert
       pnl_pct >= +50       → TARGET 1  → exit 50% → send alert, continue
       pnl_pct >= +100      → TARGET 2  → exit all → send alert, stop
       time >= 22:30 IST    → HARD CLOSE → exit all → send alert, stop
  4. Log current status every loop

On exit event:
  1. Record exit in data/active_trade.json
  2. POST to PetroCore via client.post_trade()
  3. Send Telegram exit summary
  4. Prompt user to confirm actual exit premium (for accuracy)
```

### Telegram alerts

```
🛑 STOP LOSS HIT
TWPR | Grade B Bearish
Entry: ₹820 | Current: ₹492
P&L: -40% | Exit all lots now

🎯 TARGET 1 HIT (+50%)
TWPR | Grade B Bearish
Entry: ₹820 | Current: ₹1,230
Exit 50% of position now

🏁 HARD CLOSE — 22:30 IST
TWPR | Exiting all remaining positions
Current P&L: +23%
```

### Note on live premium
Angel One SmartAPI integration is future scope. For now, monitor.py
prompts the user to manually enter the current premium each minute,
or press Enter to use the last value. Add a TODO comment for
SmartAPI integration.

---

## Build Task 5 — journal.py

### Purpose
Record completed trades, calculate performance statistics, and generate
weekly/monthly summaries. Standalone script — can run anytime.

### Commands
```bash
python scripts/journal.py log      # log a completed trade interactively
python scripts/journal.py week     # summary of current week
python scripts/journal.py month    # summary of current month
python scripts/journal.py stats    # all-time statistics
python scripts/journal.py export   # export to CSV
```

### Trade log structure (data/journal.json)
```json
{
  "trades": [
    {
      "trade_id": 1,
      "week_ending": "2026-09-12",
      "setup": "TWPR",
      "trade_type": "paper",
      "grade": "B",
      "direction": "bearish",
      "confidence": 55,
      "crude_deviation_mb": 1.209,
      "option_type": "put",
      "strike": 5900,
      "lots": 1,
      "entry_premium": 820.0,
      "entry_time": "2026-09-11T20:08:00+05:30",
      "exit_premium": 492.0,
      "exit_time": "2026-09-11T21:15:00+05:30",
      "exit_type": "stop",
      "gross_pnl": -32800.0,
      "ctt_charge": 820.0,
      "brokerage": 40.0,
      "net_pnl": -33660.0,
      "return_pct": -40.0,
      "capital_deployed": 82000.0,
      "notes": ""
    }
  ]
}
```

### P&L Calculations
```python
gross_pnl    = (exit_premium - entry_premium) × lots × 100
ctt_charge   = entry_premium × lots × 100 × 0.0005   # 0.05% CTT
brokerage    = 20 × 2                                  # ₹20 per leg (Zerodha)
net_pnl      = gross_pnl - ctt_charge - brokerage
return_pct   = (exit_premium - entry_premium) / entry_premium × 100
```

### Stats output (journal.py stats)
```
TradeDesk — TWPR Performance
──────────────────────────────
Period:         Sep 2026 → present
Total signals:  8
Trades taken:   6  (2 skipped)
Paper trades:   6  (0 live)

Win rate:       50.0%  (3W / 3L)
Avg winner:     +62.3%
Avg loser:      -38.7%
Profit factor:  1.52

Grade A:        2 trades | 100% win rate | avg +71%
Grade B:        4 trades | 25% win rate  | avg -12%

Exits by type:
  Stop loss:    2 (33%)
  Target 1:     2 (33%)
  Target 2:     1 (17%)
  Hard close:   1 (17%)

Net P&L:        ₹+12,400  (paper)
```

---

## Update Task — Add PetroCore to Existing Scripts

### Pattern for each existing script

Add this at the end of each script's main execution:

```python
from petrocore_client import PetroCoreClient

# At the end of the script, after all processing:
client = PetroCoreClient()
client.post_<endpoint>(payload)
```

### consensus_fetcher.py
Post after saving `data/consensus.json`:
```python
client.post_consensus({
    "week_ending": week_ending,
    "crude_consensus_mb": crude,
    "gasoline_consensus_mb": gasoline,
    "distillate_consensus_mb": distillate,
    "crude_previous_mb": previous,
    "source": "investing.com",
    "fetched_at": datetime.now(timezone.utc).isoformat()
})
```

### api_monitor.py
Post after saving `data/api_report.json`:
```python
client.post_api_report({
    "week_ending": week_ending,
    "report_date": report_date,
    "api_crude_mb": crude,
    "api_cushing_mb": cushing,
    "api_gasoline_mb": gasoline,
    "api_distillate_mb": distillate,
    "source": "investing.com",
    "released_at": released_at
})
```

### eia_parser.py
Post after saving `data/eia_actual.json`:
```python
client.post_eia_report({
    "week_ending": week_ending,
    "report_date": report_date,
    "crude_change_mb": crude,
    "cushing_stocks_mb": cushing,
    "gasoline_change_mb": gasoline,
    "distillate_change_mb": distillate,
    "refinery_util_pct": refinery_util,
    "source": "eia_api",
    "released_at": released_at
})
```

### signal_engine.py
Post after saving `data/signal.json`:
```python
client.post_signal({
    "week_ending": week_ending,
    "setup": "TWPR",
    "trade_type": "live",
    "grade": grade,
    "direction": direction,
    "confidence": confidence,
    "crude_deviation_mb": crude_deviation,
    "cushing_mb": cushing_mb,
    "cushing_confirms": cushing_confirms,
    "gasoline_deviation_mb": gas_deviation,
    "distillate_deviation_mb": dist_deviation,
    "api_crude_mb": api_crude,
    "api_aligns": api_aligns,
    "option_type": option_type,
    "strike_type": strike_type,
    "size_pct": size_pct,
    "key_drivers": key_drivers,
    "risks": risks,
    "reasoning": reasoning,
    "model_used": model_used,
    "generated_at": datetime.now(timezone.utc).isoformat()
})
```

---

## Build Task 6 — tests/test_signal_engine.py

Unit tests for the signal logic. The 5-step rule engine must be
100% deterministic — same inputs always produce same output.

### Test cases to cover

```python
# Step 1 — Skip zone
test_skip_zero_deviation()          # crude_deviation = 0.0 → skip
test_skip_just_under_threshold()    # crude_deviation = 0.99 → skip
test_skip_exactly_threshold()       # crude_deviation = 1.0 → skip

# Step 1 — Grade determination
test_grade_b_bullish()              # deviation = -1.2 → B bullish
test_grade_a_bullish()              # deviation = -1.6 → A bullish
test_grade_b_bearish()              # deviation = +1.2 → B bearish
test_grade_a_bearish()              # deviation = +1.6 → A bearish

# Step 2 — Cushing adjustment
test_cushing_confirms_bullish()     # bullish + cushing draw → A stays A
test_cushing_contradicts_bullish()  # bullish + cushing build → A → B
test_cushing_contradicts_bearish()  # bearish + cushing draw → A → B
test_b_stays_b_when_contradicted()  # B contradicted → still B (not downgraded further)

# Step 4 — API alignment
test_api_aligns_adds_confidence()       # +5 confidence
test_api_contradicts_reduces_confidence() # -5 confidence

# Step 5 — Confidence calculation
test_grade_a_base_confidence()      # Grade A → base 75
test_grade_b_base_confidence()      # Grade B → base 55
test_max_confidence()               # A + Cushing confirms + API aligns → 85
test_min_tradeable_confidence()     # B + both contradict → 45

# Reference week
test_reference_week_sep4_2026()     # deviation +1.209 → B bearish, confidence 55
```

---

## TWPR Signal Logic (source of truth)

Implement in `signal_engine.py`. Do not deviate from these rules.

### Inputs
```python
crude_deviation_mb      = eia_crude_change - consensus_crude
gasoline_deviation_mb   = eia_gasoline_change - consensus_gasoline
distillate_deviation_mb = eia_distillate_change - consensus_distillate
cushing_mb              = eia_cushing_change
api_crude_mb            = api_report_crude_change
```

### Step 1 — Grade from crude deviation
```python
if abs(crude_deviation_mb) <= 1.0:
    return Signal(grade='skip', direction='neutral', confidence=0)

if crude_deviation_mb <= -1.5:
    grade, direction = 'A', 'bullish'
elif crude_deviation_mb <= -1.0:
    grade, direction = 'B', 'bullish'
elif crude_deviation_mb >= 1.5:
    grade, direction = 'A', 'bearish'
else:  # >= 1.0
    grade, direction = 'B', 'bearish'
```

### Step 2 — Cushing adjustment
```python
cushing_contradicts = (
    (direction == 'bullish' and cushing_mb > 0) or
    (direction == 'bearish' and cushing_mb < 0)
)
if cushing_contradicts and grade == 'A':
    grade = 'B'   # downgrade A to B only
# B stays B regardless
```

### Step 3 — Products check
```python
products_strongly_oppose = (
    abs(gasoline_deviation_mb) > 2.0 and
    abs(distillate_deviation_mb) > 2.0 and
    (gasoline_deviation_mb * (1 if direction == 'bearish' else -1)) > 0 and
    (distillate_deviation_mb * (1 if direction == 'bearish' else -1)) > 0
)
# Note in risks if True — does NOT change grade
```

### Step 4 — API alignment
```python
api_aligns = (
    (direction == 'bullish' and api_crude_mb < 0) or
    (direction == 'bearish' and api_crude_mb > 0)
)
```

### Step 5 — Confidence score
```python
confidence = 75 if grade == 'A' else 55
if cushing_contradicts:
    confidence -= 5
else:
    confidence += 5
if api_aligns:
    confidence += 5
else:
    confidence -= 5
# Range: 45-85 for tradeable signals
```

### Trade recommendation
```python
if grade == 'A':
    option_type = 'call' if direction == 'bullish' else 'put'
    strike_type = 'ATM'
    size_pct = 2.0
elif grade == 'B':
    option_type = 'call' if direction == 'bullish' else 'put'
    strike_type = '1-OTM'
    size_pct = 1.5
```

---

## Reference Week — Sep 4, 2026

Use this to validate the signal engine end-to-end.

```python
# Inputs
crude_deviation_mb      = +1.209   # EIA -0.391 − Consensus -1.600
gasoline_deviation_mb   = +2.669
distillate_deviation_mb = +2.787
cushing_mb              = -0.684   # draw (contradicts bearish)
api_crude_mb            = +1.250   # build (aligns with bearish)

# Expected output
grade       = 'B'
direction   = 'bearish'
confidence  = 55
option_type = 'put'
strike_type = '1-OTM'
size_pct    = 1.5
```

---

## Code Standards

### Python version
Python 3.12

### Style
- Type hints on all function signatures
- Docstrings on all public functions (one-line minimum)
- f-strings for string formatting
- `pathlib.Path` for file paths (not `os.path`)
- `datetime.now(timezone.utc)` — never naive datetimes
- IST timezone: `ZoneInfo("Asia/Kolkata")`

### Error handling
- Every script has a `main()` function wrapped in `try/except`
- On exception: log the error with full traceback, send Telegram error
  alert, exit with code 1
- Never silently swallow exceptions in business logic
- PetroCore failures: log and continue (non-blocking)

### Logging
```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
```

### File paths
```python
from pathlib import Path
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
```

### Environment loading
```python
from dotenv import load_dotenv
load_dotenv()
```

---

## Requirements.txt

Ensure these packages are in requirements.txt:
```
# Data
yfinance>=0.2.40
pandas>=2.2.0
requests>=2.32.0
httpx>=0.27.0

# Async / scraping
playwright>=1.45.0
asyncio

# Scheduling
APScheduler>=3.10.0

# AI
groq>=0.11.0

# Telegram
python-telegram-bot>=21.0

# Environment
python-dotenv>=1.0.0

# Timezone
tzdata>=2024.1

# Testing
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

## Deliverables Checklist

When complete, the following must exist and work:

- [ ] `scripts/petrocore_client.py` — shared PetroCore HTTP client
- [ ] `scripts/market_data.py` — daily market data fetcher
- [ ] `scripts/scheduler.py` — local APScheduler pipeline
- [ ] `scripts/monitor.py` — post-signal exit monitor
- [ ] `scripts/journal.py` — trade log and statistics
- [ ] `scripts/consensus_fetcher.py` — updated with PetroCore POST
- [ ] `scripts/api_monitor.py` — updated with PetroCore POST
- [ ] `scripts/eia_parser.py` — updated with PetroCore POST
- [ ] `scripts/signal_engine.py` — updated with PetroCore POST
- [ ] `scripts/telegram_bot.py` — updated with PetroCore POST
- [ ] `tests/test_signal_engine.py` — all test cases pass
- [ ] `requirements.txt` — all dependencies listed
- [ ] `python -m pytest tests/` — all tests green
- [ ] Reference week test passes:
      deviation +1.209 → Grade B Bearish, confidence 55

---

## Start Here

1. Read `docs/twpr_setup_spec.md` in full before writing any code
2. Read all existing scripts in `scripts/` to understand current patterns
3. Build `petrocore_client.py` first — everything depends on it
4. Build missing scripts in this order:
   market_data → scheduler → monitor → journal
5. Update existing scripts to add PetroCore integration
6. Write and run tests last — confirm reference week passes

Do not modify `investing_scraper.py` or `tradingeconomics_scraper.py`.
