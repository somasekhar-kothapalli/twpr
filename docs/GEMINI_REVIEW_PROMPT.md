# Prompt for an external review (paste everything below the line into Gemini)

---

You are a skeptical crude-oil options trader and quant reviewing a systematic strategy I have implemented in Python. **Real money is meant to trade on this.** I am not looking for approval. Your job is to find what is wrong, unjustified, or dangerous. The strategy is **not backtested**; I have about 9 weeks of usable history.

Rules for your answer:
- Rank findings by how much money they could lose me (worst first).
- For every claim give a worked example or a source. If you are guessing, write "GUESS".
- Do not praise. If a part is fine, say "no issue" in one line and move on.
- Where I ask for a number (a threshold, a window, a rule), give one, with reasoning, not "it depends".
- Earlier reviews of this model (by an LLM) were mostly complimentary and missed real bugs. Assume this version has bugs too.

## 1. What the system does

A weekly options **buyer** (never seller) trade on **MCX CrudeOil options (INR)**, triggered by the EIA Weekly Petroleum Status Report (Wednesday 10:30 ET = 20:00 IST in US summer time, 21:00 IST in winter). The report is compared with analyst consensus; a large enough surprise, confirmed by Cushing, is traded with a deep in-the-money option, sized to risk 1% of equity.

Data comes from scraping (tradingeconomics.com and investing.com, seconds after the print), not a low-latency feed. All entries are limit orders on a retest minutes after the print, so I am not racing the print.

## 2. The model (exactly as coded)

```
ΔS_x   = actual_x − consensus_x           x ∈ {crude, gasoline, distillate}, million barrels
TLS    = ΔS_crude + ω_g·ΔS_gasoline + ω_d·ΔS_distillate
           ω_g = 0.80 May–Sep else 0.67      ω_d = 0.70 Nov–Feb else 0.50   (by release month)
σ      = spread of the last 12 weekly TLS values (needs ≥ 8); default 1.4826 × median absolute deviation
Z      = TLS / σ                          trade only if |Z| ≥ 1.25, else STAND DOWN

Cushing contradicts  = (TLS > 0 and Cushing weekly change < 0) or (TLS < 0 and Cushing weekly change > 0)
Cushing multiplier   = level < 22 : clamp(1.5 + 0.5·(22−L)/12, 1.5, 2.0)
                       22 ≤ L < 30: 1.0 + 0.5·(30−L)/8
                       L ≥ 30     : 1.0

β_vol  = (ATR_20 / 10) · sqrt(OVX / 30)          ATR_20 = mean true range of last 20 daily WTI bars
ΔP     = −TLS · β_vol · Cushing multiplier       expected WTI move, USD/bbl (Regime 1 only)
sanity = 0.15 ≤ |ΔP| / |TLS| ≤ 0.30              "USD per mb" empirical anchor (flag only, no veto)

Regime (checked in this order):
  |Z| < 1.25                     -> stand down
  Cushing contradicts headline   -> Regime 2: FADE (trade opposite the headline; enter after the spike stalls)
  Regime 3 (sell the fact)       -> TLS < 0 and crude_change < crude_consensus
                                    and (api_crude − crude_consensus) < −3.0 and crude_change > api_crude
                                    -> PUT.  (The "overnight rally > $1.00" leg is checked by hand.)
  otherwise                      -> Regime 1: with the headline (TLS > 0 build -> PUT, TLS < 0 draw -> CALL)

Option: ITM only. Delta 0.60–0.70, or 0.80–0.85 if OVX > 35.
Expiry: nearest 19th of the month, next month if ≤ 5 days remain (the "19th" is an ASSUMPTION).
Size:   lots = floor( equity·1% / (futures_stop_USD · USD/INR · delta · 100 bbl) )
        for futures stops of $0.18 / $0.25 / $0.35 (real stop is read off the chart:
        1.5×ATR_1min or outside VWAP ±1.5σ, whichever is wider), delta = middle of the range.
Exits (manual): 35-min time stop, 4-min chop exit, hard exit 2.5 h after the print, scale out 50% on first thrust.
Not automated: CL1–CL2 time-spread filter, dealer-gamma (OI concentration) filter, INR/RBI abort,
geopolitical 50% size-down, strike selection (no option-chain feed).
```

