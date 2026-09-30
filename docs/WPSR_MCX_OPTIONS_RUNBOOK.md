# MCX Crude Oil Options — WPSR Wednesday Adaptation

Adapts [WPSR_WEDNESDAY_RUNBOOK.md](WPSR_WEDNESDAY_RUNBOOK.md)'s V4 model for **MCX INR options**, not MCX/NYMEX futures. Different instrument class entirely — a linear delta-1 futures runbook doesn't survive contact with a non-linear, IV-exposed one without a rewrite of execution, instrument selection, and risk mechanics. **The physical model underneath (TLS, Z-score, tiered Cushing rule, README §2) is unchanged** — oil fundamentals don't care what instrument you trade them through. Everything below is new instrument-specific wrapping around that same core.

**Provenance & caveat, same convention as the rest of this doc's build history:** this is Gemini-generated adaptation content (2026-09-29), not backtested, not verified against real MCX options market data. The USD/INR conversion heuristic, the delta-to-premium stop translation, and the specific IST windows below are illustrative approximations — check them against live rates and your actual option chain before trusting them for sizing.

---

## Timing Correction (read this first)

EIA releases 10:30 AM ET. IST is fixed UTC+5:30; US Eastern Time is not. 10:30 AM ET = **8:00 PM IST when the US is on EDT** (roughly mid-March to early November) and **9:00 PM IST when the US is on EST** (roughly early November to mid-March). Every IST clock time below assumes 8:00 PM IST (EDT case) — if the release falls during the US EST window, shift every timestamp in this document forward by exactly 1 hour. Check the current DST status before trusting the clock times as written; the exact transition dates move year to year.

*Implementation note:* the signal computes the print, the 35-minute time stop and the hard exit from 10:30 New York time, so they follow US daylight saving automatically (e.g. 20:00 / 20:35 / 22:30 IST in September; 21:00 / 21:35 / 23:30 IST after 1 November 2026). The fixed clock times in the prose below are the summer case.

---

## 1. Instrument Selection — Avoiding the Vega Trap

Pre-release option premiums are inflated by event-driven implied volatility (IV). The instant the print lands, that uncertainty resolves and IV collapses — an OTM option can lose value from the IV crush alone even if the futures price moves the right direction.

- **Mandate: no OTM strikes, ever, on this trade.**
- **Strike selection:** ITM options only, **Delta 0.60–0.70** as the default. **Dynamic deepening (2026-09-29 addition):** if the Cboe Crude Oil Volatility Index (OVX) is elevated above 35 going into the release (typically a geopolitical supply-threat regime), shift to **Delta 0.80–0.85** instead. At high OVX the baked-in IV premium is large enough that the crush can overpower even a 0.65-delta option's price gain; a 0.80+ delta strips out most extrinsic time-value, making the position track as a near-synthetic future rather than a volatility-exposed one. ITM value comes mostly from intrinsic price movement (high delta), not volatility time-value (high vega) — this is what makes the position track the futures move instead of getting eaten by the IV crush.
- **Expiration:** current-month expiry only if >5 days remain to expiry. If it's expiry week, roll to next month — gamma-driven bid/ask blowouts near expiry make the current-month chain untradeable for this event.

*Implementation note:* the code takes the nearest **option** expiry, defined as 2 business days before the futures expiry as listed in MCX's 2026 launch calendar (not a fixed day: Jan 16, Feb 19, Mar 19, Apr 20, May 18, Jun 18, Jul 20, Aug 19, Sep 21, Oct 19, Nov 19, Dec 18), and rolls when 5 or fewer days remain. The two-business-day rule is stated in MCX's option specification ("two business days prior to the expiry day of the underlying futures contract"), e.g. October 2026: futures Mon 19 Oct, options Thu 15 Oct. A month outside the loaded calendar (2027 on) falls back to the 19th and is flagged as a guess in the signal. Weekends are skipped, and MCX's 2026 full-day holidays (Republic Day, Good Friday, Gandhi Jayanti, Christmas) are too; none of them moves a 2026 expiry. 2027 holidays are not loaded — check the expiry on your chain. The pipeline has no option-chain feed, so it states the target delta range and expiry but **you pick the ITM strike** whose delta falls in range.

---

## 2. Execution Microstructure — The Dual-Screen Setup

MCX options liquidity is fragmented relative to the underlying futures contract. In the 2-minute post-release window, options market makers pull quotes — bid/ask can widen to ₹10–₹20. **You cannot use the options chart for signal generation.**

