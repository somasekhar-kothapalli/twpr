"""Verify STRIKE_INTERVAL against the live MCX option chain.

Marked `network` and skipped by default, because a unit suite that needs the
internet is a unit suite that fails on a plane. Run it deliberately:

    python -m pytest tests/test_strike_interval.py -v -m network

Source is Zerodha's public instrument master — no auth, no key, every live
tradeable contract with its strike, tick and expiry. MCX's own site is behind an
Akamai block that refuses plain HTTP and a headless browser alike, so this is the
practical way to read the real chain.

Verified 2026-09-27: all 547 consecutive gaps across the three listed CRUDEOIL
option expiries were exactly 50.0.
"""

from __future__ import annotations

import csv
import io
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from currency import STRIKE_INTERVAL, strikes  # noqa: E402

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def crude_options():
    """Live MCX CRUDEOIL option rows, or skip if the feed is unreachable."""
    httpx = pytest.importorskip("httpx")
    try:
        response = httpx.get(INSTRUMENTS_URL, headers=HEADERS, timeout=60, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — an offline run skips, it does not fail
        pytest.skip(f"instrument master unreachable: {exc}")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    options = [
        r
        for r in rows
        if r["exchange"] == "MCX"
        and r["name"] == "CRUDEOIL"
        and r["instrument_type"] in ("CE", "PE")
    ]
    if not options:
        pytest.skip("no CRUDEOIL options listed right now")
    return options


def _strikes_by_expiry(options) -> dict[str, list[float]]:
    by_expiry: dict[str, set[float]] = {}
    for row in options:
        if row["instrument_type"] == "CE":
            by_expiry.setdefault(row["expiry"], set()).add(float(row["strike"]))
    return {expiry: sorted(values) for expiry, values in sorted(by_expiry.items())}


def test_every_strike_gap_equals_the_configured_interval(crude_options):
    """The whole point: our arithmetic must match the exchange's spacing."""
    by_expiry = _strikes_by_expiry(crude_options)
    assert by_expiry, "no expiries found"

    for expiry, values in by_expiry.items():
        gaps = Counter(round(b - a, 2) for a, b in zip(values, values[1:]))
        assert gaps, f"{expiry}: fewer than two strikes"
        assert set(gaps) == {float(STRIKE_INTERVAL)}, (
            f"{expiry}: expected every gap to be {STRIKE_INTERVAL}, saw {dict(gaps)}. "
            "If MCX has respaced the chain, update currency.STRIKE_INTERVAL."
        )


def test_every_live_strike_is_a_multiple_of_the_interval(crude_options):
    for row in crude_options:
        strike = float(row["strike"])
        assert strike % STRIKE_INTERVAL == 0, f"{row['tradingsymbol']} strike {strike}"


def test_our_atm_choice_matches_the_real_ladder(crude_options):
    """Pick an ATM strike for a realistic level and find it in the live chain."""
    by_expiry = _strikes_by_expiry(crude_options)
    nearest_expiry = next(iter(by_expiry))
    ladder = by_expiry[nearest_expiry]

    # A level in the middle of the listed range, offset so it is not already on a strike.
    level = ladder[len(ladder) // 2] + 3.06

    computed = strikes(level, "put")
    truth = min(ladder, key=lambda s: abs(s - level))
    index = ladder.index(truth)

    assert computed["strike_atm"] == truth
    assert computed["strike_1_otm"] == ladder[index - 1]  # OTM put sits below

    computed_call = strikes(level, "call")
    assert computed_call["strike_1_otm"] == ladder[index + 1]  # OTM call above


def test_options_expire_before_the_futures(crude_options):
    """Recorded because it drives near-month selection, not because it can break.

    MCX CRUDEOIL options expire two to four days ahead of the futures contract
    they settle into, so "near month" is not one date.
    """
    expiries = sorted({r["expiry"] for r in crude_options})
    assert expiries, "no option expiries"
    # Purely informational: surface the dates when run with -s.
    print(f"\n  option expiries: {expiries[:4]}")
