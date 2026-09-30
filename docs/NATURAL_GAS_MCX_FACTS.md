# MCX natural gas: contract facts and blueprint review

Written 2026-09-30 for v0.2. Sources: MCX Natural Gas (1,250 MMBtu) and Natural Gas Mini (250 MMBtu) options specifications (March 2026 contract onwards, Circular MCX/TRD/103/2026, 2 March 2026); the matching futures specifications (January 2026 contract onwards, Circular MCX/TRD/320/2025, 27 June 2025); the Natural Gas Mini leaflet and hedging brochure. Nothing here is built yet. The crude equivalents are in `CLAUDE.md` ("MCX contract facts").

## 1. Contract facts

| | NATURALGAS | NATGASMINI |
|---|---|---|
| Options underlying | NG futures, 1,250 MMBtu | NG mini futures, 250 MMBtu |
| Trading unit (one lot) | 1,250 MMBtu | 250 MMBtu |
| Quotation | Rs per MMBtu | Rs per MMBtu |
| Option type | European call and put | same |
| Strikes | **40 ITM, 1 near, 40 OTM (81 CE, 81 PE)** | same |
| Strike interval | **Rs 5** | same |
| Options tick | Rs 0.05 | Rs 0.05 |
| Futures tick | Rs 0.10 | Rs 0.10 |
| Session | 09:00 to 23:30 IST in US DST, 23:55 otherwise (crude's circular showed 23:55 from 2 Nov 2026; assume natural gas follows, confirm) | same |
| Options position limit (individual) | 1,20,00,000 MMBtu or 5% of market OI | same |
| Futures position limit | 60,00,000 MMBtu | same |
| Max futures order | 60,000 MMBtu | same |

Everything else matches crude options and is unchanged:
- The last trading day is **two business days before the underlying futures' expiry**.
- The buyer's premium is blocked upfront in full, and there is no margin for a buyer.
- Pricing is Black-76 and the base price is the previous settlement.
- Premium settles T+1, and mark-to-market gains on options are not paid in cash.
- **Positions devolve into futures at expiry** (long call becomes long futures, long put becomes short futures, opened at the strike). ITM options auto-exercise unless you give a contrary instruction. Never hold to expiry.
- Futures daily price limit: 4%, relaxed to 6%, then 9% after a 15-minute cooling-off.
- SPAN margins are recomputed at 09:30, 11:00, 13:00, 15:00, 17:00, 19:00, 20:30 and 22:30 and at end of day. A pre-expiry margin can be levied on option positions in the last days.
- The futures final settlement is cash: NYMEX NG front-month settlement × the last RBI USD/INR reference rate.

### 2026 calendar

Futures expiry is from the launch calendar. Option expiry is my computation: two business days earlier, counting the four full-day MCX closures (Republic Day, Good Friday, Gandhi Jayanti, Christmas) as non-business days, as for crude.

| Month | Futures expiry | Options expiry |
|---|---|---|
| Jan | Tue 27 | Thu 22 |
| Feb | Tue 24 | Fri 20 |
| Mar | Thu 26 | Tue 24 |
| Apr | Mon 27 | Thu 23 |
| May | Tue 26 | Fri 22 |
| Jun | Thu 25 | Tue 23 |
| Jul | Tue 28 | Fri 24 |
| Aug | Wed 26 | Mon 24 |
| Sep | Fri 25 | Wed 23 |
| Oct | Tue 27 | Fri 23 |
| Nov | Tue 24 | Fri 20 |
| Dec | Mon 28 | Wed 23 |

Options for a month launch three months before their expiry month, on the business day after the near-month futures expire. The futures list up to 6 months (the older leaflet says 3). These dates are not the crude dates and there is no shared calendar, so natural gas needs its own table in `options.py`. Confirm them against MCX's calendar before trading.

### What the contract sizes mean in money (estimate, Rs 96/USD)

At NG ~$3.4, the futures are about Rs 326 per MMBtu:
- A NATURALGAS lot has a notional of ~Rs 4.1 lakh.
- A NATGASMINI lot has a notional of ~Rs 82,000.
- The strike range is only Rs 200 ITM (40 × Rs 5), which is about 60% of the futures price. That is enough for a 0.80-0.85 delta strike at any plausible implied volatility.
- A deep-ITM premium is roughly Rs 50-60 per MMBtu: about **Rs 65,000 per full lot and Rs 13,000 per mini lot** (Black-76 estimate; check on the chain).
- One Rs 1 move in the futures is Rs 1,250 (full) or Rs 250 (mini), times delta.

## 2. Review of the pasted blueprint

The structure fits (single-variable print, storage vs the 5-year average, weather dominance). Several parts should not be built as written.

**Adopt**
- **A different model from crude.** Surprise in Bcf = actual − consensus, sigma from a rolling window of past surprises, then Z. It maps straight onto `model.z_score` and `sigma_forecast`; only the inputs change (one number, not TLS).
- **The 5-year context** (deficit or glut against the seasonal average) as the analogue of the Cushing multiplier.
- **Stand down below the gate**, the same as crude.
- **Strike deepening at high implied volatility**, the same as the crude OVX rule.
- **No overnight hold** (devolution and gap risk).

**Change**

| Blueprint | Problem | Proposal |
|---|---|---|
| Gate \|Z\| ≥ 1.5 "because consensus is loose" | Reasoning is right, the number is a guess. A looser consensus already inflates sigma, which raises the bar by itself | Start at 1.25 like crude; set from the measured history, not by argument |
| $\Omega_{5Y}$ 1.5-2.0 for deficit/glut, 0.5 for contradiction | Thresholds (−5%, +10%, −2%, +5%) are unsourced and inconsistent between the section 1 clamp and Regime 1/2 | One table in code, marked unproven like `beta_vol`; multipliers feed only the message |
| "NG OVX" for the strike rule | There is no NG OVX. Yahoo/CBOE do not publish natural gas implied volatility | Use the realised volatility of NG=F (20-day) as a proxy, labelled as such; the real number is on the MCX option chain (manual) |
| Prompt spread backwardation > +$0.05 = bullish | NG is usually in contango; this filter would rarely be true | Show NG1−NG2 as information only |
| LNG feedgas, dry gas output, HDD/CDD forecasts | No free machine-readable source; the weather forecast is the main driver, and the blueprint says so | **Manual checklist items**, not automated inputs |
| Regime 1 "extreme temperatures forecast", Regime 2 "forecast flipped overnight" | Depends on weather data the pipeline cannot read | Regime 1 and 2 need the trader's confirmation; the engine can only offer the storage side |
| Regime 2 fades a "≤ −10 Bcf" draw when in a glut | A −10 Bcf surprise is enormous (typical surprise is 3-6 Bcf); rarely fires | Reuse the crude Regime 2 trigger: the context contradicts the headline |
| Stop = 1.0% of desk equity | Equity sizing was removed in v0.1 | Keep the lot-count model (`MCX_NATURALGAS_LOTS`, `MCX_NATURALGASM_LOTS`) and show INR lost at the stop |
| Hard exit 22:45 IST | Crude uses print + 2.5 h capped an hour before the close (22:30 summer, 22:55 winter) | Same rule; fixed times are wrong in winter |
| Stop 2.0 × 1-minute ATR, scale out half on first thrust, retrace 80% in 15 minutes | Useful trading rules, none verifiable here | Put on the manual checklist |

**Crude machinery that does not carry over**
- No API report, so no pre-positioning check and no Regime 3.
- No gasoline/distillate weights, no Cushing.
- The Thursday release moves to Wednesday around some US holidays (Thanksgiving, Juneteenth, July 4th). The release-date logic must not assume Wednesday.
- The print is Thursday 10:30 ET, so 20:00 IST in summer and 21:00 IST in winter (the same clock offset as crude). A separate weekly schedule is needed alongside crude's.

## 3. What v0.2 would have to build

1. **Consensus and actuals for storage** from tradingeconomics/investing.com (check that both carry the indicator and the row shapes match; new slugs in `app/scraper/sources.py` only). `consensus.json` and the actuals file need a commodity dimension or separate files (names in `common.py`).
2. **Storage level and 5-year average.** The EIA weekly storage table is public (the crude Cushing level uses the same idea, `utils/eia_levels.py`). Check the file and its structure first; if it needs a key, a paid source is out.
3. **Surprise history** for storage (backfill from investing.com); needs at least 8 weeks like crude.
4. **Market data:** NG=F price, ATR(20), NG1−NG2 (contract symbols `NG<month><yy>.NYM`), realised volatility proxy.
5. **Model:** a new pure module (Bcf surprise, Z, 5-year multiplier, regimes); reuse `z_score`, `sigma_forecast` and the delta rule.
6. **Options:** NG expiry calendar, Rs 5 strike interval, contract sizes 1,250 and 250 (`CONTRACT_BARRELS` becomes per-unit sizes), strike guide from Black-76.
7. **Signal, message, schedule** for Thursday; two more workflows (Thursday pre and print), and the DST cron notes.
8. **Lot settings:** the existing `MCX_NATURALGAS_LOT_SIZE` (1250) and `MCX_NATURALGASM_LOT_SIZE` (250) are reserved for the contract sizes; add `MCX_NATURALGAS_LOTS` and `MCX_NATURALGASM_LOTS` for counts, as for crude.

## 4. Open questions for MCX

- Natural gas session close after 2 Nov 2026 (crude's circular says 23:55).
- Whether weekly options exist for natural gas.
- Confirm the mini symbol as listed for options (`NATGASMINI`) against the broker's instrument master.
- MCX's definition of a business day on morning-only holidays (same open question as crude).

## 5. Feasibility check (2026-09-30)

Read-only checks against the live sources. No pipeline code changed.

| Input | Result |
|---|---|
| **tradingeconomics** `united-states/natural-gas-stocks-change` | Loads. Shows 3 rows (previous, latest, next) with actual, previous and consensus. Values carry a `Bcf` suffix (`53Bcf`), which `calendar.to_mb_suffixed` does not parse, so today the rows come back with `actual`/`consensus` = `None`. The stats table works (53.0). The `united-states/natural-gas-stocks` slug does not exist. |
| **investing.com** `natural-gas-storage-386` | Loads. About 10 weekly rows with actual, forecast (= consensus) and previous. Values carry a `B` suffix (`53.00B`), also unparsed today. |
| **EIA storage table** `https://ir.eia.gov/ngs/wngsr.csv` | Public, no key. Needs `follow_redirects=True` (302 to a signed URL). Gives total stocks (3,351 Bcf), net change (+53), the year-ago figure and the **5-year average (3,256 Bcf, stocks 2.9% above it)**, so the deficit/glut context can be computed. The `ngs.csv` variant is 403. |
| **Yahoo `NG=F`** | 5-minute bars for the last 60 days (all of the last 8 prints), enough to measure post-print moves. |

**Consensus panels differ.** For the 24-09 release investing.com's consensus was 50 Bcf and tradingeconomics' was 53 (equal to the actual), so the surprise is +3 or 0 depending on the source. Crude showed the same effect (-0.6 vs -0.7). Backfilled history (investing.com) and live values (TE) would mix panels, as for crude.

**Surprise size.** Over the 8 investing.com weeks: standard deviation of actual minus consensus 3.8 Bcf, mean absolute 3.3 Bcf. At the 1.25 gate a trade needs about 4.6 Bcf, roughly one week in four.

**Anchor test (NG=F, 5-minute bars, the 8 prints from 6 Aug to 24 Sep):**

| Release | Surprise (Bcf) | 5 min | 30 min | 60 min |
|---|---|---|---|---|
| 06-08 | +3 | -0.016 | -0.024 | -0.001 |
| 13-08 | +5 | -0.016 | -0.004 | +0.002 |
| 20-08 | +1 | -0.008 | -0.026 | -0.025 |
| 27-08 | -4 | **-0.038** | -0.044 | -0.052 |
| 03-09 | 0 | -0.003 | -0.066 | -0.095 |
| 10-09 | +5 | **+0.003** | +0.007 | +0.020 |
| 17-09 | -5 | **-0.018** | -0.019 | -0.022 |
| 24-09 | +3 | -0.006 | +0.089 | +0.148 |

- A bullish surprise (negative) should lift the price. The move went the **expected way in 4 of the 7 non-zero prints and the wrong way in 3**, and the three wrong ones are the large ones (27-08, 10-09, 17-09).
- The fitted slope is **-0.0009 USD per Bcf at 5 minutes** (correlation -0.61, wrong sign), against the blueprint's +0.003 to +0.005. n = 8, so this proves nothing either way, but **it gives no support to the anchor**.
- The first-5-minute moves are small in absolute terms (0.003 to 0.038 USD, or Rs 0.3 to 3.6 a MMBtu); the 60-minute ranges are 0.04 to 0.18. Weather forecasts, not the storage number, seem to be doing the work.
- Caveats: 8 prints, one season (summer injections), investing.com's consensus panel, a continuous front-month series (27-08 is a contract-roll day).

**Read-out.** The inputs exist and are free (steps 1 and 2 of the plan are feasible: two consensus sources, the EIA level and 5-year average, price bars). The **edge is not demonstrated**: in the only measurable sample the surprise did not predict the first-5-minute direction. Do not build the model (steps 3-8) until a larger sample, or Gemini answers to questions 1, 5 and 6, say otherwise. Cheap next checks: keep logging each Thursday's print and price path (the journal does this for crude) so the sample grows by one a week; the 60-day bar window means the earliest of these 8 drops out in early October.

## 6. Gemini's answers (2026-09-30) and what we did with them

Gemini reviewed the plan and answered the twelve questions. Treat every figure below as an unverified claim: none of it comes from data we hold.

**Not applicable to this system** (the review seems to have assumed a different codebase): a hardcoded expiry on the 19th (we use MCX's dated calendar), automatic order execution (there is none), Regime 3 fired from inventory alone (it needs a measured rally, and natural gas has no Regime 3), Cushing `None` forcing a trade, `omega_distillate` and `beta_vol` (crude parts; nothing built for natural gas).

**Unreliable:**
- The anchor is inconsistent across two Gemini answers: 0.003-0.005 USD per Bcf earlier, **0.007-0.015** now (0.012-0.015 in winter). Neither matches the 8-print measurement (section 5: about -0.0009 at 5 minutes, wrong sign). The cited paper (Linn and Zhu) is real but concerns volatility around storage reports; it does not establish a per-Bcf slope.
- Spreads (Rs 2-3 deep-ITM before the print, Rs 5-8 after) and implied-volatility levels (60-75% before, 45-50% after) are unsourced. They are plausible, and they show why the trade may not pay, but nobody here has seen the MCX chain.
- "By minute 2, 80-90% of the move is done" is consistent with our data (small, undirected 5-minute moves) but not proven either way.
- Holidays: Gemini says a holiday delays the report to Friday. My understanding is that EIA moves it to Wednesday (Thanksgiving week). Check EIA's schedule; the recorder does not assume a weekday.
- "Refuse to trade on scraped feeds": the seconds of scraper delay are small next to a manual order placed minutes after the print, and a sub-millisecond feed would not help a manual trader. The underlying worry, that the move is over before a human can act, is real and is what the recorder measures.

**Adopted:**
- **Implied flow versus net change.** EIA's table carries both. The recorder stores both and flags `reclassified` when they differ by more than 0.5 Bcf. Any future rule aborts on it.
- **Both consensus panels are kept** (tradingeconomics and investing.com), since they differ (24-09: 53 and 50).
- **A pre-print spread limit, and an option stop that adds spread and IV crush** are manual checklist items when there is anything to trade (they need chain quotes we do not have).
- **Seasonal sigma:** a 12-week window mixes seasons and a 52-week window is not obtainable (about 10 weeks of history). Recording a year forward is the only fix.
- **A sample of at least two years** is the honest bar; nothing can be backfilled to it.

**Not adopted:** mean-centring the Z-score (fair test, but with 8 weeks it adds noise), a fixed 0.01 USD per Bcf scalar (it would replace one unmeasured number with another), the 52-week window, and the USD/INR intraday veto (the onshore rupee market is shut during the hold).

**Decision: record, do not trade.** `app/ng_recorder.py` (section 7) writes each Thursday's print and price path. No model, signal, sizing or alert. Revisit after roughly 20-26 weeks (about March 2027). The full model (steps 3-8 of the plan) is on hold until a measured slope beats round-trip costs.

## 7. The passive recorder (built)

`python -m app.ng_recorder` writes `data/ng_record.json` (`NG_RECORD_FILE`), one record per release date, merged so a later, poorer run never erases data:

| Field | Source |
|---|---|
| `actual_bcf`, `previous_bcf`, `actual_agrees` | tradingeconomics, investing.com (the actuals are compared) |
| `consensus_bcf`, `surprise_bcf` | one entry per site (the panels differ) |
| `eia` | EIA's storage table: stocks, net change, implied flow, `reclassified`, year-ago, 5-year average and the % against it (kept only if EIA's file is for that release; it only ever holds the latest week) |
| `price` | NG=F after the print: the pre-print close, the price at 0/1/2/5/10/15/30/60 minutes, moves from the pre-print price, first-hour high and low. 1-minute bars if the release is under a week old, else 5-minute (no 1- and 2-minute prices), and none after about 55 days |

- `--date DD-MM-YYYY` records a specific release; `--backfill` records every release investing.com still lists (the 8 prints from 6 Aug to 24 Sep are in `data/ng_record.json`, though `eia` exists only for the latest); `--show` prints the surprises against the moves and the fitted slope once there are 3 or more.
- `.github/workflows/twpr_ng_record.yml` runs it Thursdays 17:00 UTC (22:30 IST, so the hour of price bars is complete in summer and winter) and commits the file. Like the crude workflows it runs only from `main`, and it needs the same `production` environment secrets (Telegram only).
- It alerts Telegram only on failure. `to_mb_suffixed` now reads `Bcf` and `B` suffixes and `sources.py` has the `ng_storage` slugs.
- The recorder needs an unattended Thursday to have run a few times before anyone trusts the record. It cannot capture what only the MCX chain shows (spreads, premiums); that stays a manual note until a chain feed exists.