- **Signal chart:** MCX Crude Oil Futures (current month), 1-min + Volume Profile + VWAP bands, exactly as in the futures runbook. All entries, stops, and targets are read off the **futures** price.
- **Execution chart:** the selected ITM call/put option's DOM, watched separately, open and ready before the release.
- **Entry mechanic:** when futures price hits the Regime 1 limit-retest zone (or the equivalent Regime 2/3 trigger), execute a **limit order on the option chain** at the current bid/ask. **Never a market order on MCX options during this event** — slippage on an illiquid options book during the event window will wipe out the R:R the futures-side calc assumed.

---

## 3. Position Sizing & Stop Translation (USD → INR → Options Premium)

The futures runbook's USD-denominated stop distance has to pass through two conversions before it becomes an options risk number.

**Step 1 — FX conversion (illustrative heuristic, check live rate):**
```
$1.00 WTI move ≈ ₹83–84 MCX move   (verify against live USD/INR spot — do not hardcode this)
```

**Step 2 — Futures stop distance in INR:**
```
Example: a $0.25 (25-tick) WTI stop → roughly ₹20–22 MCX futures stop
```

**Step 3 — Options stop distance (delta-adjusted):**
```
Options Stop Distance ≈ Futures Stop Distance (₹) × Option Delta
Example: ₹22 futures stop × 0.65 delta ≈ ₹14 options premium stop
```

Exit the option when its own premium hits this stop — **do not hold the option hoping the futures price turns around.** The options stop is the actual risk-management trigger, not the futures level.

**Step 4 — Position sizing (MCX lot = 100 bbl):**
```
Max Risk Amount (INR) = Total Account Equity × 0.01
Risk Per Lot (INR)     = Options Stop Distance (₹) × 100
Contract Size (Lots)   = floor( Max Risk Amount / Risk Per Lot )

Example: ₹14 stop × 100 = ₹1,400 risk/lot. On a ₹1,000,000 account,
Max Risk = ₹10,000 → floor(10,000 / 1,400) = 7 lots.
```

