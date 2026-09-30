# Market factors: what moves crude, and what our system sees

The seven factors on the "Factors Influencing The Market" slide, plus the extra ones MCX lists in its own Crude Oil leaflet, mapped to what this pipeline actually does about each. The weekly model is built around exactly one of them (US inventories). The rest are either proxied, left to the trader's checklist, or not covered. This file says which, so nobody assumes a factor is watched when it is not.

Nothing here is backtested. "Covered" means the code reads the input, not that the input has been shown to predict anything.

## The map

| # | Factor | Why it matters to crude | What the system does today | Status |
|---|---|---|---|---|
| 1 | **OPEC output or supply** | OPEC+ quota decisions and voluntary cuts move price for weeks; MCX's own price chart is annotated with them. | Nothing automated. OVX, which the model does read, prices some of the uncertainty. | Not covered. The runbook (§7) lists the monthly OPEC MOMR as a cross-check; no script reads it. |
| 2 | **Oil demand from emerging and developing countries** | Slow-moving, sets the medium-term trend (India's consumption grew 3.8% a year in 2014-2024 per the leaflet). | Nothing. A weekly surprise model cannot see a trend that moves over quarters. | Not covered, and not meant to be. The IEA monthly report (§7) is the reference. |
| 3 | **US crude and product inventories** | The one factor released on a fixed weekly schedule with a published consensus. The surprise against consensus is the tradable event. | **The core of the model.** Consensus for crude/gasoline/distillate, the EIA print, TLS and its Z-score against the last 12 weeks, the Cushing check (change and level), and the Tuesday API crude change for Regime 3. | Covered. `consensus_fetcher`, `eia_actuals`, `api_monitor`, `model.py`, `signal_engine`. |
| 4 | **Refinery utilisation rate** | Refiners' crude intake sets demand for the barrels the inventory data measures; a drop of a couple of points is a demand signal. | `eia_actuals` fetches the week-over-week change (`refinery_util_change_pct`, investing.com only, optional). It is shown to the narrative model and stored in the signal inputs. **No rule uses it.** The utilisation *level* is not available without a paid or keyed EIA feed. | Read, not used. A candidate for a rule (for example a large drop downgrading a bullish draw), but nobody has decided one. |
| 5 | **Global geopolitics** | Supply-threat premia show up as high implied volatility and gap risk. | Indirectly: **OVX above 35 switches to the deeper 0.80-0.85 delta option**. Directly, only a manual checklist line ("geopolitical tension elevated? size down 50%"). No news feed. | Proxied and manual. |
| 6 | **Speculative buying and selling** | Crowded positioning makes bullish prints sell off and bearish ones squeeze (runbook §7, CFTC Commitments of Traders, Fridays). | Nothing reads COT. The scorecard's CL1-CL2 spread and the price-action checklist are the nearest stand-ins. | Not covered. |
| 7 | **Weather conditions** | Gulf hurricanes shut refineries and ports; winter cold moves heating demand. | The seasonal weights on gasoline (summer) and distillate (winter) in TLS are the only weather-shaped input. No hurricane or forecast feed. | Only the seasonal proxy. |

## Extra factors in MCX's own leaflet

| Factor | What the system does |
|---|---|
| **US dollar / currency movements** | USD/INR is read live for the rupee conversions. DXY is fetched into the informational scorecard. An INR-specific shock (RBI action) is a manual **abort** item on the checklist; nothing detects it. |
| **Economic factors** (growth, recession, inflation) | Not covered. |
| **Government trade policies** (duties, quotas) | Not covered. |
| **Prices in international markets** | The whole model is built on NYMEX WTI moves; MCX prices follow it in rupees. `market_data` reads WTI, Brent-WTI and the CL1-CL2 spread. |

## Where each factor shows up in the alert

- Inventories: TLS, Z, the three surprises, the Cushing line, the API line.
- Refinery utilisation: stored in `signal.json` inputs, not in the message.
- Geopolitics: the delta line ("OVX 53.7 above 35, deep ITM") and a checklist item.
- Currency: the USD/INR figure next to every rupee number, and the abort checklist item.
- Everything else: nothing.

## What follows from this

- The system trades **one** weekly information event. It cannot tell a week where the inventory surprise is the whole story from one where OPEC news or a geopolitical headline is doing the moving. The trader's judgement is the only filter for factors 1, 2, 6 and 7.
- The two factors most worth the effort if the model is extended: **refinery utilisation** (data already fetched, only a rule is missing) and **positioning (COT)** (weekly, free, and the runbook already argues for it).
- Do not read the OVX rule as "geopolitics is handled". It changes which option is bought; it does not decide whether to trade.

Sources: the "Factors Influencing The Market" slide (user-supplied), MCX Crude Oil leaflet and hedging brochure (2024), `docs/WPSR_WEDNESDAY_RUNBOOK.md` §7 and its implementation-status table.
