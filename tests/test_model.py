import pytest

from app import model


@pytest.mark.parametrize("level,expected", [
    (None, 1.0),
    (5.0, 2.0), (10.0, 2.0),          # full ceiling at <= 10 mb
    (16.0, 1.75),                     # 1.5 + 0.5 * 6/12
    (21.999, 1.5), (22.0, 1.5),       # the two branches meet at 1.5x
    (26.0, 1.25), (29.999, 1.0), (30.0, 1.0),
    (45.0, 1.0), (60.0, 1.0),         # normal range and the neutral >45 default
])
def test_cushing_multiplier(level, expected):
    assert model.cushing_multiplier(level) == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize("tls,cushing,expected", [
    (2.0, -1.0, True), (-2.0, 1.0, True),      # build with a Cushing draw / draw with a Cushing build (>= 1.0 mb)
    (2.0, -3.0, True), (-2.0, 2.5, True),
    (2.0, 1.5, False), (-2.0, -1.5, False),    # same direction: confirms
    (2.0, -0.999, False), (-2.0, 0.5, False),  # opposite but under 1.0 mb: noise, not a contradiction
    (8.0, -0.1, False),                        # a -0.1 mb Cushing draw cannot veto a big build
    (2.0, 0.0, False), (-2.0, 0.0, False),     # a flat Cushing contradicts nothing
    (2.0, None, None),
])
def test_cushing_contradicts(tls, cushing, expected):
    assert model.cushing_contradicts(tls, cushing) is expected


@pytest.mark.parametrize("tls,cushing,expected", [
    (2.0, -1.0, "contradicts"), (2.0, 1.0, "confirms"), (2.0, 0.999, "immaterial"), (2.0, -0.4, "immaterial"),
    (-2.0, 0.0, "immaterial"), (2.0, None, "unknown"),
])
def test_cushing_status_names_what_the_signal_will_say(tls, cushing, expected):
    assert model.cushing_status(tls, cushing) == expected


def test_anchor_range_is_signed_like_the_expected_move():
    assert model.anchor_move_usd(2.0) == (-0.30, -0.60)          # a build: down
    assert model.anchor_move_usd(-4.0) == (0.60, 1.20)           # a draw: up


def test_beta_vol_and_expected_move():
    assert model.beta_vol(3.0, 30.0) == pytest.approx(0.3)
    assert model.beta_vol(3.0, 120.0) == pytest.approx(0.6)            # sqrt(4) = 2
    assert model.expected_move_usd(2.0, 0.3, 1.5) == pytest.approx(-0.9)   # a build is a negative move
    assert model.expected_move_usd(-2.0, 0.3, 1.0) == pytest.approx(0.6)


def test_sanity_anchor_is_015_to_030_usd_per_mb():
    assert model.sanity_ok(2.0, -0.4) and model.sanity_ok(2.0, 0.6) and model.sanity_ok(-2.0, 0.3)
    assert not model.sanity_ok(2.0, -0.2) and not model.sanity_ok(2.0, -0.7)


def classify(tls, z, contradicts=False, change=1.0, consensus=0.0, api=1.0, rally=None):
    return model.classify(tls, z, contradicts, change, consensus, api, rally)


def test_gate_is_inclusive_at_125_sigma():
    assert classify(2.0, 1.2499) == (None, "neutral")
    assert classify(2.0, 1.25) == (1, "bearish")
    assert classify(-2.0, -1.25) == (1, "bullish") and classify(-2.0, -1.2499) == (None, "neutral")


def test_contradiction_beats_everything_else_and_fades_the_headline():
    assert classify(2.0, 2.0, contradicts=True) == (2, "bullish")
    assert classify(-2.0, -2.0, contradicts=True) == (2, "bearish")
    # even a textbook Regime 3 setup is Regime 2 when Cushing contradicts
    assert classify(-3.0, -2.0, contradicts=True, change=-1.0, consensus=-0.5, api=-5.0) == (2, "bearish")
    assert classify(2.0, 0.5, contradicts=True) == (None, "neutral")     # ...but never past the gate


def test_regime_3_needs_every_leg_of_the_api_story_and_a_measured_rally():
    base = dict(tls=-3.0, z=-2.0, change=-1.0, consensus=-0.5, api=-5.0, rally=1.5)
    assert classify(**base) == (3, "bearish")
    assert classify(**{**base, "api": -3.4}) == (1, "bullish")           # API surprise -2.9: not extreme (> 3.0)
    assert classify(**{**base, "api": -3.5}) == (1, "bullish")           # exactly -3.0: not beyond it
    assert classify(**{**base, "api": -3.6}) == (3, "bearish")
    assert classify(**{**base, "change": -0.4}) == (1, "bullish")        # EIA did not beat consensus
    assert classify(**{**base, "change": -6.0}) == (1, "bullish")        # EIA draw bigger than the API whisper
    assert classify(tls=3.0, z=2.0, change=1.0, consensus=0.0, api=-5.0, rally=1.5) == (1, "bearish")   # a build is never sell-the-fact


def test_regime_3_needs_the_overnight_rally_to_be_known_and_above_one_dollar():
    base = dict(tls=-3.0, z=-2.0, change=-1.0, consensus=-0.5, api=-5.0)
    assert model.regime3_setup(-3.0, -1.0, -0.5, -5.0) is True
    assert classify(**base) == (1, "bullish")                            # no rally data: not fired
    assert classify(**base, rally=None) == (1, "bullish")
    assert classify(**base, rally=0.99) == (1, "bullish")
    assert classify(**base, rally=1.00) == (1, "bullish")                # must exceed, not equal
    assert classify(**base, rally=1.01) == (3, "bearish")
    assert classify(**base, rally=-2.0) == (1, "bullish")                # a selloff is not the rally story
    assert classify(**base, rally=1.01, contradicts=True) == (2, "bearish")   # Cushing still wins
