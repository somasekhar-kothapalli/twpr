"""MCX natural gas option mechanics (groundwork for v0.2). Pure: no I/O, no clock. NOTHING CALLS THIS YET:
natural gas is record-only (docs/V0_2_SCOPE.md), so there is no signal, message or sizing line built on it. It exists
so the contract facts from docs/NATURAL_GAS_MCX_FACTS.md are in tested code for the day the recorder's sample says the
setup is worth building.

Facts (MCX options specifications, March 2026, Circular MCX/TRD/103/2026; futures January 2026, MCX/TRD/320/2025):
NATURALGAS 1,250 MMBtu and the mini (symbol NATGASMINI, 250 MMBtu) options are European, quoted in Rs per MMBtu,
40 ITM / 1 near / 40 OTM strikes (81 calls and puts) at a Rs 5 interval, tick Rs 0.05; the last trading day is two
business days before the underlying futures' expiry; positions devolve into futures at expiry (never hold). It reuses
the crude module's business-day, holiday and Black-76 helpers because the rules are the same.
"""
from datetime import date, timedelta

from app import options

# NATURALGAS futures expiry dates for 2026 (MCX launch calendar); the mini contract uses the same dates. They are NOT
# crude's dates and no shared calendar applies. 2027 is not loaded: a month missing from it falls back to a guess.
FUTURES_EXPIRY_CALENDAR = {
    (2026, 1): date(2026, 1, 27), (2026, 2): date(2026, 2, 24), (2026, 3): date(2026, 3, 26),
    (2026, 4): date(2026, 4, 27), (2026, 5): date(2026, 5, 26), (2026, 6): date(2026, 6, 25),
    (2026, 7): date(2026, 7, 28), (2026, 8): date(2026, 8, 26), (2026, 9): date(2026, 9, 25),
    (2026, 10): date(2026, 10, 27), (2026, 11): date(2026, 11, 24), (2026, 12): date(2026, 12, 28),
}
FALLBACK_EXPIRY_DAY = 25       # a month missing from the calendar: a guess (2026 dates run from the 24th to the 28th)
# MMBtu per lot. The keys follow the .env setting names (MCX_NATURALGAS_*, MCX_NATURALGASM_*); MCX's own symbol for
# the mini is NATGASMINI.
CONTRACT_MMBTU = {"NATURALGAS": 1250, "NATURALGASM": 250}
MCX_SYMBOL = {"NATURALGAS": "NATURALGAS", "NATURALGASM": "NATGASMINI"}
STRIKE_INTERVAL = 5            # Rs per MMBtu between strikes
STRIKE_TICK = 0.05             # options tick, Rs
DELTA_DEFAULT = options.DELTA_DEFAULT
DELTA_HIGH_VOL = options.DELTA_HIGH_OVX
VOL_DEEPEN_ABOVE = 60.0        # UNPROVEN (from an unsourced blueprint): above this volatility % deepen to 0.80-0.85.
                               # There is no natural gas OVX: use NG=F's realised volatility as a labelled proxy.


def target_delta(vol_pct):
    """(low, high, deepened): ITM delta 0.60-0.70, or 0.80-0.85 when volatility is above 60% (unproven)."""
    if vol_pct > VOL_DEEPEN_ABOVE:
        return (*DELTA_HIGH_VOL, True)
    return (*DELTA_DEFAULT, False)


def futures_expiry(year, month):
    """The month's futures expiry from the calendar; otherwise a guess (the 25th, or the business day before)."""
    known = FUTURES_EXPIRY_CALENDAR.get((year, month))
    return known or options._business_days_before(date(year, month, FALLBACK_EXPIRY_DAY), 0)


def expiry_is_known(year, month):
    return (year, month) in FUTURES_EXPIRY_CALENDAR


def option_expiry(year, month):
    """Two business days before the futures expiry (October 2026: futures Tue 27th, options Fri 23rd)."""
    return options._business_days_before(futures_expiry(year, month), options.OPTION_LEAD_BUSINESS_DAYS)


def _expiry_on_or_after(day):
    this_month = option_expiry(day.year, day.month)
    if day <= this_month:
        return this_month
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return option_expiry(year, month)


def pick_expiry(release_day):
    """The expiry to trade: the nearest, rolled to next month when 5 or fewer days remain (as for crude)."""
    expiry = _expiry_on_or_after(release_day)
    rolled = (expiry - release_day).days <= options.ROLL_WITHIN_DAYS
    if rolled:
        expiry = _expiry_on_or_after(expiry + timedelta(days=1))
    return {"expiry_date": expiry.strftime("%d-%m-%Y"), "days_to_expiry": (expiry - release_day).days,
            "rolled": rolled,
            "expiry_source": "mcx_calendar" if expiry_is_known(expiry.year, expiry.month) else "assumed_guess"}


def round_strike(strike):
    return round(round(strike / STRIKE_INTERVAL) * STRIKE_INTERVAL, 2)


def strike_guidance(futures_inr, vol_pct, days, delta_range, option_type):
    """Where the target-delta strikes should sit (no option chain): Black-76 with `vol_pct` standing in for MCX
    implied volatility, rounded to the Rs 5 interval. An ESTIMATE: confirm on the live chain. The futures level is
    NYMEX NG x USD/INR."""
    iv = vol_pct / 100
    guide = {"futures_level_inr": round(futures_inr, 2), "atm_strike": round_strike(futures_inr),
             "iv_used_pct": vol_pct, "days_to_expiry": days}
    for label, delta in zip(("delta_low", "delta_high"), delta_range):
        strike = round_strike(options.strike_for_delta(futures_inr, iv, days, delta, option_type))
        guide[f"strike_at_{label}"] = strike
        guide[f"delta_at_{label}_strike"] = round(abs(options.black76_delta(futures_inr, strike, iv, days, option_type)), 2)
    return guide


def sizing(lots_by_contract, usd_inr, delta_range, stops_usd):
    """INR lost if the option's stop is hit, per contract and per futures stop (USD per MMBtu):

        loss = lots * stop * USD/INR * mid delta * MMBtu per lot

    The stops are yours to choose: there is no measured stop bracket for natural gas."""
    delta = round(sum(delta_range) / 2, 3)
    return {"delta_used": delta,
            "contracts": {name: {"lots": lots, "mmbtu_per_lot": CONTRACT_MMBTU[name],
                                 "risk_inr_by_futures_stop_usd": {
                                     f"{stop:.2f}": int(round(lots * stop * usd_inr * delta * CONTRACT_MMBTU[name]))
                                     for stop in stops_usd}}
                          for name, lots in lots_by_contract.items()}}