Key code (Python):

```python
def cushing_multiplier(level_mb):
    if level_mb is None: return 1.0
    if level_mb < 22:  return min(2.0, max(1.5, 1.5 + 0.5 * (22 - level_mb) / 12))
    if level_mb < 30:  return 1.0 + 0.5 * (30 - level_mb) / 8
    return 1.0

def sigma_forecast(history, weeks=12, min_weeks=8, method="mad"):
    values = [tls_of(row) for row in last_12_weeks(history)]     # each week uses its own month's weights
    if method == "mad":
        median = statistics.median(values)
        mad = 1.4826 * statistics.median(abs(v - median) for v in values)
        if mad > 0: return mad
    return statistics.stdev(values)

def classify(tls, z, cushing_is_contradicting, crude_change, crude_consensus, api_crude):
    if abs(z) < 1.25: return None, "neutral"
    headline = "bearish" if tls > 0 else "bullish"
    if cushing_is_contradicting: return 2, ("bullish" if headline == "bearish" else "bearish")
    api_surprise = api_crude - crude_consensus
    if tls < 0 and crude_change < crude_consensus and api_surprise < -3.0 and crude_change > api_crude:
        return 3, "bearish"
    return 1, headline
```

## 3. The data I actually have

Weekly surprises (actual − consensus, mb) and the resulting TLS. Consensus for these rows is investing.com's panel (the live pipeline uses tradingeconomics' panel; they differ by ~0.1–0.5 mb, e.g. crude consensus −0.7 vs −0.6 for 23-09).

| Release | Crude | Gasoline | Distillate | Weights (g/d) | TLS |
|---|---|---|---|---|---|
| 29-07-2026 | −7.867 | −0.693 | +0.862 | 0.80/0.50 | −7.99 |
| 05-08-2026 | +3.979 | −0.343 | −3.673 | 0.80/0.50 | +1.87 |
| 12-08-2026 | +19.123 | +0.232 | +1.290 | 0.80/0.50 | +19.95 |
| 19-08-2026 | +4.205 | +1.888 | −0.630 | 0.80/0.50 | +5.40 |
| 26-08-2026 | −1.505 | −1.836 | −0.628 | 0.80/0.50 | −3.29 |
| 02-09-2026 | −4.050 | +0.727 | +2.096 | 0.80/0.50 | −2.42 |
| 10-09-2026 | +1.009 | +2.669 | +2.787 | 0.80/0.50 | +4.54 |
| 16-09-2026 | +0.960 | +1.794 | +1.485 | 0.80/0.50 | +3.14 |
| 23-09-2026 | +3.669 | −1.786 | +0.172 | 0.80/0.50 | +2.33 |

