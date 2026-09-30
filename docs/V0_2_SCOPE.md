# TWPR v0.2: scope

Written 2026-09-30. v0.2 is about natural gas, and for now it is **record only**.

## What v0.2 is

A **passive recorder** for the EIA natural gas storage report (Thursdays 10:30 ET). Each Thursday it writes down the print, both consensus panels, EIA's storage table (net change against implied flow, stocks against the 5-year average) and NG=F's price path after the print. It produces no signal, sizes nothing, sends no alert on success and trades nothing.

Why not a model yet: an 8-print check found no support for the assumed price response (`docs/NATURAL_GAS_MCX_FACTS.md`, sections 5 and 6), and the history that could test it does not exist anywhere free. The only way to get a sample is to record forward.

## In scope (built and tested)

- `app/ng_recorder.py`, `app/utils/ng_storage.py`, the `ng_storage` slugs in `sources.py`, `Bcf`/`B` suffix parsing, `NG_RECORD_FILE`.
- `.github/workflows/twpr_ng_record.yml` (Thursday 17:00 UTC).
- Groundwork, pure and **not called by anything**: `app/ng_options.py` (2026 futures/option expiry calendar, 1,250 / 250 MMBtu contract sizes, Rs 5 strike guide, delta rule, sizing maths) and validated `MCX_NATURALGAS_LOT_SIZE` / `MCX_NATURALGASM_LOT_SIZE` / `MCX_NATURALGAS_LOTS` / `MCX_NATURALGASM_LOTS` settings (`signal_engine.load_ng_lot_counts`, kept apart from the crude sizing).
- MCX natural gas contract facts and the review of the blueprint (`docs/NATURAL_GAS_MCX_FACTS.md`).
- The 8 prints from 6 Aug to 24 Sep 2026, backfilled.

## Out of scope for now

The natural gas model (Bcf Z-score, 5-year multiplier, regimes), a signal or message, wiring `ng_options` into the engine, weather or LNG inputs, any trade. The natural gas settings are validated but nothing reads them.

## Decision rule

Look at `python -m app.ng_recorder --show` after about 20-26 recorded weeks (around March 2027). Build the model only if the measured move per Bcf of surprise, in the expected direction, clearly beats round-trip costs at MCX spreads. Those costs are not measured yet: note the pre- and post-print spread on your chain by hand in the meantime. Otherwise natural gas stays parked.

## Watch-outs

- The workflow runs only from `main`; nothing is recorded on Thursdays until this branch is merged.
- Yahoo keeps 5-minute bars about 60 days and 1-minute bars a week, so a missed Thursday cannot be recovered later.
- Consensus panels differ (24-09: 53 on tradingeconomics, 50 on investing.com), so the surprise depends on the source. Both are recorded.
- Crude v0.1 validation (one live Wednesday, three forward Wednesdays at minimum size) remains the priority.
