# Manual testing runbook

How to exercise every script by hand, without waiting for a Wednesday.

Run everything from the repo root. Commands are copy-pasteable.

> **`data/*.json` is committed.** Manual runs overwrite those files, so finish
> with the [cleanup](#9-cleanup) step before committing anything, or you will push
> test data as if it were a real week.

---

## 0. Prerequisites

```bash
pip install -r requirements.txt
python -m playwright install chromium    # only for the investing.com fallback
cp .env.example .env                     # then fill in what you have
```

**If you use a virtualenv, install into it, not the system Python.** A venv
created before `playwright` landed in `requirements.txt` will log
`investing_scraper.fetch_consensus() failed: playwright is not installed ...`
and fall through to Trading Economics. That is graceful degradation, not a
crash — but it leaves you with one consensus source instead of two:

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/Scripts/python.exe -m playwright install chromium
```

**Keep comments in `.env` on their own line.** `python-dotenv` keeps an inline
`# ...` as the value when the value is empty, so this sets the key to the comment
text and the EIA API answers 403 as if the key were bad:

```bash
EIA_API_KEY=        # free at eia.gov/opendata   <-- WRONG, key becomes "# free at ..."
```

`common.env()` now treats a `#`-leading value as unset, so the pipeline falls back
cleanly instead of sending garbage. Check what your `.env` actually yields:

```bash
python - <<'EOF'
from dotenv import dotenv_values
for k, v in dotenv_values(".env").items():
    state = "unset" if not v else ("LEAKED COMMENT" if v.lstrip().startswith("#") else f"set/{len(v)}")
    print(f"  {k:22} {state}")
EOF
```

Confirm you are running the interpreter you think you are:

```bash
python -c "import sys, importlib.util as u; print(sys.executable); print('playwright:', u.find_spec('playwright') is not None)"
```

Nothing below needs a full `.env`. What each key unlocks:

| Key | Without it |
| --- | ---------- |
| *(none)* | rule engine, journal, monitor, scrapers, scheduler all work |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts are logged to the terminal instead of sent. **With them set, every test below really messages your phone** — unset them for a quiet run |
| `EIA_API_KEY` | `eia_parser.py` scrapes instead (no `refinery_util_pct`); `market_data.py` leaves M1/M2 null |
| `PETROCORE_URL`, `PETROCORE_API_KEY` | POSTs are skipped with a warning |
| `GROQ_API_KEY` | narrative falls back to `rule_based` |

**Pick a week with published data.** The scrapers only have numbers for weeks
already released. Find one:

```bash
python -c "import sys;sys.path.insert(0,'app');from common import week_ending;print(week_ending())"
```

That prints the week the *current* cycle covers — usually not yet released. Use
the Friday **one or two weeks earlier** for live scraper tests. Examples below
use `2026-09-18`; substitute a released week when you read this.

---

## 1. Fast checks first (5 seconds, no network)

```bash
python -m pytest tests/ -q
```

Expect `110 passed`. These are all offline. If this fails, stop here — nothing
below will be meaningful.

Run just the reference week, the one test that must never go red:

```bash
python -m pytest tests/test_signal_engine.py::test_reference_week_sep4_2026 -v
```

Confirm every module imports (catches a bad edit before you run anything):

```bash
python -c "import sys;sys.path.insert(0,'app');import importlib;[importlib.import_module(m) for m in ('common','petrocore_client','telegram_bot','signal_engine','eia_parser','consensus_fetcher','api_monitor','monitor','journal','scheduler','market_data','tradingeconomics_scraper','investing_scraper')];print('all modules import ok')"
```

---

## 2. The rule engine

The core. Pure function — feed it five numbers, read the verdict.

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from signal_engine import generate_signal

def show(dev, cush=0.0, api=0.0, gas=0.0, dist=0.0):
    s = generate_signal(dev, gas, dist, cush, api)
    trade = "-" if s.grade == "skip" else f"{s.option_type} {s.strike_type} {s.size_pct}%"
    print(f"dev {dev:+6.3f}  cushing {cush:+5.2f}  api {api:+5.2f}  ->  "
          f"{s.grade:4} {s.direction:7} conf {s.confidence:3}  {trade}")

show(0.0)                        # skip zone
show(1.0)                        # exactly 1.0 -> still skip (inclusive)
show(1.001)                      # B bearish
show(1.5)                        # exactly 1.5 -> Grade A (inclusive)
show(-1.6)                       # A bullish
show(1.6, cush=-0.5)             # Cushing contradicts -> A downgrades to B
show(1.2, cush=-0.5)             # B contradicted -> stays B (B is the floor)
show(1.6, cush=1.0, api=1.0)     # everything confirms -> max confidence 85
show(1.2, cush=-1.0, api=-1.0)   # everything contradicts -> min 45
show(1.209, gas=2.669, dist=2.787, cush=-0.684, api=1.250)   # reference week
EOF
```

**What to check.** The last line must read `B bearish conf 55 put 1-OTM 1.5%`.
Both thresholds are inclusive: `1.0` skips, `1.5` is Grade A. Confidence never
leaves 45–85.

### The AI must not be able to move a number

The one invariant worth re-testing after any change to `signal_engine.py`:

```bash
python - <<'EOF'
import sys, json; sys.path.insert(0,'app')
from unittest.mock import patch
from common import setup_logging; setup_logging()
import signal_engine as se

base = se.generate_signal(1.209, 2.669, 2.787, -0.684, 1.250)
evil = json.dumps({"key_drivers":["x"], "risks":["y"],
                   "reasoning":"Actually Grade A bullish at 99.",
                   "grade":"A", "direction":"bullish", "confidence":99, "size_pct":10.0})
class FakeGroq:
    def __init__(self,*a,**k):
        self.chat=type("c",(),{"completions":type("x",(),{"create":lambda *a,**k: type("r",(),{
            "choices":[type("ch",(),{"message":type("m",(),{"content":evil})()})()]})()})()})()

import os; os.environ["MODEL_MODE"]="groq"; os.environ["GROQ_API_KEY"]="fake"
with patch.dict(sys.modules, {"groq": type("m",(),{"Groq":FakeGroq})}):
    s = se.add_narrative(se.generate_signal(1.209, 2.669, 2.787, -0.684, 1.250))

print("narrative:", s.reasoning[:45])
for f in ("grade","direction","confidence","size_pct"):
    ok = getattr(s,f) == getattr(base,f)
    print(f"  {f:11} {getattr(s,f)!s:8} {'OK' if ok else 'COMPROMISED'}")
EOF
```

**What to check.** Every field `OK`. The prose changes, the numbers do not. Any
`COMPROMISED` means the AI boundary is broken — do not trade on that build.

---

## 3. Consensus and API report (live scrapers)

Trading Economics is tried first (plain HTTP, ~1s), investing.com second
(headless Chromium, ~15s).

```bash
# both sources directly, side by side
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import setup_logging; setup_logging()
import tradingeconomics_scraper as te, investing_scraper as inv
WEEK = "2026-09-18"
print("TE        consensus:", te.fetch_consensus(WEEK))
print("investing consensus:", inv.fetch_consensus(WEEK))
print("TE        api report:", te.fetch_api_report(WEEK))
EOF
```

**What to check.** Actuals agree between sources. Crude *consensus* may differ by
~0.1 mb — different survey panels, expected. A `None` for a week that is not yet
released is correct behaviour, not a failure.

Now through the real fetchers:

```bash
python app/consensus_fetcher.py --no-prompt --week 2026-09-18
cat data/consensus.json

python app/api_monitor.py --once --no-prompt --week 2026-09-18
cat data/api_report.json
```

**What to check.** `source` says which scraper won. `week_ending` matches the
`--week` you passed — if it does not, the fetch and the label disagree, which is
a bug worth stopping for.

Manual entry, no network at all:

```bash
python app/consensus_fetcher.py --crude -1.6 --gasoline 0.5 --distillate -0.3 --previous -2.0 --week 2026-09-04
python app/api_monitor.py --crude 1.25 --cushing 0.2 --gasoline 1.0 --distillate 0.5 --week 2026-09-04
```

Interactive prompt (omit the flags and it asks; type a letter to see it re-ask):

```bash
python app/consensus_fetcher.py --week 2026-09-04
```

---

## 4. EIA actuals

Three sources, tried in order on every poll: the EIA API when `EIA_API_KEY` is
set, then Trading Economics, then investing.com.

**No key needed** — the scrapers cover it:

```bash
EIA_API_KEY= python app/eia_parser.py --once --week 2026-09-18
cat data/eia_actual.json
```

**What to check.** `source` reads `tradingeconomics.com` (or `investing.com`),
and `refinery_util_pct` is `null` — neither site carries the utilization
percentage, and no rule reads it. The four stock changes must all be present; a
partial scrape is discarded rather than written.

Both scrapers directly, to confirm they agree:

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import setup_logging; setup_logging()
import tradingeconomics_scraper as te, investing_scraper as inv
W = "2026-09-18"
print("TE       :", te.fetch_eia_actuals(W))
print("investing:", inv.fetch_eia_actuals(W))
EOF
```

**What to check.** Identical numbers. These are published facts, not forecasts —
unlike the consensus, they must not differ.

### With a key

One attempt instead of polling for 90 minutes:

```bash
python app/eia_parser.py --once --week 2026-09-18
cat data/eia_actual.json
```

**What to check.** Figures are in **million barrels** — crude should be a number
like `-0.391`, not `-391`. If it is 1000× too large, the kb→mb conversion broke.
If you get `need 2 weeks of data` or no rows, the series ids are the first
suspect (see `STOCK_SERIES` in `app/eia_parser.py`).

No key? Exercise the whole flow against a mock, including the conversion:

```bash
python - <<'EOF'
import sys, json; sys.path.insert(0,'app')
from unittest.mock import patch
from common import setup_logging, write_json, DATA_DIR
setup_logging()
import eia_parser as ep

# thousand barrels, two weeks apart -> the reference week's changes
V = {"WCESTUS1":(421300,421691), "W_EPC0_SAX_YCUOK_MBBL":(24316,25000),
     "WGTSTUS1":(222669,220000), "WDISTUS1":(122787,120000), "WPULEUS3":(93.1,92.0)}
def fake(url, params=None, timeout=None):
    cur, prev = V[dict(params)["facets[series][]"]]
    body = {"response":{"data":[{"period":"2026-09-04","value":cur},
                               {"period":"2026-08-28","value":prev}]}}
    return type("R",(),{"json":lambda s=None: body, "raise_for_status":lambda s=None: None})()

with patch("eia_parser.httpx.get", fake):
    report = ep.fetch_eia_report("fake-key", expected_week="2026-09-04")
write_json(DATA_DIR/"eia_actual.json", report)
print(json.dumps(report, indent=2))
EOF
```

**What to check.** `crude_change_mb: -0.391`, `cushing_stocks_mb: -0.684`,
`gasoline_change_mb: 2.669`, `distillate_change_mb: 2.787` — the reference week.

---

## 5. Full Wednesday pipeline

### Entirely from scrapers, no API key

The shortest end-to-end proof, on a real past week:

```bash
export EIA_API_KEY=
python app/consensus_fetcher.py --no-prompt --week 2026-09-18
python app/api_monitor.py --once --no-prompt --week 2026-09-18
python app/eia_parser.py --once --week 2026-09-18
python app/signal_engine.py
```

**What to check.** `Grade A bearish | confidence 85 | deviation +3.569 mb`
(EIA +2.969 − consensus −0.600). Verify the arithmetic yourself:

```bash
python -c "import json;c=json.load(open('data/consensus.json'));e=json.load(open('data/eia_actual.json'));print(f\"{e['crude_change_mb']:+.3f} - {c['crude_consensus_mb']:+.3f} = {e['crude_change_mb']-c['crude_consensus_mb']:+.3f} mb\")"
```

### With the reference week

Chain the steps the way the Wednesday workflow does. This uses the reference
week so you know the answer in advance.

```bash
python app/consensus_fetcher.py --crude -1.6 --gasoline 0.0 --distillate 0.0 --previous -2.0 --week 2026-09-04
python app/api_monitor.py --crude 1.25 --cushing 0.2 --gasoline 1.0 --distillate 0.5 --week 2026-09-04

# EIA actuals for the reference week, without needing a key
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import write_json, DATA_DIR
write_json(DATA_DIR/"eia_actual.json", {
    "week_ending":"2026-09-04", "report_date":"2026-09-09",
    "crude_change_mb":-0.391, "cushing_stocks_mb":-0.684,
    "gasoline_change_mb":2.669, "distillate_change_mb":2.787,
    "refinery_util_pct":93.1, "source":"eia_api",
    "released_at":"2026-09-09T14:30:00+00:00"})
print("wrote data/eia_actual.json")
EOF

python app/telegram_bot.py --prebrief
python app/signal_engine.py
python app/telegram_bot.py
```

**What to check.** `signal_engine.py` logs
`Grade B bearish | confidence 55 | deviation +1.209 mb`, and the alert renders
with `Trade: PUT 1-OTM | size 1.5% of capital`. Without Telegram keys the message
is printed under `Telegram message (unsent)` — that is the exact text your phone
would receive.

Signal with a missing API report (it is optional — the week still trades):

```bash
rm -f data/api_report.json && python app/signal_engine.py
```

Expect a warning that `api_crude_mb` is treated as 0.0, and confidence 5 lower.

### The week-mismatch guard

Every deviation subtracts consensus from actuals, so the two files must be for
the same week. Stage a mismatch and confirm it refuses:

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import read_json, write_json, DATA_DIR
c = read_json(DATA_DIR/"consensus.json")
c["week_ending"] = "2026-09-18"        # a week the EIA file is not about
write_json(DATA_DIR/"consensus.json", c)
print("staged a stale consensus")
EOF
python app/signal_engine.py; echo "exit=$?"
```

**What to check.** Exit 1 and a `week mismatch` error naming both weeks. It must
not produce a signal. This is the shape of the real failure: Tuesday's fetch
breaks, Wednesday runs against last week's consensus, and the deviation comes
out plausible but wrong — for the reference week it lands on +0.209, inside the
skip zone, turning a Grade B trade into "no trade".

Restore it with `python app/consensus_fetcher.py --crude -1.6 --gasoline 0.0
--distillate 0.0 --previous -2.0 --week 2026-09-04`.

---

## 6. The exit monitor

> **Needs `data/signal.json` first.** `monitor.py` reads the signal it is
> monitoring, so run [section 5](#5-full-wednesday-pipeline) before this one. With
> no signal file the commands below exit straight away and print nothing, which
> reads like a hang rather than a missing prerequisite.

Interactive. Pipe answers in to replay a whole trade in one line —
`--interval 0` removes the 60-second wait.

```bash
# STOP LOSS: 1 lot, entry 820, premium falls to 492 (-40%)
# (each run consumes data/signal.json, so regenerate it between scenarios)
printf "y\n1\n820\n\n5900\n492\n492\n" | python app/monitor.py --interval 0
```

Input order: `y` (entered the trade) → lots → entry premium → option type
(blank accepts the signal's) → strike → current premium → confirmed fill.

```bash
# TARGET 1 then TARGET 2: 4 lots, 820 -> 1230 -> 1640
python app/signal_engine.py >/dev/null
printf "y\n4\n820\n\n5900\n1230\n1230\n1640\n1640\n" | python app/monitor.py --interval 0

# declined entry -> recorded as no_trade
printf "n\n" | python app/monitor.py && cat data/active_trade.json

# a skip signal -> exits immediately, nothing to monitor
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import write_json, DATA_DIR
write_json(DATA_DIR/"signal.json", {"grade":"skip","direction":"neutral",
    "week_ending":"2026-09-18","crude_deviation_mb":0.4,"confidence":0})
EOF
python app/monitor.py
```

**What to check.** Target 1 exits **half** the lots and keeps monitoring; Target 2
closes the rest. On the 4-lot run you should see `2 lots open` after T1.

The hard close needs the clock past 22:30 IST, so patch it:

```bash
python app/signal_engine.py >/dev/null
python - <<'EOF'
import sys, io; sys.path.insert(0,'app')
from datetime import time as dtime
from unittest.mock import patch
from common import setup_logging; setup_logging()
import monitor
sys.stdin = io.StringIO("y\n2\n820\n\n5900\n1010\n1010\n")
with patch.object(monitor, "HARD_CLOSE_IST", dtime(0, 1)):   # already past
    monitor.main()
EOF
```

Exit-condition precedence without any I/O — the stop must win over the hard
close, so a crash that trips both books as a stop:

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from datetime import time as dtime
from monitor import check_exit
print("-45% at 22:31   ->", check_exit(-45.0, True,  dtime(22,31)), "expect stop")
print("+120% at 22:31  ->", check_exit(120.0, True,  dtime(22,31)), "expect target_2")
print("+23% at 22:31   ->", check_exit(23.0,  True,  dtime(22,31)), "expect hard_close")
print("+60% t1 pending ->", check_exit(60.0,  False, dtime(21,0)),  "expect target_1 half")
print("+60% t1 done    ->", check_exit(60.0,  True,  dtime(21,0)),  "expect None")
EOF
```

Resume an interrupted position (Ctrl+C the first command after a few seconds):

```bash
printf "y\n2\n820\n\n5900\n" | python app/monitor.py --interval 3
printf "492\n492\n" | python app/monitor.py --resume --interval 0
```

---

## 7. The journal

```bash
# log a trade -- press Enter through the prompts to accept the values
# pre-filled from data/active_trade.json
python app/journal.py log

python app/journal.py stats
python app/journal.py week
python app/journal.py month
python app/journal.py export && cat data/journal_export.csv
```

Check the money maths directly:

```bash
python -c "import sys;sys.path.insert(0,'app');from journal import calculate_pnl;print(calculate_pnl(820.0,492.0,1))"
```

**What to check.** `ctt_charge` is **41.0**, not 820 — CTT is 0.05% of the entry
premium. `net_pnl` is `-32881.0`. `Profit factor` reads `n/a` before your first
losing trade, never `inf`.

---

## 8. Plumbing

### PetroCore client

Without `PETROCORE_URL` every POST is skipped and returns `False` — verify it
never raises:

```bash
python -c "import sys;sys.path.insert(0,'app');from common import setup_logging;setup_logging();from petrocore_client import PetroCoreClient;c=PetroCoreClient(base_url='',api_key='');print('skipped, returned',c.post_signal({'a':1}))"
```

Point it at a dead port to confirm a backend outage cannot stop a trade
(3 attempts, ~10s, no exception):

```bash
python -c "import sys;sys.path.insert(0,'app');from common import setup_logging;setup_logging();from petrocore_client import PetroCoreClient;print(PetroCoreClient(base_url='http://127.0.0.1:1',api_key='k').post_signal({'a':1}))"
```

For a full check — auth header, 5xx retry, 4xx *not* retried — run a throwaway
server that fails on purpose:

```bash
python - <<'EOF' &
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG=[]; FAIL={"/api/v1/twpr/signal":2}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        LOG.append((self.path, self.headers.get("X-API-Key")))
        left=FAIL.get(self.path,0)
        if left: FAIL[self.path]=left-1; self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        if self.path=="/api/v1/twpr/trade": self.send_response(422); self.end_headers(); self.wfile.write(b"bad"); return
        self.send_response(201); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(LOG).encode())
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1",8799),H).serve_forever()
EOF
sleep 2
python - <<'EOF'
import sys, time, httpx; sys.path.insert(0,'app')
from common import setup_logging; setup_logging()
from petrocore_client import PetroCoreClient
c = PetroCoreClient(base_url="http://127.0.0.1:8799", api_key="test-key")
print("201 happy path      ->", c.post_consensus({"a":1}))
t=time.monotonic(); print("500,500,201 retried ->", c.post_signal({"a":1}), f"{time.monotonic()-t:.1f}s (expect ~5s)")
t=time.monotonic(); print("422 not retried     ->", c.post_trade({"a":1}), f"{time.monotonic()-t:.1f}s (expect <1s)")
print("headers received    ->", httpx.get("http://127.0.0.1:8799/").json())
EOF
kill %1
```

**What to check.** `/signal` appears **three** times in the header log (the
retry), `/trade` **once** (4xx is not retried). `X-API-Key` present on all.

### Telegram

```bash
python app/telegram_bot.py --test      # needs the two keys; otherwise logs and exits 1
```

Render every message shape without sending anything:

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from common import setup_logging; setup_logging()
from telegram_bot import format_signal
from monitor import alert_for
print(format_signal({"grade":"skip","week_ending":"2026-09-18","crude_deviation_mb":0.4}))
trade = {"grade":"B","direction":"bearish","entry_premium":820.0}
for kind, cur, pct in (("stop",492.0,-40.0), ("target_1",1230.0,50.0),
                       ("target_2",1640.0,100.0), ("hard_close",1010.0,23.2)):
    print(); print(alert_for(kind, trade, cur, pct))
EOF
```

### Market data

```bash
python app/market_data.py --days 5
cat data/market_data.json
```

**What to check.** `wti_close` in the 60–110 range, `mcx_close` ≈
`wti_close × usd_inr_close`. `brent_wti_spread` is normally 3–5 — a much wider
number means yfinance served something odd, which also corrupts `crack_321`.
`wti_m1m2_spread` is null without `EIA_API_KEY`; that is expected.

### Scheduler

```bash
python app/scheduler.py --next     # next 5 runs, then exits
python app/scheduler.py --test     # runs market_data once, then exits
```

**What to check.** The times read `Mon–Fri 09:00`, `Tue 19:00`, `Wed 01:45`,
`Wed 19:30`, `Wed 20:00` IST. A failing job must alert and let the scheduler
survive:

```bash
python - <<'EOF'
import sys; sys.path.insert(0,'app')
from unittest.mock import patch
from common import setup_logging; setup_logging()
import scheduler
with patch.object(scheduler, "run_script", return_value=3):
    scheduler.run_job("consensus", ["consensus_fetcher.py"])
with patch.object(scheduler, "run_script", side_effect=OSError("disk full")):
    scheduler.run_job("eia_signal", ["eia_parser.py"])
print("scheduler survived both")
EOF
```

---

## 9. Cleanup

Manual runs leave files that are **tracked by git**. Reset before committing:

```bash
rm -f data/consensus.json data/api_report.json data/eia_actual.json \
      data/signal.json data/market_data.json data/active_trade.json \
      data/journal.json data/journal_export.csv
rm -rf app/__pycache__ tests/__pycache__ .pytest_cache
git status --short        # expect only your intended changes
git checkout data/        # if you overwrote a real week's committed data
```

---

## Quick smoke test

The short version when you just changed something and want confidence:

```bash
python -m pytest tests/ -q && \
python app/scheduler.py --next && \
python -c "import sys;sys.path.insert(0,'app');from signal_engine import generate_signal as g;s=g(1.209,2.669,2.787,-0.684,1.250);assert (s.grade,s.direction,s.confidence)==('B','bearish',55),s;print('reference week OK')"
```

---

## Known gaps

Things no manual test here can cover:

- **A real Wednesday.** The EIA leg has only ever run against fixtures.
- **EIA series ids** need one live run with a real `EIA_API_KEY`.
- **Consensus availability on Tuesday.** Both sources publish a consensus close
  to the release; whether it is there by Tuesday 19:00 IST is unverified. If it
  is not, that workflow exits 1 and you enter numbers by hand.
- **Live option premiums.** `monitor.py` prompts for them until Angel One
  SmartAPI replaces `read_current_premium()`.
