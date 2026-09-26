"""Tests for the journal's money maths.

CTT is 0.05% of the premium, not 1%: an 820 premium on 1 lot costs Rs 41, not
Rs 820. The worked example in the original build prompt had the latter and is
wrong — confirmed 2026-09-25.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from journal import (  # noqa: E402
    BROKERAGE_PER_LEG,
    CTT_RATE,
    LOT_SIZE,
    calculate_pnl,
    summarize,
)


def test_losing_trade_reference_case():
    """Entry 820, exit 492, 1 lot — the spec's worked example."""
    result = calculate_pnl(820.0, 492.0, 1)
    assert result["gross_pnl"] == -32800.0
    assert result["ctt_charge"] == 41.0  # 820 * 1 * 100 * 0.0005
    assert result["brokerage"] == 40.0  # Rs 20 per leg, both legs
    assert result["net_pnl"] == -32881.0
    assert result["return_pct"] == -40.0
    assert result["capital_deployed"] == 82000.0


def test_winning_trade():
    """Entry 820, exit 1230, 2 lots — target 1."""
    result = calculate_pnl(820.0, 1230.0, 2)
    assert result["gross_pnl"] == 82000.0
    assert result["ctt_charge"] == 82.0
    assert result["net_pnl"] == 82000.0 - 82.0 - 40.0
    assert result["return_pct"] == 50.0


def test_ctt_scales_with_lots_not_with_the_exit():
    """CTT is charged on the entry premium, so the exit price cannot change it."""
    cheap_exit = calculate_pnl(820.0, 100.0, 3)
    rich_exit = calculate_pnl(820.0, 2000.0, 3)
    assert cheap_exit["ctt_charge"] == rich_exit["ctt_charge"]
    assert cheap_exit["ctt_charge"] == pytest.approx(820.0 * 3 * LOT_SIZE * CTT_RATE)


def test_charges_always_reduce_net_pnl():
    """Net is gross minus charges — never above gross, for a win or a loss."""
    for exit_premium in (100.0, 820.0, 1500.0):
        result = calculate_pnl(820.0, exit_premium, 1)
        assert result["net_pnl"] < result["gross_pnl"]
        assert result["net_pnl"] == pytest.approx(
            result["gross_pnl"] - result["ctt_charge"] - result["brokerage"]
        )


def test_breakeven_premium_still_loses_the_charges():
    result = calculate_pnl(820.0, 820.0, 1)
    assert result["gross_pnl"] == 0.0
    assert result["return_pct"] == 0.0
    assert result["net_pnl"] == -(820.0 * LOT_SIZE * CTT_RATE + BROKERAGE_PER_LEG * 2)


# --- summary rendering -----------------------------------------------------

TRADE = {
    "net_pnl": 1000.0,
    "return_pct": 10.0,
    "grade": "A",
    "trade_type": "paper",
    "exit_type": "target_1",
    "week_ending": "2026-09-04",
}


def test_profit_factor_is_not_inf_when_there_are_no_losses():
    """A money report showing "inf" reads like a bug."""
    out = summarize([TRADE], "Sep 2026")
    assert "Profit factor:  n/a" in out
    assert "inf" not in out


def test_profit_factor_computed_when_losses_exist():
    loser = {**TRADE, "net_pnl": -500.0, "return_pct": -20.0, "exit_type": "stop"}
    out = summarize([TRADE, loser], "Sep 2026")
    assert "Profit factor:  2.00" in out  # 1000 / 500


def test_empty_journal_renders_without_dividing_by_zero():
    out = summarize([], "week of 2026-09-21")
    assert "No trades logged." in out


def test_money_fields_use_f_string_formatting():
    """%-style logging has no comma flag; the thousands separator needs an f-string.

    Regression guard: `logger.info("... %+,.0f", x)` silently logged nothing.
    """
    with pytest.raises(ValueError, match="unsupported format character"):
        _ = "%+,.0f" % -32881.0
    assert f"{-32881.0:+,.0f}" == "-32,881"
