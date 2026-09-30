# Gemini prompt: natural gas questions (paste everything below the line)

---

You are helping a solo retail trader in India decide whether a natural gas setup around the weekly EIA storage report is worth building. Answer only what you can support. **For every number, give a source (paper, agency, exchange document, dataset) or write "no source, my estimate" with a range. If you do not know, say "I don't know". Do not invent studies or figures.** Short answers, no preamble.

## What exists (do not review or rebuild it)

- **Trader:** buys options only, on MCX natural gas (`NATURALGAS` 1,250 MMBtu and `NATGASMINI` 250 MMBtu; European options on the MCX futures, Rs 5 strike interval, 81 strikes, quoted in Rs per MMBtu, expire two business days before the futures). Places every order **by hand**, no broker connection, no execution code. Evening session, so the EIA report (Thursday 10:30 ET) arrives at 20:00 IST in summer, 21:00 IST in winter.
- **Software:** a passive recorder that, each Thursday, writes down: the actual storage change, both public consensus figures (tradingeconomics and investing.com, they differ), EIA's storage table (stocks, net change against implied flow, the 5-year average and the percentage against it) and the NYMEX `NG=F` price at 0/1/2/5/10/15/30/60 minutes after the print. It scrapes public pages; there is no paid or low-latency feed. It makes no signal and no trade. There is no natural gas model. Nothing here executes automatically.
- **Scope decision already made:** record only for about 20-26 weeks, then decide whether to build a model at all.

## Data so far (8 prints, summer injection season, `NG=F` 5-minute bars, investing.com consensus)

| Release | Actual (Bcf) | Consensus | Surprise | Move at 5 min | 30 min | 60 min |
|---|---|---|---|---|---|---|
| 06-08-2026 | +33 | 30 | +3 | -0.016 | -0.024 | -0.002 |
| 13-08-2026 | +36 | 31 | +5 | -0.017 | -0.004 | +0.001 |
| 20-08-2026 | +16 | 15 | +1 | -0.008 | -0.026 | -0.024 |
| 27-08-2026 | +15 | 19 | -4 | -0.037 | -0.044 | -0.051 |
| 03-09-2026 | +30 | 30 | 0 | -0.003 | -0.067 | -0.095 |
| 10-09-2026 | +40 | 35 | +5 | +0.003 | +0.008 | +0.020 |
| 17-09-2026 | +44 | 49 | -5 | -0.017 | -0.018 | -0.022 |
| 24-09-2026 | +53 | 50 | +3 | -0.006 | +0.090 | +0.147 |

(Moves in USD per MMBtu from the price just before 10:30 ET. A negative surprise means a smaller build than expected, which should be bullish.) Storage on 18 Sep 2026: 3,351 Bcf, 2.9% above the 5-year average. The fitted 5-minute slope was about -0.0009 USD per Bcf (wrong sign), the direction was right in 4 of the 7 non-zero prints. Eight points prove nothing; they only mean the assumed response is not confirmed. Consensus on tradingeconomics for 24-09 was 53, not 50, so the surprise depends on the panel.

## Answers already given that I do not trust (do not repeat them)

You previously gave two different anchors for the price response (0.003-0.005 and 0.007-0.015 USD per Bcf) with no source I can verify. Do not give a per-Bcf anchor again unless you can cite a specific study and its sample.

## Questions

1. **MCX option spreads.** What are typical bid/ask spreads (in Rs per MMBtu) on MCX natural gas and natural gas mini options in the **evening** session, for an ATM strike and for a strike about 0.80 delta ITM? How do they change in the two minutes after 20:00 IST on a storage day? Cite MCX data, a broker or a market-microstructure source; otherwise say you do not know.
2. **Retracement.** After an EIA natural gas storage release, how much of the first-5-minute NYMEX move is typically retraced by 15 and 60 minutes? Cite a study or dataset.
3. **Weather data.** Which **free, machine-readable** sources provide the 6-10 and 8-14 day HDD/CDD or temperature outlook and its change since the previous day? Give the exact URLs and file formats (e.g. NOAA CPC products), how often they update, and what needs to be parsed. Also: is there any free proxy for LNG feedgas flows?
4. **Consensus history and typical misses.** What is the typical absolute miss of the EIA storage consensus by month (injection and withdrawal seasons)? Is there a **public, free** archive of consensus estimates longer than the last 10 weeks (Reuters or Bloomberg polls are paid; tell me if nothing free exists)?
5. **Context variable.** What is a defensible, sourced way to project end-of-season storage (31 Oct, 31 Mar) from the current level, and what glut/deficit thresholds do practitioners or agencies use? Do not give thresholds without a source.
6. **Report dates.** State the exact rule for when EIA's storage release moves off Thursday (holidays), with a link to EIA's schedule.
7. **The honest question.** Given only what is above, what is the strongest reason a retail options buyer trading this by hand through MCX has no edge after costs, and what single measurement would most quickly confirm or refute that? Then say what result would justify building a model.

## Format

Number the answers 1-7. For each: the answer, then `Source:` or `No source, my estimate:` or `I don't know`. End with a list of any claim you are less than 80% sure of.