*Implementation note:* USD/INR is read live (yfinance, 84.0 fallback), not hardcoded — it was ≈ 95.9 on 2026-09-30, so the "₹83–84 per $1" figure above is stale. The real futures stop comes from the chart, so the code no longer derives a lot count from a 1% budget (changed 2026-09-30). You set the lots you trade in `MCX_CRUDEOIL_LOT_SIZE` (1 for §6's validation window) and/or `MCX_CRUDEOILM_LOT_SIZE` (the 10 bbl mini contract) and the signal shows what those lots lose in INR if the option stop is hit, at futures stops of $0.18 / $0.25 / $0.35 and the middle of the delta range. If the lot count isn't set, nothing is shown rather than guessed. The 1% rule above is now a check you apply yourself: pick lots so the loss at your real stop is at most 1% of equity.
---

## 4. Regime Adaptations for MCX Options

Same three regimes as the futures runbook (README §5 / futures runbook §4-5) — only the instrument and the IST windows change. Times below assume the 8:00 PM IST (EDT) case — shift +1hr if EST applies (see Timing Correction above).

**Profit-taking, all regimes (2026-09-29 addition — Gamma capture):** scale out 50% of the position on the first clean structural momentum thrust, regardless of whether it has reached the final ΔP_expected target. As an options buyer, Gamma accelerates delta on the way up but reverses just as fast the moment price stalls or pulls back — the premium can deflate faster than the futures chart suggests once the thrust loses steam. Take half off into the initial liquidity vacuum, move the stop on the remainder to break-even, and let the runner target the level in the regime card below.

### Regime 1 — Full Alignment (Momentum)

- **Action:** Buy ITM Call (on a draw) or ITM Put (on a build). *(The signal also gives an expected WTI/MCX move for this regime only.)*
- **MCX nuance:** Wait for futures to retest the pre-release boundary, roughly 8:02–8:05 PM IST. Limit order into the option on that retest. If futures blast through the target with no pullback, **let it go** — chasing a runaway option fills you at peak IV, which then mean-reverts against you even if the futures direction was right.

### Regime 2 — Physical Divergence (The Fade)

- **Action:** Buy ITM Put (fading a headline draw) or ITM Call (fading a headline build). *(The signal's direction is already the fade, opposite the headline; it gives no modelled move, since the targets are chart levels.)*
- **MCX nuance:** this regime suits options better than Regime 1 does. The stall-and-reject entry (roughly 8:03–8:08 PM IST) happens after the initial IV crush, so you're buying the option at a structural discount right as the market sets up to reverse — better risk-adjusted entry than chasing the initial spike.

### Regime 3 — Expectation Skew (Sell the Fact)

- **Action:** Buy ITM Put.
- **MCX nuance:** MCX opens later than Globex — confirm the futures runbook's "Asian session low" target (still not formally defined anywhere in the source model, per that runbook's own caveat) actually maps to a real liquidity pool on the MCX intraday chart, typically the pre-market consolidation ~5:00–7:30 PM IST, rather than assuming it lines up automatically.

---

## 5. MCX-Specific Failure Protocols

1. **INR basis risk — abort rule:** if the RBI intervenes or USD/INR is independently volatile concurrent with the release, MCX futures will decouple from the NYMEX feed. **Abort the trade.** This mirrors README §13/futures runbook §8's currency basis risk note, but for options the correct response is an outright abort, not a downsize — an already-fragile options R:R can't absorb decoupled underlying pricing on top of the IV crush.
2. **Hard time-based exit — 10:30 PM IST:** Indian markets close 11:30–11:55 PM IST; the release leaves only 2.5–3.5 hours of session liquidity. Enforce a hard exit at 10:30 PM IST regardless of where the premium sits. **Never carry an EIA-day option overnight** — overnight theta decay plus gap risk destroys the system's expectancy on top of whatever the trade itself did.
3. **Circuit limits:** MCX has tighter dynamic price bands than CME. An extreme shock (Z_TLS > 4.0, README §2.2) can hit a circuit limit, at which point options liquidity goes to zero — if you're on the wrong side, you cannot exit. **Size down 50% if geopolitical tension is elevated going into the print**, independent of what the TLS/Z-score model itself says, since this is a liquidity-structure risk the price model doesn't capture at all.
4. **Halved chop exit (2026-09-29 addition — Theta/Vega defense):** the futures runbook's chop-exit rule is 8 minutes of VWAP ±$0.05 oscillation with no resolution (futures runbook §6). For options, **cut this to 4 minutes.** A futures position holding flat for 8 minutes loses only opportunity cost; an options position is actively bleeding extrinsic value the entire time — the IV crush bleeds out over roughly the first 5–10 minutes post-release, and a chopping market during that window is pure decay with no compensating directional gain. Exit at market on the option, not the future, once the 4-minute mark passes with no resolution.

---

## 6. Validation Protocol Before Scaling Size

Per External Review #7 (README.md), approved for live execution subject to this window — same pattern as the futures runbook's §6.1, applied to options:

1. **Size cap:** 1 lot only, for the next 3 EIA Wednesdays. Do not scale before this window completes regardless of how individual trades go.
2. **FX heuristic check:** verify the $1.00 WTI move ≈ ₹83-84 MCX move approximation (§3, Step 1) against the actual live rate during each trade — don't trust the hardcoded illustrative number.
3. **Slippage check:** record actual fill quality on ITM limit orders during the 8:02-8:05 PM IST execution window (§4, Regime 1) against the ₹10-₹20 bid-ask-widening assumption in §2.
4. **Scale-up gate:** only move to standard book size if both hold — the delta-to-premium stop math (§3, Steps 3-4) tracks live liquidity as modeled, AND the FX basis does not distort the WTI signal (README §13 abort rule, §5.1 here). If either fails, stay at 1 lot and revisit the model, don't average up.

---

## 6.1 MCX contract facts that change the trade (MCX options specification, March 2026, and the 2024 leaflet)

- **Options exist on the mini contract too** (`CRUDEOILM`, 10 bbl; confirmed by its own March 2026 specification): a real way to trade 1/10th the size. Every rule is identical to the full contract (same expiry calendar, 75 strikes ITM and OTM, Rs 50 interval, devolution into futures at expiry) except the size and the tick, Rs 0.05 against Rs 0.10.
- **75 strikes are listed in the money** (Rs 50 apart = Rs 3,750, about 43% of the futures price) in the March 2026 specification, so the 0.80-0.85 delta strike exists in any month (at IV 54% it is about 17-26 strikes ITM). An earlier draft of this section relied on the 2024 leaflet's 25 ITM strikes and concluded the 0.85 strike would vanish after a roll; the newer specification overturns that. You still pick the strike from the live chain.
- **You pay the whole premium upfront** and it is your maximum loss if you held: about Rs 1.3 lakh per crude lot or Rs 13,000 per mini lot for a deep-ITM option (estimate). The INR-at-risk figures in the signal are what you lose at your *stop*, a small fraction of that.
- **Futures price limit is 4%, widening to 6% and 9%.** In a volatile regime (OVX 54, daily sigma about 3.4%) 4% is a little over one sigma; a shock can lock the futures and freeze the options. The circuit-limit rule in §5.3 is not theoretical. The signal now prints the expected move as a share of the 4% band and warns when it is 75% or more.
- **The EIA release usually falls on an MCX evening session that is open**, even on MCX's morning-only holidays. Only four 2026 days close the whole day, and New Year Day closes just the evening; the signal warns when the release day's evening is closed.
- **The session ends 23:30 IST while US daylight saving time is in force and 23:55 IST after it ends** (MCX Circular MCX/TRD/550/2026: 23:55 from 2 Nov 2026 to 12 Mar 2027). In summer the print is 20:00 IST and the close 23:30, so 22:30 is one hour before it. In winter the print is 21:00 IST and the close 23:55, so "print + 2.5 h" would be 23:30, only 25 minutes before the close. The code therefore caps the hard exit at one hour before the close: 22:30 in summer, 22:55 in winter (§5.2's "10:30 PM IST" is the summer case).
- **European options that devolve into futures at expiry** (March 2026 spec): a long call becomes long futures, a long put short futures, at the strike, needing futures margin; the exchange may add pre-expiry margin in the last days. This is what the expiry gate and the same-evening exit protect against.

---

## 7. Implementation Status (app/code, 2026-09-30)

The pipeline in this repo produces the pre-release inputs and the signal; the option side is partly manual. See also the status table at the end of [WPSR_WEDNESDAY_RUNBOOK.md](WPSR_WEDNESDAY_RUNBOOK.md).

| Item in this file | Status |
|---|---|
| Delta target (0.60–0.70; 0.80–0.85 when OVX > 35, strictly above) | Automated |
| Expiry gate (> 5 days) | Automated from MCX's 2026 expiry calendar (option expiry = 2 business days before the futures expiry); 2027 on is a flagged guess; 2026 holidays loaded |
| Evening session closed on release day | Warned in the signal and the message (`mcx_evening_open`); the trade itself cannot be placed |
| ITM strike selection | **Manual**, aided by an estimate: no option-chain feed, so the signal gives Black-76 strikes for the target deltas (OVX as IV, futures = WTI x USD/INR, Rs 50 grid) to aim your search; confirm on the live chain |
| USD → INR → premium stop, INR risk of your lots | Automated per futures stop for the lots you set in `MCX_CRUDEOIL_LOTS` / `MCX_CRUDEOILM_LOTS`; the real stop is read off the chart, and the 1% check is yours |
| 1-lot rule for the validation window | Set `MCX_CRUDEOILM_LOTS=1` (or `MCX_CRUDEOIL_LOTS=1`) in `.env` |
| Limit-only entries, retest timing, 50% scale-out, 4-minute chop exit | **Manual** (listed on the signal's checklist); `app.watch` reminds you of the entry window, the 35-minute time stop and the hard exit, but cannot see your premium |
| INR basis abort, 50% geopolitical size-down | **Manual** checklist items. The INR item is now a fair-value check (MCX futures near WTI x USD/INR): the onshore FX market is shut during the hold, so the RBI cannot act in the window; the signal also shows the 5-session rupee trend and whether it helps or hurts the trade |
| Hard exit and time stop times | Automated (DST-aware, capped an hour before the close), shown in the message; `app.watch` sends the reminders |
| Slippage / fill log for §6 | `app.journal` (you enter the fills; it computes slippage, estimated net P&L and the post-print price path) |

The alert states which regime fired; a stand-down (|Z| < 1.25) carries no trade detail.

---

*Physical model, formulas, and all reconciliation history: [README.md](README.md). Futures-instrument execution mechanics this file adapts from: [WPSR_WEDNESDAY_RUNBOOK.md](WPSR_WEDNESDAY_RUNBOOK.md). Nothing in this file is backtested or validated against real MCX options market behavior — treat the FX heuristic, delta-translation math, and IST windows as a starting structure to verify live, not settled numbers.*