The 12-08 crude actual was **+17.423 mb** against a consensus of −1.7 (a genuine print, the following week's "previous" column repeats it).

Spread estimates on the 8 weeks before 23-09: plain std dev **8.33**, MAD-based **5.80**, median TLS +2.5, std dev with the 12-08 week removed **4.89**. On all 9 weeks: std 7.79, MAD 4.56. With a 1.25 gate that means a trade needs |TLS| of roughly 6 to 10 mb.

Market inputs on 2026-09-29 (yfinance): WTI $89.74, ATR_20 **$4.81**, OVX **53.74**, CL1−CL2 +$2.28, Brent−WTI +$6.73, 3:2:1 crack $61.46, DXY 101.4, USD/INR ≈ 95.9.

The 23-09-2026 report (a real replay):
- crude change +2.969 (consensus −0.6 → surprise +3.569), gasoline −1.686 (consensus +0.1 → −1.786), distillate −0.428 (consensus −0.6 → +0.172)
- Cushing change +2.266, Cushing level **23.748 mb** (multiplier 1.391), API crude +1.786
- TLS = 3.569 − 0.80·1.786 + 0.50·0.172 = **+2.226 mb**; σ (MAD, 8 prior weeks) = 5.80 (std = 8.33); **Z = +0.27–0.38 → STAND DOWN** under either estimator.
- For reference the model would have said: β_vol = 0.4807 · sqrt(53.74/30) = **0.643**; ΔP = −2.226 · 0.643 · 1.391 = **−$1.99/bbl** (≈ $0.89 per mb of TLS, three times the top of the 0.15–0.30 anchor).
- Reference week 10-09-2026 (TLS +4.54, gasoline +2.669, distillate +2.787 surprises) also stands down (Z ≈ 0.6–0.8).

## 4. What I want you to attack (answer each; numbered)

1. **σ_forecast.** Is 1.4826×MAD over the last 12 weekly TLS values defensible instead of a sample std dev? With this history (one +19.95 mb week, median +2.5), what estimator, window and minimum sample would you use, and what |TLS| gate does that imply? Is a rolling window even the right idea given EIA surprises are heavy-tailed? Is it valid to build σ from investing.com's consensus but compare live TLS built with tradingeconomics' consensus?
2. **TLS construction.** Are the weights (0.67/0.80 gasoline, 0.50/0.70 distillate) and the additive form justified, or does the literature/market data suggest other weights (or that products barely move crude)? Should σ be computed on TLS or per product? Should refinery utilisation, exports, imports or SPR enter?
3. **Sanity band vs β_vol.** Here β_vol·multiplier gives ≈ $0.9 per mb versus an anchor of $0.15–0.30 per mb. Which is wrong, and in which volatility regime? Give an empirically grounded per-mb price response for crude surprises at OVX ≈ 55 and at OVX ≈ 25, and say whether `ATR/10 · sqrt(OVX/30)` has any theoretical basis.
4. **Cushing rule.** Is routing to the fade whenever Cushing's weekly change has the opposite sign to the headline (however small, e.g. +0.1 mb) sensible? Should there be a materiality threshold? Are the level tiers (22 / 30 / 45 mb, 1.5–2.0× at low levels) supported by evidence? Cushing's *operational* minimum has changed over the years; what is the right lower bound now?
5. **Regime 3 boolean.** List real EIA/API weeks in the last few years where this exact boolean would have fired, and what happened to WTI in the following hour. What is its false-positive risk, and what is the cleanest way to encode the overnight-rally condition from data?
6. **Regime 2 as a trade.** Does a Cushing-contradiction fade have positive expectancy at all? What should the entry filter be? Is buying a deep-ITM option to fade a spike sensible compared with waiting for the IV crush to finish?
7. **Option choice and MCX mechanics.** I assumed: options on MCX crude expire around the 19th (please state the actual rule for MCX CRUDEOIL options and how weekends/holidays shift it); lot = 100 bbl; the option underlies the MCX futures (so Black-76, not Black-Scholes, for delta/IV). Confirm or correct each. Is 0.80–0.85 delta ITM realistically liquid on MCX crude options, what are typical bid/ask spreads and open interest on those strikes, and what does that do to a 1% risk stop?
8. **Sizing.** I convert a futures stop to an option stop with `option_stop = futures_stop × USD/INR × delta` and size lots to 1% risk. Where does that break (gamma, IV crush, spread widening, gap through the stop, margin)? Give a better rule for a deep-ITM option bought at the print.
9. **Timing and data.** My inputs arrive seconds after the print by scraping; entries are limit orders on a retest 2–5 minutes later. Is there any edge left by then, or has the move already happened? Quantify what fraction of the ΔP typically remains after 2 and after 5 minutes.
10. **Validation.** With about 9–10 weeks of history, what can and cannot be concluded? Design a validation plan that would falsify this model: sample size, metrics by regime, what result means stop. Is "1 lot for 3 Wednesdays" long enough to learn anything? (It cannot measure edge; what is it good for?)
11. **Risk rules.** 1% risk per event, 2.5% daily loss circuit-breaker, hard exit 2.5 h after the print, never overnight, 50% size-down in geopolitical stress, abort on INR/RBI events. What is missing (margin spikes, circuit limits, correlated exposure, event-day liquidity)? Which single rule would you add?
12. **Bugs and silent failures.** Read the formulas and code above and list logic errors, sign errors, off-by-one boundaries (inclusive/exclusive), unit errors (mb vs kb, USD vs INR) and any situation where the system would confidently produce a wrong trade.

## 5. Format of your answer

1. A ranked list of the top 10 risks (most expensive first), each with: the problem, a concrete example, and the fix.
2. Then answer questions 1–12 briefly, numbers first.
3. End with the three changes you would make before risking real money, and the one thing you would refuse to trade without.
