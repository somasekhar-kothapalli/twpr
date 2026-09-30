# TWPR v0.1: scope

What v0.1 is, what it is not, and what has to be true before it is called done. Written 2026-09-30 against what is built (371 offline tests, one full replay of 23-09-2026 through `python -m app.run all --replay 23-09-2026`).

## What v0.1 is

**A weekly alert-and-record system for buying MCX crude oil options around the EIA Weekly Petroleum Status Report. The system decides and alerts; you place and manage every order by hand.** Crude oil only: `CRUDEOIL` (100 bbl) and `CRUDEOILM` (10 bbl mini). Options buyer only, ITM, held for the evening of the release and exited the same night.

Nothing in v0.1 places, changes or cancels an order, and nothing connects to a broker.

## In scope (built and tested)

| Area | What v0.1 does |
|---|---|
| **Inputs** | Consensus (crude, gasoline, distillate) and the EIA print from tradingeconomics / investing.com raced together; the API crude change; EIA's Cushing level; ATR, OVX, spreads, DXY, the overnight rally and the USD/INR trend from Yahoo; USD/INR from yfinance then FreeCurrencyAPI (no default rate). |
| **Decision** | TLS with seasonal weights, Z-score against 12 weeks of history (MAD by default), the 1.25 gate, the Cushing check with a 1.0 mb materiality threshold, Regimes 1 (with the headline), 2 (Cushing fade) and 3 (sell the fact, needs a measured rally). |
| **Option** | ITM only; delta 0.60-0.70, or 0.80-0.85 when OVX is above 35; expiry from MCX's 2026 calendar (two business days before the futures), rolled at 5 days or fewer; a Black-76 estimate of the target-delta strikes; the rupee context. |
| **MCX mechanics** | Contract sizes, 2026 holidays, the evening-session check, session close and the hard exit capped an hour before it (23:30 summer / 23:55 winter), the 4% futures band against the expected move, devolution avoided by never holding to expiry. |
| **Sizing** | You set the lots (`MCX_CRUDEOIL_LOTS`, `MCX_CRUDEOILM_LOTS`); the signal shows what they lose in INR at three futures stops. No equity maths. |
| **Delivery** | The signal alert, a pre-print brief (how big a surprise the model needs), Telegram failure alerts from every script, time reminders (`watch`). |
| **Record** | `journal`: your fills, slippage against your intended price, estimated net P&L, and WTI's path after each print. `surprise_history` grows by itself each week. `crude_recorder` writes every Wednesday's decision and price path automatically (traded or not), so the surprise-versus-move relationship can be measured on all weeks, not only the traded ones. |
| **Operation** | `python -m app.run pre` / `print --watch` / `all --replay DATE`; each stage stops the chain on failure. |
| **Docs** | `CLAUDE.md`, `README.md`, the two runbooks (with implementation notes), `MARKET_FACTORS.md`, the MCX contract facts. |

## Out of scope for v0.1 (deliberately)

- **Order execution of any kind**, and any broker connection.
- **An option chain feed** (real strikes, premiums, spreads, implied volatility) and **premium-based stop watching**. The strike guide is an estimate; you read the chain yourself.
- **Unattended scheduling.** The commands are ready to schedule but nothing schedules them.
- **Natural gas.** The size flags are read and validated and nothing else.
- **2027**: the futures/options launch calendars and the 2027 holidays are not loaded (2027 expiries are a flagged guess).
- **Backtesting**, CI, headless running (the scrapers use a real browser), weekly options, the CFTC positioning and refinery-utilisation rules.
- **Trusting the AI paragraph.** It is optional prose; it can be wrong and never affects the trade.

## How a Wednesday runs

1. **Afternoon:** `python -m app.run pre`. Consensus is not posted for gasoline/distillate until close to the release, so it may need a rerun; you get the pre-brief.
2. **~19:55 IST (20:55 in winter):** `python -m app.run print --watch`. Actuals, signal, alert, then the reminders.
3. **You:** read the alert, work the checklist, find the strike on your chain (starting from the guide), place limit orders, manage the stop, exit by the time stop or the hard exit.
4. **After:** `python -m app.journal fill ...` for each trade, and `python -m app.journal path` within a week.

## v0.1 is done when

1. `.env` is complete: Telegram, `FREECURRENCYAPI_KEY`, `MCX_CRUDEOIL_LOTS` or `MCX_CRUDEOILM_LOTS` (1 lot to start; the mini is the natural choice).
2. **One live Wednesday** runs end to end with no manual patching: the print arrives, a signal (or a stand-down) is sent, and nothing needed a code change.
3. The two things the code assumes are **checked once against the live chain**: the expiry date and that the target-delta strikes sit near the strike guide.
4. **Three forward Wednesdays** at minimum size, with every trade and every price path in the journal (the runbook's own validation window). The point is to measure slippage and how much of the move is left when you can enter, not profit.
5. The known limits below are read and accepted.

## Known limits (read before real money)

- **The edge is unproven.** About 9 weeks of history, no backtest. In today's volatility the gate needs |TLS| of roughly 6-7 mb, so the system will mostly stand down.
- **The modelled move (beta_vol) is unreliable** and is shown as an "unproven" footnote; the 0.15-0.30 USD/mb anchor leads. The one real print measured so far favoured the anchor.
- **The data path is fragile:** scrapers can be blocked, consensus posts late, Yahoo has contract-roll distortions and no CL1-CL2 near expiry. A failure alerts you and produces no signal.
- **What decides P&L is not in the system:** premium, spread, implied volatility, the IV crush. The lot risk assumes the option loses `delta x` the futures stop and ignores all of that.
- **A deep-ITM option costs a lot up front** (about Rs 1.3 lakh a crude lot, Rs 13,000 a mini lot; estimate) and is capital at risk if held.
- **The 4% futures band** can lock the market and freeze options; at large surprises even the anchor's top end approaches it (the message says so).
- **Nothing watches your position** except time reminders.

## Candidates for v0.2 (not committed to)

A read-only broker option chain (strikes, premiums, spreads, the capital blocked); premium-stop and price alerts; scheduling; the 2027 calendars and holidays; a backtest on a deeper surprise history; CI and a headless scraper; a refinery-utilisation rule and CFTC positioning; natural gas if a setup exists.
