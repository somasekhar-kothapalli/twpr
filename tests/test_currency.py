import pytest

from app import currency


def test_trend_is_the_percent_change_oldest_to_newest_and_skips_gaps():
    assert currency.trend_pct([95.0, None, 95.5, 96.0]) == pytest.approx(1.053, abs=1e-3)
    assert currency.trend_pct([95.0]) is None and currency.trend_pct([]) is None and currency.trend_pct([0.0, 1.0]) is None


@pytest.mark.parametrize("trend,expected", [
    (None, "unknown"), (0.0, "flat"), (0.099, "flat"), (-0.099, "flat"),
    (0.1, "inr_weakening"), (0.62, "inr_weakening"), (-0.1, "inr_strengthening"), (-1.2, "inr_strengthening"),
])
def test_classify(trend, expected):
    assert currency.classify(trend) == expected


@pytest.mark.parametrize("direction,rupee,effect", [
    ("bearish", "inr_weakening", "dampens"),        # a weaker rupee lifts the rupee price of crude
    ("bearish", "inr_strengthening", "amplifies"),
    ("bullish", "inr_weakening", "amplifies"),
    ("bullish", "inr_strengthening", "dampens"),
    ("bearish", "flat", "neutral"), ("bullish", "unknown", "neutral"), ("neutral", "inr_weakening", "neutral"),
])
def test_effect_on_the_trade_is_spelled_out_per_case(direction, rupee, effect):
    assert currency.effect_on(direction, rupee) == effect


def test_a_sharp_move_against_the_trade_earns_two_notes_including_why_it_cannot_reverse():
    notes = currency.risk_notes(0.9, "bearish")
    assert notes[0].startswith("INR sharply weakened 0.90% over 5 sessions, working against a bearish MCX move")
    assert "shut 17:00-09:00 IST" in notes[1]


def test_a_mild_move_against_the_trade_is_one_note_without_the_sharpness():
    notes = currency.risk_notes(0.3, "bearish")
    assert len(notes) == 1 and "sharply" not in notes[0]


def test_a_move_in_the_trades_favour_is_only_noted_when_it_is_sharp():
    assert currency.risk_notes(-0.3, "bearish") == []
    assert "amplifying a bearish MCX move" in currency.risk_notes(-0.8, "bearish")[0]


def test_missing_history_is_stated_not_hidden():
    assert "unknown" in currency.risk_notes(None, "bearish")[0]
    assert currency.context(None, "bearish")["effect"] == "neutral"


def test_context_block_shape():
    block = currency.context(0.62, "bullish")
    assert (block["usd_inr_trend_pct"], block["direction"], block["effect"]) == (0.62, "inr_weakening", "amplifies")
    assert len(block["notes"]) == 2 and "amplifying a bullish MCX move" in block["notes"][0]   # sharp: two notes
    assert currency.context(0.3, "bullish")["notes"] == []                                     # mild and helpful: silent
