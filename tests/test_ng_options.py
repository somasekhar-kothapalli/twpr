from datetime import date

import pytest

from app import ng_options as ng
from app import signal_engine
from app.signal_engine import InputError

# Futures expiry (from the MCX launch calendar) and the option expiry two business days earlier.
EXPECTED = {1: ((27), (22)), 2: (24, 20), 3: (26, 24), 4: (27, 23), 5: (26, 22), 6: (25, 23),
            7: (28, 24), 8: (26, 24), 9: (25, 23), 10: (27, 23), 11: (24, 20), 12: (28, 23)}


@pytest.mark.parametrize("month", range(1, 13))
def test_2026_expiries_match_the_calendar(month):
    futures_day, option_day = EXPECTED[month]
    assert ng.futures_expiry(2026, month) == date(2026, month, futures_day)
    assert ng.option_expiry(2026, month) == date(2026, month, option_day)


def test_natural_gas_is_not_on_crudes_calendar():
    assert ng.option_expiry(2026, 10) == date(2026, 10, 23)      # crude's October options expire on the 15th


def test_a_month_outside_the_calendar_is_flagged_as_a_guess():
    assert not ng.expiry_is_known(2027, 1)
    assert ng.pick_expiry(date(2026, 12, 30))["expiry_source"] == "assumed_guess"


def test_pick_expiry_rolls_inside_five_days():
    assert ng.pick_expiry(date(2026, 10, 1)) == {"expiry_date": "23-10-2026", "days_to_expiry": 22, "rolled": False,
                                                 "expiry_source": "mcx_calendar"}
    rolled = ng.pick_expiry(date(2026, 10, 20))                  # 3 days before the 23rd
    assert rolled["rolled"] and rolled["expiry_date"] == "20-11-2026"


def test_contract_sizes_and_symbols():
    assert ng.CONTRACT_MMBTU == {"NATURALGAS": 1250, "NATURALGASM": 250}
    assert ng.MCX_SYMBOL["NATURALGASM"] == "NATGASMINI"


def test_delta_deepens_only_above_the_volatility_threshold():
    assert ng.target_delta(60.0) == (0.60, 0.70, False)
    assert ng.target_delta(60.1) == (0.80, 0.85, True)


def test_strikes_sit_on_the_rs_5_grid_and_near_the_target_delta():
    guide = ng.strike_guidance(326.0, 60.0, 20, (0.60, 0.70), "CALL")
    assert guide["atm_strike"] % 5 == 0 and guide["strike_at_delta_low"] % 5 == 0
    assert guide["strike_at_delta_low"] < guide["strike_at_delta_high"] < guide["atm_strike"] or \
        guide["strike_at_delta_high"] <= guide["strike_at_delta_low"] < guide["atm_strike"]
    assert abs(guide["delta_at_delta_low_strike"] - 0.60) <= 0.04
    put = ng.strike_guidance(326.0, 60.0, 20, (0.60, 0.70), "PUT")
    assert put["strike_at_delta_low"] > put["atm_strike"]        # an ITM put is above the futures price


def test_sizing_maths_for_a_mini_lot():
    out = ng.sizing({"NATURALGASM": 2}, 96.0, (0.60, 0.70), (0.05,))
    # 2 lots x $0.05 x 96 x delta 0.65 x 250 MMBtu = 1560
    assert out["contracts"]["NATURALGASM"]["risk_inr_by_futures_stop_usd"] == {"0.05": 1560}
    assert out["contracts"]["NATURALGASM"]["mmbtu_per_lot"] == 250


def test_natural_gas_contract_sizes_are_validated_and_point_to_the_lots_setting(monkeypatch):
    for name in ("MCX_CRUDEOIL_LOT_SIZE", "MCX_CRUDEOILM_LOT_SIZE", "MCX_NATURALGAS_LOT_SIZE", "MCX_NATURALGASM_LOT_SIZE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCX_NATURALGASM_LOT_SIZE", "250")
    assert signal_engine.load_lot_sizes()["NATURALGASM"] == 250
    monkeypatch.setenv("MCX_NATURALGAS_LOT_SIZE", "3")            # a lot COUNT typed in the size setting
    with pytest.raises(InputError, match="MCX_NATURALGAS_LOTS"):
        signal_engine.load_lot_sizes()


def test_natural_gas_lot_counts_are_separate_from_crude(monkeypatch):
    for name in ("MCX_NATURALGAS_LOTS", "MCX_NATURALGASM_LOTS", "MCX_CRUDEOIL_LOTS", "MCX_CRUDEOILM_LOTS"):
        monkeypatch.delenv(name, raising=False)
    assert signal_engine.load_ng_lot_counts() == {"NATURALGAS": None, "NATURALGASM": None}
    monkeypatch.setenv("MCX_NATURALGASM_LOTS", "4")
    assert signal_engine.load_ng_lot_counts() == {"NATURALGAS": None, "NATURALGASM": 4}
    assert signal_engine.load_lot_counts() == {"CRUDEOIL": None, "CRUDEOILM": None}   # crude sizing never sees it
    monkeypatch.setenv("MCX_NATURALGASM_LOTS", "0")
    with pytest.raises(InputError, match="MCX_NATURALGASM_LOTS"):
        signal_engine.load_ng_lot_counts()
