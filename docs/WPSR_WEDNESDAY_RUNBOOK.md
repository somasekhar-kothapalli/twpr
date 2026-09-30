# EIA WPSR Trading System — Wednesday Operational Runbook

Operational extract/evolution of [README.md](README.md) — the full spec, background, and 23-section reference doc lives there. This file = what to actually do each Wednesday.

**Rebuilt 2026-09-29, three times, against three rounds of Gemini professional-trader critique** (all full reviews + caveats: README's "External Review" section). None of the three reviews is backtested — all are structured skeptical reads, not verified fact. Review #2 replaced fixed thresholds with statistically-normalized ones (Z-score filter, tiered Cushing rule, setup count 4→3); review #3 closed the two remaining judgment-call gaps that #2 left open (Cushing grey zones, GEX ambiguity) with deterministic rules. **This model has been backported into README §2/§5/§6 as canonical — README and this file now describe the same current model, not a fork.**

**Implemented in code (2026-09-30):** the data pipeline and the §3 model now exist in this repo (`app/`). Where the code deliberately differs from the wording below, the section says so in an *Implementation note*; what is automated versus still done by eye on the chart is in [Implementation status](#implementation-status-appcode-2026-09-30) near the end.

---

## Prerequisites & Known Limitations (read before trading this)

- **This system assumes automation.** A human reading the EIA PDF and calculating by hand cannot compete at 10:32 AM — the math must complete in under 500ms of the data feed arriving (see Pre-Release Initialization below). If you don't have a machine-readable feed (Bloomberg B-Pipe, Refinitiv, or a script polling the EIA API endpoint directly) wired into a calculator, **do not trade the 10:32:00 entries**. Fallback: wait for confirmed continuation at 10:35 AM+ (lower edge, no latency race) or skip the week.

  *Implementation note:* the repo's pipeline is not a sub-500ms feed. It scrapes tradingeconomics.com / investing.com (seconds, not milliseconds) and runs the model right after. That is enough for the runbook's own entries (limit orders on a retest, minutes after the print) but **not** for a 10:32:00 race, so treat every entry as the "10:35 AM+ continuation" fallback.
- **Cushing grey zones (22-30 MMbbl, >45 MMbbl) are now deterministic, not judgment calls** (fixed in review #3 — §3 Step 3 has the interpolation formula and the neutral default). The interpolation formula itself and the >45 MMbbl neutral default are original to this doc, not sourced from either review — they satisfy "hardcode a rule" without fabricating physical evidence that doesn't exist.
- **Dealer GEX is read from visible strike-level open interest, not true dealer positioning data.** §3 Step 8 now gates the Long/Short Gamma calls behind an OI concentration ratio (review #3 fix) — an ambiguous read defaults to the conservative case (tighten) rather than guessing. This manages the data gap, it doesn't close it; closing it for real needs an actual options-flow feed.
- **Position sizing formula below is calibrated for NYMEX CL (1,000 bbl/contract).** Trading MCX (100 bbl/lot, §13) — divide the contract multiplier by 10, and remember margin can spike ad-hoc on exactly this kind of volatility day. **MCX also carries USD/INR currency basis risk the model doesn't calculate** (review #5, §8) — check the rupee isn't in its own volatility event before sizing.

---

## Pre-Release Initialization — 10:29:50 AM ET

Before the print, pre-load into memory/spreadsheet/script — this cannot happen after 10:30:00 and still make the 10:32 window:
- Consensus estimates (crude, Cushing, mogas, distillate)
- Tuesday's API deltas
- 20-day ATR (WTI)
- Current OVX
- Rolling 12-week forecast-error spread (σ_forecast, for Step 2 — MAD by default, see the Step 2 implementation note)

The trader's role at 10:30:00 is **execution oversight and pattern validation, not calculation** — the pipeline below should already be running.

```
                      10:29:58 AM
              Pre-load consensus & priors
                           │
                           ▼
                      10:30:00 AM
              Automated feed payload arrives
                           │
                           ▼
                  Sub-second calculation:
                 TLS, Z-Score, Vol Scalar
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
    |Z_TLS| < 1.25σ               |Z_TLS| ≥ 1.25σ
      STAND DOWN             Auto-arm conditional DOM
                            levels for 10:32 execution
```

---

## 0. T-1 Day — Tuesday

**4:30 PM ET — API Weekly Statistical Bulletin (precursor)**

- Pull API's crude/gasoline/distillate Δ vs consensus.
- Flag if `|Δ_API − Δ_Consensus| > 3.0 MMbbl` → market already pre-positioned into Wednesday (README §3).
- Flag if API shows a large draw + WTI rallies overnight by >$1.00 → sets up **Regime 3** (expectation skew) for tomorrow. Note the API number now, you'll need it at 10:30.
- API access is subscription-only (LSEG/ICE) — full detail + 2026 release-date table: README §9.3.

---

## 1. T-0 Pre-Market — 10:00–10:29:59 AM ET

**Reference freeze (README §4.1):**
- Mark P_high / P_low of the 10:00–10:29:59 AM 30-min range.
- Compute VWAP_pre + ±1σ/±1.5σ/±2σ initial balance bands (the ±1.5σ band is now the stop-anchor reference — see §6).
- Note large resting bid/offer clusters (DOM).

**Chart setup (README §4.4):**
- Context: 60-min + Daily. Execution: 1-min + 30-sec.
- VWAP anchored twice: 00:00 UTC (Globex open) and 09:00 AM ET (cash open).
- Developing Volume Profile — mark POC, VAH, VAL.
- Auto-plot pre-release range high/low.
- **No unhedged market orders, ever, on entry** (tightened from "no orders 10:29:50–10:32:00" — see §6 order-routing rule).

**Pre-release conditioning scorecard (README §3) — score every layer before the print:**

| Layer | Variable | Threshold | Confirms |
|---|---|---|---|
| Physical Curve | CL1−CL2 | Backwardation > +$0.30/bbl | Physical tightness; dips get bought |
| Downstream | 3:2:1 Crack Spread | > $22.00/bbl | Refiners max out crude intake |
| Arbitrage | Brent−WTI | > $5.50/bbl | Export arb open, USGC exports stay high |
| Precursor | API surprise | \|Δ\| > 3.0 MMbbl vs consensus | Market pre-positioned |
| Alternative | Cushing tank level | See tiered rule, §3 Step 3 below (supersedes the flat 20/24 MMbbl thresholds) | Determines convexity multiplier, not just asymmetry direction |
| Microstructure | Dealer gamma | See GEX regime filter, §3 Step 8 below | Long Gamma = tighten targets; Short Gamma = widen them |

A trade is only taken if physical indicators **confirm** the inventory trajectory the model spits out — this table is the filter, not the trigger.

**Context checks before trusting any of the above (README §8.1/§8.4):**
- Where does the headline level sit vs the 5-year min/max/average for this calendar week, not just this week's Δ? A draw that leaves inventory still above the 5-yr max is a weaker bullish signal than the raw number suggests.
- Common misreads to rule out before trading: confusing a build with oversupply when the absolute level is still below seasonal norms; ignoring the adjustment factor's effect on the headline; treating gross imports as available supply without netting exports; misreading an SPR transfer as a commercial-inventory move; a holiday-shifted release throwing off the "Wednesday" assumption (check README §9.2's schedule table first).

---

## 2. The Print — 10:30:00 AM ET

If the automation pipeline is live, this arrives as a single payload. Manual fallback order:

1. **Commercial crude stock Δ** (actual vs consensus)
2. **Cushing, OK stock Δ**
3. **Gasoline (mogas) stock Δ**
4. **Distillate stock Δ**
5. **Refinery utilization %** — direction and absolute level (normal band 85–95%)
6. **Adjustment factor** ("unaccounted-for crude") — large positive value dilutes reliability of a bullish draw
7. **SPR transfer Δ** — net this against #1 to get true Net Domestic Balance:
   ```
   Net Domestic Balance = Commercial Crude Δ − SPR Transfer Δ
   ```
8. **Net imports/exports** — watch exports specifically, don't treat gross imports as available supply

---

## 3. Run The Model — 10:30:00–10:31:59 AM ET (zero orders, calc only)

Bid-ask blows out $0.01→$0.06–$0.12 here, HFT scrapers eat top-of-book. Sit on hands, run the calc — automated, in under 500ms if the pipeline is live.

**Step 1 — Total Liquid Surprise (TLS):**

```
ΔS_crude = V_act_crude − V_est_crude
ΔS_mogas = V_act_mogas − V_est_mogas
ΔS_dist  = V_act_dist  − V_est_dist

TLS = ΔS_crude + ω_g·ΔS_mogas + ω_d·ΔS_dist
```

| Weight | Base | Seasonal bump |
|---|---|---|
| ω_g (gasoline) | 0.67 | → 0.80, May–Sep (summer driving) |
| ω_d (distillate) | 0.50 | → 0.70, Nov–Feb (winter heating) |

**Step 2 — Significance filter (revised — statistically normalized, replaces the fixed 2.0 MMbbl cutoff):**

```
Z_TLS = TLS / σ_forecast          (σ_forecast = rolling 12-week std dev of consensus forecast errors,
                                    recomputed weekly — NOT the flat ≈2.0 MMbbl approximation)
Trade only if |Z_TLS| ≥ 1.25
```

Any shock smaller than 1.25σ is normal inventory noise and produces chop, not direction — stand down. This is a genuine model upgrade over README §2.2's fixed threshold, not just a relabeling: the bar now moves with actual recent forecast-error volatility instead of a static number picked once.

*Implementation note (deviation, 2026-09-30):* σ_forecast is computed over the weekly **TLS** values (each week's own seasonal weights), not per product, and needs at least 8 weeks (the engine refuses to trade without them; `python -m app.surprise_history --backfill` seeds ~10 weeks from investing.com). The default estimator is **1.4826 × median absolute deviation (MAD)**, not the plain sample standard deviation written above. Reason: the 12-08-2026 week printed TLS ≈ +20 mb; a plain std dev over the 9 weeks available was ~7.8 mb, which would have raised the gate to |TLS| ≥ ~9.7 mb and locked out every ordinary signal for 12 weeks. MAD on the same weeks is ~4.6 mb (gate |TLS| ≥ ~5.7 mb), matching the std dev with that one week removed without anyone choosing which week to drop. `SIGMA_METHOD=std` restores the literal wording; a zero MAD falls back to std. The estimator used is written into every signal. Both remain strict in a volatile window: the 23-09-2026 report (TLS +2.23 mb) stands down under either.

**Step 3 — Cushing tiered rule (replaces the Ω_cushing clamp formula entirely; grey zones closed in review #3):**

```
IF Cushing contradicts the headline TLS direction:
    Do NOT dampen and continue with a momentum trade.
    Route directly to Regime 2 (Physical Hub & Refining Divergence, §5) instead —
    divergence is a different trade thesis, not a scaled-down version of alignment.
    (Checked first — overrides every tier below.)

ELSE IF Cushing level < 22 MMbbl (near operational tank bottoms):
    Cushing_Multiplier = clamp( 1.5 + 0.5 × (22 − Cushing) / 12,  1.5,  2.0 )
    Reaches the full 2.0x ceiling at Cushing ≤ 10 MMbbl. Physical delivery squeeze
    mechanics — fires on absolute proximity to the physical floor, not on TLS agreement.

ELSE IF 22 ≤ Cushing level < 30 MMbbl (interpolation zone):
    Cushing_Multiplier = 1.0 + 0.5 × (30 − Cushing) / 8
    Linear interpolation between the two defined boundaries (1.5x at 22, 1.0x at 30) —
    no discretion, deterministic.

ELSE IF 30 ≤ Cushing level ≤ 45 MMbbl (normal range):
    Cushing_Multiplier = 1.0x. Weight Cushing equally with other PADD3 Gulf Coast
    hub data, volumetric basis only.

ELSE (Cushing level > 45 MMbbl):
    Cushing_Multiplier = 1.0x. No directional evidence at high Cushing levels from
    either review — neutral default, deterministic, just not modeled.
```

The 22-30 MMbbl interpolation, the >45 MMbbl neutral default, and the 10 MMbbl full-ceiling anchor for the <22 branch are **original to this doc**, added to close the judgment-call gap review #3 flagged — not sourced from either Gemini review.

*Implementation note:* "contradicts" is judged on Cushing's **actual weekly change** against the sign of the headline TLS (a build with a Cushing draw, or the reverse) — there is no Cushing consensus in the pipeline. **Materiality threshold (addition, 2026-09-30, after an external review):** the opposite move must be at least **1.0 mb**; anything smaller is "immaterial" and does not route to Regime 2 (otherwise a +0.1 mb Cushing build would flip a -8 mb crude draw into a fade). The 1.0 mb figure is a judgment call, not sourced. The Cushing **level** comes from EIA's public weekly table and is accepted only if its last two columns reproduce the scraped Cushing change (so last week's level is never used); if it is unavailable the multiplier is 1.0 and the signal says the level was unknown. Only the Cushing check is evaluated for Regime 2 routing — gasoline/crack-spread divergence is not.

**Step 4 — Price Scalar (unchanged):**

```
β_vol = (ATR_20 / 10) · sqrt(OVX / 30)
```

**Step 5 — Expected move:**

```
ΔP_expected = −TLS · β_vol · Cushing_Multiplier   (Cushing_Multiplier from Step 3, default 1.0)
```

**Step 6 — Sanity check against empirical anchor:** every 1.0 MMbbl unexpected TLS ≈ **$0.15–$0.30/bbl** move in the initial digestion window. If ΔP_expected is wildly outside this band, recheck inputs before trading it.

*Implementation note:* the engine reports the check (`sanity_ok`) and the Telegram message shows the anchor range next to the β_vol move; it does **not** veto the trade, since the runbook says "recheck", not "abort". Be aware the anchor and β_vol disagree in high volatility: at OVX ≈ 54 and ATR ≈ 4.8, β_vol × Cushing multiplier gives ≈ $0.9 per mb of TLS, three times the band's top, so the check will fail on most trades in that regime. That is a calibration question for the model, not a data error.

**Step 7 — Time-spread validation filter (new — required before any Regime 1 long):**

```
IF flat price rallies ≥ $0.40 on the print:
    REQUIRE CL1−CL2 to widen toward backwardation by ≥ $0.02–$0.04
    ELSE reject the long signal — a flat-price rally without spread confirmation
    is speculative financial flow, not physical demand, and fades fast.
```

*Implementation note:* needs a price move measured at the print; the pipeline has no live tick feed, so this stays a **manual checklist item** on the signal message.

**Step 8 — Dealer gamma (GEX) regime filter, with confidence gate (gate added in review #3):**

```
OI Concentration Ratio = OI at nearest strike / OI at 2nd-nearest strike

IF ratio ≥ 2.0 (one strike clearly dominant — reliable signal):
    Apply the full regime rule below.
ELSE (ratio < 2.0, ambiguous):
    Default to the conservative case regardless of apparent gamma sign — tighten
    targets to VWAP boundaries. Never widen targets on an ambiguous read.
```

- **Long Gamma** (price pinned between strikes, dealers buy dips/sell rips), ratio ≥2.0: tighten profit targets to pre-release VWAP boundaries, do not attempt momentum breakout trades — the market will mean-revert.
- **Short Gamma** (dealers net short options near the break level, delta-hedging accelerates moves), ratio ≥2.0: widen profit targets to 2.0×ATR, trail stops loosely behind 1-min swing pivots.

The concentration ratio test is **original to this doc**, not sourced from either review — a deterministic gate against acting on an ambiguous OI read, not a claim of new data access.

*Implementation note:* needs strike-level open interest from the option chain, which the pipeline does not have yet. **Manual until a chain feed exists**; without one, default to the conservative reading (tighten to VWAP).

---

## 4. Route to a Regime — 10:32:00 AM ET

Setup count reduced 4→3 — the old "Setup 4: Absorption Long" is deleted. Per the second review: crude builds paired with product draws typically resolve into low-liquidity churn, not a clean V-bottom bounce; there's no confirmed historical pattern supporting it as a distinct high-conviction setup.

```
                           EIA RELEASE REGIMES
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
  REGIME 1:                    REGIME 2:                    REGIME 3:
FULL ALIGNMENT              PHYSICAL SPREAD              EXPECTATION SKEW
(Momentum Continuation)       DIVERGENCE                ("Sell the Fact")
Crude + Products +        Headline vs. Cushing        EIA beats consensus but
Cushing draw together     or Products conflict         misses extreme API print
```

---

## 5. Regime Cards — 10:32:00–11:00:00 AM ET

### Regime 1 — Full Supply Chain Alignment (Trend Continuation)

| | |
|---|---|
| **Condition** | \|Z_TLS\| ≥ 1.25 (§3 Step 2), Cushing and refined products moving the **same direction** as the headline (no Step 3 divergence routing). Time-spread filter (§3 Step 7) confirms. |
| **Entry** | Wait for the initial 2-min auction to settle. Limit order on retest of the broken pre-release boundary (P_high for longs, P_low for shorts), confirmed by aligned CVD. |
| **Stop** | 1.5×ATR_1min from execution price, or outside the VWAP ±1.5σ band from §1 — whichever is wider. Minimum stop width $0.18–$0.35. |
| **Target** | P_entry + ΔP_expected under Long Gamma; 2.0×ATR trailing under Short Gamma (§3 Step 8). |

### Regime 2 — Physical Hub & Refining Divergence (The Fade)

Fires whenever Step 3 routes here: headline direction contradicted by Cushing and/or gasoline. Direction-agnostic — the same fade logic applies whichever way the headline pointed.

| | |
|---|---|
| **Condition** | Headline crude draws, but Cushing builds OR gasoline builds significantly, with 3:2:1 crack spread falling — **or the mirror**: headline crude builds, but Cushing/products draw. The crude-build/product-draw variant is the lower-conviction, higher-chop-risk direction — the deleted old "Setup 4" pattern lives on here only as a same-mechanics fade, not a distinct V-bottom-absorption thesis. Size it down or skip without a clean confirmation candle. |
| **Entry** | Do not trade the initial spike. Wait for it to stall at higher-timeframe resistance/support (prior daily high/low, Value Area High/Low). Enter on the 1-min candle closing back inside the pre-release range, against the initial spike direction. |
| **Stop** | Same as Regime 1: 1.5×ATR_1min or outside VWAP ±1.5σ, whichever wider, min $0.18–$0.35. |
| **Target** | Pre-release range low/high (opposite side from entry direction) and session Volume Profile POC. |

### Regime 3 — Expectation Skew / "Sell the Fact"

| | |
|---|---|
| **Condition** | Tuesday API printed an extreme draw, driving an overnight rally >$1.00. EIA prints a draw that beats official consensus but misses the API whisper. The physical draw is already fully priced in. *(Implemented as: TLS < 0, EIA crude change below consensus, API crude more than 3.0 mb below consensus, the EIA draw smaller than the API draw, **and** a measured WTI rally of more than +$1.00 from Tuesday 16:30 ET to just before the print, taken from Yahoo 5-minute bars. If the rally can't be measured Regime 3 does not fire.)* |
| **Entry** | Short on confirmed cross below the 09:00 AM cash-open VWAP (revised from the prior "2-min candle below 10:29 close" — cash VWAP is the cleaner reference per this review). |
| **Stop** | Same ATR/VWAP-band rule as Regime 1/2. |
| **Target** | Liquidity pool below Asian session low (window still not formally defined anywhere in the source model — treat as the overnight low on your 1-min chart before the 10:00 AM reference freeze). |

**Order-type quick table:**

| Regime | Order Type | Invalidation |
|---|---|---|
| 1 (Full Alignment) | Limit on retest of P_high/P_low | 1.5×ATR_1min or outside VWAP ±1.5σ, whichever wider |
| 2 (Divergence Fade) | Limit on close back inside pre-release range | Same as above |
| 3 (Expectation Skew) | Short on confirmed cross below 09:00 AM cash VWAP | Same as above |

---

## 6. Risk Mandates (hard rules, no exceptions)

**Position sizing (new — scales with the now-wider stops):**

```
Contract Size = floor( (Account Equity × 0.01) / (Stop Distance in $ × 1,000) )
```

(1,000 = NYMEX CL multiplier — 1,000 bbl/contract. For MCX, use 100 instead of 1,000, §13/Prerequisites.) Because stops widened roughly 10x from the original 1-2 tick assumption, position size must scale down proportionally to hold the same 1.0% account risk.

```
Maximum Risk Capital per Event:   1.0% of Total Trading Book Equity (all regimes — Regime 2's
                                   crude-build/product-draw variant should be sized down manually
                                   within that cap per its own lower-conviction note above)
Stop-Loss Anchor:                 1.5×ATR_1min or outside VWAP ±1.5σ band, whichever wider
                                   Minimum stop width: $0.18–$0.35 (18–35 ticks)
Order Routing:                    No unhedged market orders on entry. Limit orders only, on
                                   pullbacks to liquidity nodes (pre-release range edge, 1-min VWAP)
Slippage Budget:                  6–10 ticks ($0.06–$0.10) on stop-loss executions — build this
                                   into the strategy's expected-value calc, not just as a stop buffer
Time-Based Stop Invalidation:     35 minutes post-release (11:05 AM ET)
Drawdown Hard Circuit-Breaker:    Daily desk loss limit = 2.5% max equity drawdown
```

**Operational failure protocols (new):**

| Scenario | Response |
|---|---|
| Data delay / server glitch (no verified feed by 10:30:15 AM ET) | Cancel all resting orders, abort the session. Delayed releases produce erratic, illiquid whipsaws. |
| Conflicting data feeds (primary vs secondary mismatch) | Hard lock execution for 3 minutes until official EIA tables confirm final numbers. |
| Macro data overlap (FOMC day) | Close all EIA positions by 12:00 PM ET regardless of target status — avoid cross-asset macro liquidation. |
| Weekly options pinning (price within $0.15 of a heavy-OI strike, e.g. $75.00) | Reduce expected-move calculation (ΔP_expected) by 40% before setting targets. |

**On missing trades to the limit-order mandate (confirmed, not changed, in review #4):** a true Regime 1 blowout can trend straight through without offering a VWAP/range-edge retest — the limit order never fills and the trade is missed, including some of the year's biggest moves. **This is correct behavior, not a bug. Do not override it with a market order.** Missing a trade costs nothing; chasing a runaway market into a 5-10 tick spread costs real equity. If you catch yourself reaching for a market order because "this one's too good to miss," that's the exact moment the rule is protecting you from.

- **Chop exit:** price oscillates VWAP ±$0.05 for >8 min with no directional resolution → close at market.
- No trade clears the §1 scorecard *and* the §3 model *and* still doesn't fit one of the three regimes → **no trade**. Absence of a clean regime is a valid, expected outcome most weeks.

---

## 6.1 Validation Protocol Before Scaling Size (added, review #4)

Nothing in this spec is backtested — every threshold across all three rebuilds is reasoned-from-principles or LLM-asserted, not empirically validated on this system's own track record. This is the concrete, falsifiable check that closes that gap before real capital goes behind it:

1. **Trade minimum size only:** 1 NYMEX lot, or the MCX micro-equivalent (§13/Prerequisites) — whichever venue you're actually routing through.
2. **Run live for the next 3 EIA Wednesdays**, no exceptions, no skipping a week because the setup "doesn't feel right" (unless §1's scorecard/§3's model genuinely says no-trade, per the rules above — that's a valid outcome, not a skip).
3. **Track two numbers against their budgeted values:**
   - Actual fill slippage vs the 6–10 tick budget (§6).
   - Actual data-to-decision latency vs the sub-500ms pipeline assumption (§5.3) — does your provider and broker combination actually deliver this, or is the automation architecture aspirational?
4. **Decision rule:** if both numbers hold up across the 3-week sample, scale to standard book size. If either one is meaningfully worse than budgeted, the position-sizing formula (§5.2) and/or the slippage budget (§6) need to be recalibrated to the *observed* numbers before scaling — not the model's asserted ones.

This is real-time forward validation, not historical backtesting — it doesn't replace checking the model's thresholds (Z≥1.25, Cushing tiers, stop widths) against actual past WPSR prints, which remains a separate, still-open task (README Open Items).

---

## 7. Companion Confirmation — Cross-Check Before/After

| Report | Timing | Use |
|---|---|---|
| API WSB | Tue 4:30 PM ET | Precursor — already captured in §0 above |
| Baker Hughes Rig Count | Fri 1:00 PM ET | 3–6 month supply trend, doesn't affect this week's trade but updates next week's regime read |
| CFTC COT | Fri 3:30 PM ET | Managed Money positioning — crowded net-long → bullish EIA prints often sell off instead of rallying |
| OPEC MOMR | Monthly, 2nd week | Global S/D context, OPEC+ quota compliance |
| IEA OMR | Monthly, 2nd week | Consumer-side counterweight to MOMR, most authoritative OECD inventory source |
| DXY | Continuous | Surging dollar = headwind even on a bullish draw |

Full detail on all of these, plus access routes and exact methodology: README §9.

---

## 8. Reference Links

- EIA WPSR: https://www.eia.gov/petroleum/supply/weekly/
- EIA release schedule (holidays): https://www.eia.gov/petroleum/supply/weekly/schedule.php
- API WSB: https://www.api.org/energy-insights/statistics/wsb
- NYMEX WTI (CL) contract specs: CME Group

**Trading via MCX instead of NYMEX (README §13):** lot size 100 bbl, tick ₹1 (₹100/tick P&L), expiry 19th/20th of month, cash-settled. NRML margin ≈9% of contract value, MIS ≈4.5% — ad-hoc margin can jump to ₹100,000 during high volatility, exactly what this release causes. In the position-sizing formula (§6), use a 100 multiplier instead of 1,000. Check margin isn't about to spike before sizing. MCX follows WTI spec, not Brent — the model in §3 applies directly, no basis adjustment.

**USD/INR currency basis risk (review #5, MCX-only):** ΔP_expected is a USD/bbl figure from NYMEX inputs — it does not account for the rupee itself moving. A sharp USD/INR move during the release window (RBI action, a concurrent major USD event) can decouple the MCX price from what the model predicts, independent of whether the NYMEX-side calc is right. **Before sizing an MCX position, confirm USD/INR isn't itself in an active volatility event.** If it is, skip or size down regardless of what §3 outputs — the model has no way to separate "WTI surprise" from "rupee move" in the price you'll actually see on MCX.

---

## 9. Formula Appendix (copy-paste block)

```
TLS = ΔS_crude + ω_g·ΔS_mogas + ω_d·ΔS_dist
  ω_g = 0.67 (→0.80 May–Sep)   ω_d = 0.50 (→0.70 Nov–Feb)

Z_TLS = TLS / σ_forecast (rolling 12 weekly TLS; 1.4826×MAD by default, plain std dev via SIGMA_METHOD=std; ≥ 8 weeks) → trade only if |Z_TLS| ≥ 1.25

Cushing tiered rule (replaces Ω_cushing clamp):
  contradicts headline  → route to Regime 2, no momentum scaling (checked first)
  <22 MMbbl            → clamp(1.5 + 0.5×(22−Cushing)/12, 1.5, 2.0)
  22–30 MMbbl           → 1.0 + 0.5×(30−Cushing)/8  (interpolation, deterministic)
  30–45 MMbbl           → 1.0x (normal)
  >45 MMbbl             → 1.0x (neutral default, undocumented territory)

GEX confidence gate: OI ratio (nearest/2nd-nearest strike) ≥2.0 → apply regime rule;
  <2.0 → default to tighten-to-VWAP regardless of apparent gamma sign

β_vol = (ATR_20/10) · sqrt(OVX/30)

ΔP_expected = −TLS · β_vol · Cushing_Multiplier

Time-spread filter: rally ≥$0.40 requires CL1−CL2 widen ≥$0.02–$0.04, else reject long

Position Size = floor( (Equity × 0.01) / (Stop Distance$ × 1,000) )   [NYMEX; ×100 for MCX]

Net Domestic Balance = Commercial Crude Δ − SPR Transfer Δ

Crack Spread = [(2×RBOB×42) + (1×ULSD×42) − (3×WTI)] / 3

Stops (all regimes): max(1.5×ATR_1min, outside VWAP ±1.5σ band), min $0.18–$0.35
```

---

## Summary of Changes (this rebuild vs the original runbook)

```
Component           Original Runbook                    This Version
──────────────────────────────────────────────────────────────────────────────────
Stop Losses         1–2 ticks ($0.01–$0.02)             1.5×ATR / VWAP±1.5σ ($0.18–$0.35)
Slippage Budget     3 ticks ($0.03)                     6–10 ticks ($0.06–$0.10)
Cushing Model       Ω_cushing clamp(0.5x–1.8x)          Tiered absolute-capacity rule
Trade Filtering     Static ±2.0 MMbbl cutoff            Rolling Z-score, |Z|≥1.25σ
Setup Architecture  4 symmetrical setups                3 physical market regimes
Data Ingestion      Assumed manual/visual                Programmatic, <500ms calc latency
Validation Gates    Isolated inventory numbers           + CL1−CL2 time-spread confirmation
Options Mechanics   Brief pin-risk mention               Explicit GEX regime filter + 40% haircut rule
Position Sizing     Flat 1.0%/0.5% by setup              Formula scaling with stop distance
Failure Protocols   Ad hoc notes                         Formal table (delay/conflict/FOMC/pinning)
σ_forecast          Rolling std dev (this doc, v4)       1.4826×MAD by default (2026-09-30, outlier-robust)
```

---

## Implementation status (app/code, 2026-09-30)

What the repo does versus what is still by eye. Nothing here is backtested, and none of it has traded real money.

| Runbook item | Status | Where |
|---|---|---|
| Consensus (crude, gasoline, distillate) | Automated: tradingeconomics and investing.com race | `app/consensus_fetcher.py` |
| Tuesday API report, API surprise flag (> 3.0 mb) | Automated for **crude only** (the API's Cushing/gasoline/distillate are paywalled; the free copies lag); flag is in the signal scorecard | `app/api_monitor.py` |
| The print (four stock changes, refinery util change) | Automated, polling from 10:30 ET | `app/eia_actuals.py` |
| Cushing level | Automated from EIA's public table, cross-checked to the change | `app/utils/eia_levels.py` |
| ATR_20, OVX, CL1−CL2, 3:2:1 crack, Brent−WTI, DXY | Automated, yfinance (must be run before the print) | `app/market_data.py` |
| σ_forecast | Automated from a local weekly history (seed once, then self-appending) | `app/surprise_history.py`, `app/model.py` |
| TLS, Z gate, Cushing rule, β_vol, ΔP_expected, regime routing | Automated, pure functions | `app/model.py`, `app/signal_engine.py` |
| Scorecard (backwardation, crack, Brent−WTI, API) | Reported, **informational**: the runbook does not say how a miss changes the trade | `app/signal_engine.py` |
| Step 7 time-spread filter | **Manual** (needs a live price move at the print) | signal checklist |
| Step 8 dealer-gamma gate | **Manual** (needs strike-level OI, no chain feed yet) | signal checklist |
| Entries, real stop (1.5×ATR_1min / VWAP ±1.5σ), scaling out, chop exit | **Manual** on the chart | signal checklist |
| 5-year seasonal context, misread checks (§1) | **Manual** | — |
| Alert to Telegram; failure alerts from every script | Automated | `app/telegram_bot.py`, `app/utils/telegram.py` |

Not built yet: scheduling, the Tuesday pre-brief message, an option-chain feed, a trade journal / slippage log for §6.1, and a hard-exit reminder.

---

*Model derivation, reconciliation history, macro background, glossary, and all source citations: [README.md](README.md). This model is backed into README §2/§5/§6 as canonical — both files are in sync.*
