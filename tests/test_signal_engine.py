import datetime
import json
import logging
import sys
import types

import httpx

import pytest

from app import model
from app import signal_engine as se
from app.signal_engine import (InputError, build_signal, fetch_usd_inr, generate_analysis, load_inputs,
                               load_lot_counts, load_lot_sizes, load_market, load_sigma)

# The 23-09-2026 report: crude +3.569 vs consensus, Cushing +2.266 at a 23.748 mb level, OVX 53.7.
INPUTS = {
    "release_date": "23-09-2026",
    "crude_consensus_mb": -0.6, "gasoline_consensus_mb": 0.1, "distillate_consensus_mb": -0.6,
    "crude_change_mb": 2.969, "cushing_change_mb": 2.266, "cushing_level_mb": 23.748,
    "gasoline_change_mb": -1.686, "distillate_change_mb": -0.428, "refinery_util_change_pct": -2.8,
    "api_crude_mb": 1.786,
}
MARKET = {"atr_20": 4.839, "ovx": 53.74, "cl1_cl2": 2.24, "crack_321": 61.6, "brent_wti": 6.8, "dxy": 101.3,
          "wti": 89.7, "as_of": "22-09-2026", "fetched_at": "23-09-2026 18:00"}
SIGMA = 1.5          # TLS 2.226 -> Z 1.48, past the 1.25 gate
TODAY = datetime.date(2026, 9, 23)


def stub(text="analysis", name="groq/test"):
    return lambda prompt: (text, name)


def signal(sigma=SIGMA, market=None, lots=None, analyse=None, **overrides):
    return build_signal({**INPUTS, **overrides}, market or MARKET, sigma, 84.0, "fallback", lots,
                        analyse=analyse or stub(), generated_at="23-09-2026 20:02")


# ------------------------------------------------------------------ the decision

def test_reference_week_is_regime_1_bearish_put_with_deep_itm_delta():
    s = signal()
    c, t = s["calculations"], s["signal"]
    assert c["tls_mb"] == pytest.approx(3.569 - 0.80 * 1.786 + 0.50 * 0.172, abs=1e-3)
    assert c["z_tls"] == pytest.approx(1.48, abs=0.01) and c["api_aligns"] is True
    assert (t["action"], t["regime"], t["direction"], t["option_type"], t["strike_type"]) \
        == ("trade", 1, "bearish", "PUT", "ITM")
    assert c["cushing_contradicts"] is False and c["cushing_multiplier"] == pytest.approx(1.391, abs=1e-3)
    assert s["option"]["ovx_deepened"] is True and (s["option"]["delta_low"], s["option"]["delta_high"]) == (0.80, 0.85)
    assert (s["option"]["expiry_date"], s["option"]["days_to_expiry"], s["option"]["rolled"]) == ("15-10-2026", 22, False)
    assert s["option"]["expiry_source"] == "mcx_calendar" and not any("GUESS" in line for line in s["checklist"])
    assert s["schedule"]["release_ist"] == "20:00" and s["schedule"]["hard_exit_ist"] == "22:30"


def test_expected_move_is_negative_for_a_build_and_flags_the_sanity_band():
    move = signal()["expected_move"]
    assert move["wti_usd"] == pytest.approx(-2.005, abs=0.01) and move["mcx_inr"] == -168   # at 84.0
    assert (move["anchor_low_usd"], move["anchor_high_usd"]) == (-0.33, -0.67)                # 0.15 / 0.30 per mb
    assert (move["anchor_low_inr"], move["anchor_high_inr"]) == (-28, -56)
    band = move["band"]                                                                       # WTI 89.7 x 84 INR
    assert (band["futures_price_inr"], band["band_inr"], band["near_band"]) == (7535, 301, False)
    assert band["move_share_of_band"] == pytest.approx(0.19, abs=0.01)         # the top of the anchor range, not beta_vol
    assert signal(market={**MARKET, "wti": None})["expected_move"]["band"] is None            # no price: no band
    assert move["per_mb_usd"] > 0.30 and move["sanity_ok"] is False        # OVX 54 pushes it past 0.15-0.30
    calm = signal(market={**MARKET, "ovx": 30.0, "atr_20": 2.0})
    assert calm["expected_move"]["sanity_ok"] is True and calm["option"]["ovx_deepened"] is False


def test_small_surprise_relative_to_sigma_stands_down_with_no_trade_detail():
    s = signal(sigma=5.0)
    assert s["signal"] == {"action": "stand_down", "regime": None, "direction": "neutral",
                           "option_type": "NONE", "strike_type": "NONE", "reason": "z_below_gate"}
    assert s["expected_move"] is s["option"] is s["sizing"] is None and s["checklist"] == []
    assert (s["analysis"], s["model_used"]) == ("", "rule_based")      # no narrative is asked for on a stand-down


def test_cushing_contradiction_routes_to_regime_2_and_fades_the_headline():
    s = signal(cushing_change_mb=-1.5, cushing_level_mb=40.0)
    assert (s["signal"]["regime"], s["signal"]["direction"], s["signal"]["option_type"]) == (2, "bullish", "CALL")
    assert s["calculations"]["cushing_contradicts"] is True and s["calculations"]["cushing_status"] == "contradicts"
    assert s["expected_move"] is None                      # targets are chart levels
    assert any("FADE" in line for line in s["checklist"])


def test_bullish_regime_1_call_with_a_positive_expected_move():
    s = signal(crude_change_mb=-4.0, gasoline_change_mb=-1.0, distillate_change_mb=-1.0,
               cushing_change_mb=-1.0, api_crude_mb=-1.0)
    assert (s["signal"]["regime"], s["signal"]["option_type"]) == (1, "CALL")
    assert s["expected_move"]["wti_usd"] > 0 and s["expected_move"]["mcx_inr"] > 0
    assert any("CL1-CL2 to widen" in line for line in s["checklist"])


def test_the_time_spread_reminder_is_only_for_longs():
    assert not any("CL1-CL2 to widen" in line for line in signal()["checklist"])      # the bearish reference week


REGIME_3 = dict(crude_change_mb=-1.0, gasoline_change_mb=-3.0, distillate_change_mb=-2.0,
                cushing_change_mb=-1.0, api_crude_mb=-5.0)


def test_regime_3_sell_the_fact_when_the_eia_draw_falls_short_of_an_extreme_api_draw_and_wti_rallied():
    s = signal(market={**MARKET, "overnight_rally_usd": 1.8}, **REGIME_3)
    assert (s["signal"]["regime"], s["signal"]["direction"], s["signal"]["option_type"]) == (3, "bearish", "PUT")
    assert s["scorecard"]["api_prepositioned"] is True and s["expected_move"] is None
    assert s["calculations"]["overnight_rally_usd"] == 1.8 and s["calculations"]["regime3_setup"] is True
    assert any("rally is confirmed" in line for line in s["checklist"])


@pytest.mark.parametrize("rally,said", [(None, "unknown"), (0.4, "only +0.40 USD")])
def test_regime_3_setup_without_a_confirmed_rally_falls_back_to_regime_1_and_says_why(rally, said):
    s = signal(market={**MARKET, "overnight_rally_usd": rally}, **REGIME_3)
    assert (s["signal"]["regime"], s["signal"]["direction"]) == (1, "bullish")
    assert s["calculations"]["regime3_setup"] is True
    assert said in s["checklist"][0] and "not fired" in s["checklist"][0]


def test_a_small_opposite_cushing_move_does_not_turn_a_big_headline_into_a_fade():
    s = signal(crude_change_mb=-8.0, cushing_change_mb=0.1, gasoline_change_mb=-1.0, distillate_change_mb=-1.0,
               api_crude_mb=-1.0)
    assert s["signal"]["regime"] == 1 and s["signal"]["direction"] == "bullish"
    assert s["calculations"]["cushing_contradicts"] is False and s["calculations"]["cushing_status"] == "immaterial"


def test_unknown_cushing_data_is_neutral_not_a_route_to_regime_2():
    s = signal(cushing_change_mb=None, cushing_level_mb=None)
    c = s["calculations"]
    assert c["cushing_contradicts"] is None and c["cushing_multiplier"] == 1.0 and c["cushing_level_known"] is False
    assert s["signal"]["regime"] == 1


def test_scorecard_reads_the_thresholds_and_keeps_unknowns_none():
    card = signal(market={**MARKET, "cl1_cl2": 0.30, "crack_321": None, "brent_wti": 5.51})["scorecard"]
    assert card == {"backwardation": False, "crack_321": None, "brent_wti": True, "api_prepositioned": False}


def test_sizing_shows_what_your_lots_risk_at_the_mid_delta():
    s = signal(lots={"CRUDEOIL": 2, "CRUDEOILM": 3})      # OVX 54: delta 0.80-0.85, mid 0.825; USD/INR 84
    assert s["sizing"]["delta_used"] == 0.825
    assert s["sizing"]["contracts"] == {
        "CRUDEOIL": {"lots": 2, "barrels_per_lot": 100,
                     "risk_inr_by_futures_stop_usd": {"0.18": 2495, "0.25": 3465, "0.35": 4851}},
        "CRUDEOILM": {"lots": 3, "barrels_per_lot": 10,
                      "risk_inr_by_futures_stop_usd": {"0.18": 374, "0.25": 520, "0.35": 728}}}
    assert signal()["sizing"] is None                     # no lot count configured: nothing is invented


def test_prompt_states_which_way_the_surprise_points():
    build, draw = signal(), signal(crude_change_mb=-4.0)
    assert "BUILD is bearish" in se.build_prompt(INPUTS, build["calculations"], build["signal"])
    assert "DRAW is bullish" in se.build_prompt(INPUTS, draw["calculations"], draw["signal"])


def test_prompt_survives_missing_optional_values():
    s = signal(cushing_change_mb=None, refinery_util_change_pct=None)
    assert se.build_prompt({**INPUTS, "cushing_change_mb": None, "refinery_util_change_pct": None},
                           s["calculations"], s["signal"]).count("N/A") == 2


def test_ai_output_cannot_change_the_signal():
    lying = lambda prompt: ("STRONG BUY, regime 1, confidence 99", "groq/x")
    honest, lied = signal(), signal(analyse=lying)
    for block in ("signal", "calculations", "option", "expected_move", "sizing", "checklist"):
        assert lied[block] == honest[block]


# ------------------------------------------------------------------------ inputs

CONSENSUS = {"release_date": "23-09-2026", "crude_consensus_mb": -0.6,
             "gasoline_consensus_mb": 0.1, "distillate_consensus_mb": -0.6}
API = {"release_date": "22-09-2026", "api_crude_mb": 1.786}
EIA = {"release_date": "23-09-2026", "crude_change_mb": 2.969, "cushing_change_mb": 2.266,
       "cushing_level_mb": 23.748, "gasoline_change_mb": -1.686, "distillate_change_mb": -0.428,
       "refinery_util_change_pct": -2.8}


def history(weeks=10):
    """Weekly rows before 23-09-2026 with a spread of surprises."""
    start = datetime.date(2026, 7, 8)
    crude = [1.0, -2.0, 3.0, -1.0, 2.0, -3.0, 1.5, -0.5, 2.5, -1.5, 0.5, 1.0]
    return [{"release_date": (start + datetime.timedelta(weeks=i)).strftime("%d-%m-%Y"),
             "crude_surprise_mb": crude[i], "gasoline_surprise_mb": 0.0, "distillate_surprise_mb": 0.0}
            for i in range(weeks)]


def write(tmp_path, consensus=None, api=None, eia=None, market=None, rows=None):
    files = {"consensus.json": CONSENSUS if consensus is None else consensus,
             "api_report.json": API if api is None else api,
             "eia_actuals.json": EIA if eia is None else eia,
             "market.json": MARKET if market is None else market,
             "surprise_history.json": history() if rows is None else rows}
    for name, content in files.items():
        (tmp_path / name).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return tmp_path


def test_load_inputs_happy_path_and_api_release_is_the_day_before(tmp_path):
    inputs = load_inputs(write(tmp_path), TODAY)
    assert inputs["release_date"] == "23-09-2026" and inputs["crude_change_mb"] == 2.969
    assert inputs["api_crude_mb"] == 1.786 and inputs["cushing_level_mb"] == 23.748


@pytest.mark.parametrize("which,content,title", [
    ("consensus", "{not json", "Invalid JSON"),
    ("api", "[1, 2]", "Invalid JSON"),
    ("eia", {"crude_change_mb": 1}, "release_date missing"),
    ("eia", {"release_date": "2026-09-23", "crude_change_mb": 1}, "release_date invalid"),
])
def test_load_inputs_rejects_bad_files(tmp_path, which, content, title):
    with pytest.raises(InputError) as err:
        load_inputs(write(tmp_path, **{which: content}), TODAY)
    assert err.value.title == title


def test_load_inputs_missing_file(tmp_path):
    write(tmp_path)
    (tmp_path / "api_report.json").unlink()
    with pytest.raises(InputError, match="API report") as err:
        load_inputs(tmp_path, TODAY)
    assert err.value.title == "Input file missing"


def test_load_inputs_stale_and_allow_stale(tmp_path):
    write(tmp_path)
    with pytest.raises(InputError) as err:
        load_inputs(tmp_path, datetime.date(2026, 10, 5))
    assert err.value.title == "Stale data"
    assert load_inputs(tmp_path, datetime.date(2026, 10, 5), allow_stale=True)["release_date"] == "23-09-2026"


@pytest.mark.parametrize("which,content", [
    ("consensus", {"release_date": "16-09-2026", "crude_consensus_mb": -1.6}),   # last week's consensus
    ("api", {"release_date": "15-09-2026", "api_crude_mb": 1.25}),               # last week's API report
    ("api", {"release_date": "24-09-2026", "api_crude_mb": 1.25}),               # API AFTER the EIA release
])
def test_release_date_cross_validation(tmp_path, which, content):
    with pytest.raises(InputError) as err:
        load_inputs(write(tmp_path, **{which: content}), TODAY, allow_stale=True)
    assert err.value.title == "Release date mismatch"


@pytest.mark.parametrize("which,field", [
    ("consensus", "gasoline_consensus_mb"), ("consensus", "distillate_consensus_mb"),
    ("eia", "crude_change_mb"), ("eia", "cushing_change_mb"), ("eia", "gasoline_change_mb"),
    ("eia", "distillate_change_mb"),
    ("api", "api_crude_mb"),
])
def test_all_three_liquids_cushing_and_the_api_crude_are_mandatory(tmp_path, which, field):
    base = {"consensus": CONSENSUS, "eia": EIA, "api": API}[which]
    with pytest.raises(InputError, match=field) as err:
        load_inputs(write(tmp_path, **{which: {**base, field: None}}), TODAY)
    assert err.value.title == "Mandatory field missing"


def test_cushing_level_and_refinery_are_optional(tmp_path):
    bare = {k: v for k, v in EIA.items() if k not in ("cushing_level_mb", "refinery_util_change_pct")}
    inputs = load_inputs(write(tmp_path, eia=bare), TODAY)
    assert inputs["cushing_level_mb"] is None and inputs["refinery_util_change_pct"] is None


def test_load_market_requires_atr_and_ovx_and_fresh_data(tmp_path):
    write(tmp_path)
    assert load_market(tmp_path, TODAY)["ovx"] == 53.74
    with pytest.raises(InputError, match="Stale") as err:
        load_market(tmp_path, datetime.date(2026, 10, 5))
    assert err.value.title == "Stale data"
    assert load_market(tmp_path, datetime.date(2026, 10, 5), allow_stale=True)["atr_20"] == 4.839
    write(tmp_path, market={**MARKET, "ovx": None})
    with pytest.raises(InputError, match="ovx") as err:
        load_market(tmp_path, TODAY)
    assert err.value.title == "Mandatory field missing"


def test_sigma_excludes_the_week_being_traded_and_refuses_a_short_history(tmp_path):
    rows = history(10)
    write(tmp_path, rows=rows)
    sigma, weeks = load_sigma("23-09-2026", tmp_path)
    assert weeks == 10 and sigma == pytest.approx(model.sigma_forecast(rows))
    assert load_sigma("23-09-2026", tmp_path, method="std")[0] == pytest.approx(model.sigma_forecast(rows, method="std"))
    same = load_sigma(rows[-1]["release_date"], tmp_path)          # the traded week is left out
    assert same[1] == 9
    write(tmp_path, rows=history(5))
    with pytest.raises(InputError, match="needs 8 weeks") as err:
        load_sigma("23-09-2026", tmp_path)
    assert err.value.title == "Not enough surprise history"


def test_sigma_method_defaults_to_mad_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("SIGMA_METHOD", raising=False)
    assert se.sigma_method() == "mad"
    monkeypatch.setenv("SIGMA_METHOD", "STD")
    assert se.sigma_method() == "std"
    monkeypatch.setenv("SIGMA_METHOD", "# mad or std")            # dotenv placeholder = unset
    assert se.sigma_method() == "mad"
    monkeypatch.setenv("SIGMA_METHOD", "mean")
    with pytest.raises(InputError, match="SIGMA_METHOD"):
        se.sigma_method()


def test_signal_records_which_sigma_method_set_the_gate():
    assert signal()["calculations"]["sigma_method"] == "mad"
    assert build_signal(INPUTS, MARKET, SIGMA, 84.0, "fallback", analyse=stub(), sigma_method="std")["calculations"]["sigma_method"] == "std"


def test_lot_settings_are_optional_but_never_silently_wrong(monkeypatch):
    names = ("MCX_CRUDEOIL_LOT_SIZE", "MCX_CRUDEOILM_LOT_SIZE", "MCX_NATURALGAS_LOT_SIZE",
             "MCX_NATURALGASM_LOT_SIZE", "MCX_CRUDEOIL_LOTS", "MCX_CRUDEOILM_LOTS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert load_lot_sizes() == {"CRUDEOIL": None, "CRUDEOILM": None, "NATURALGAS": None, "NATURALGASM": None}
    assert load_lot_counts() == {"CRUDEOIL": None, "CRUDEOILM": None}
    monkeypatch.setenv("MCX_CRUDEOIL_LOT_SIZE", "100")                     # the contract sizes MCX publishes
    monkeypatch.setenv("MCX_CRUDEOILM_LOT_SIZE", "10")
    monkeypatch.setenv("MCX_NATURALGAS_LOT_SIZE", "1250")                  # reserved: any positive whole number
    monkeypatch.setenv("MCX_NATURALGASM_LOT_SIZE", "250")
    assert load_lot_sizes() == {"CRUDEOIL": 100, "CRUDEOILM": 10, "NATURALGAS": 1250, "NATURALGASM": 250}
    monkeypatch.setenv("MCX_CRUDEOIL_LOTS", "2")
    monkeypatch.setenv("MCX_CRUDEOILM_LOTS", "10")
    assert load_lot_counts() == {"CRUDEOIL": 2, "CRUDEOILM": 10}
    monkeypatch.setenv("MCX_CRUDEOIL_LOTS", "# lots per signal")            # dotenv placeholder = unset
    assert load_lot_counts()["CRUDEOIL"] is None
    for name in names:
        for bad in ("two", "1.5", "0", "-1"):
            monkeypatch.setenv(name, bad)
            with pytest.raises(InputError, match=name):
                (load_lot_counts if name.endswith("LOTS") else load_lot_sizes)()
        monkeypatch.delenv(name)


@pytest.mark.parametrize("name,wrong,lots_name", [("MCX_CRUDEOIL_LOT_SIZE", "1", "MCX_CRUDEOIL_LOTS"),
                                                   ("MCX_CRUDEOIL_LOT_SIZE", "2", "MCX_CRUDEOIL_LOTS"),
                                                   ("MCX_CRUDEOILM_LOT_SIZE", "100", "MCX_CRUDEOILM_LOTS")])
def test_a_lot_size_that_is_not_the_exchanges_is_refused_and_points_to_the_lots_setting(monkeypatch, name, wrong, lots_name):
    """The usual mistake: putting the NUMBER of lots in a SIZE setting (LOT_SIZE=100 next to LOTS=... is right)."""
    monkeypatch.setenv(name, wrong)
    with pytest.raises(InputError, match=lots_name) as err:
        load_lot_sizes()
    assert "barrels" in err.value.detail


# ----------------------------------------------------- external calls (all stubbed)

def fake_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def fake_yfinance(monkeypatch, price=None, error=None):
    class Ticker:
        def __init__(self, symbol):
            assert symbol == "INR=X"

        @property
        def fast_info(self):
            if error:
                raise error
            return {"lastPrice": price}
    fake_module(monkeypatch, "yfinance", Ticker=Ticker)


KEY = "fca_live_SECRETKEY123"


def freecurrency(rate=None, status=200, error=None, calls=None):
    """A stand-in for httpx.get that answers like FreeCurrencyAPI."""
    def get(url, params=None, timeout=None):
        if calls is not None:
            calls.append((url, params))
        if error:
            raise error
        request = httpx.Request("GET", url, params=params)
        body = {"data": {"INR": rate}} if status == 200 else {"message": "nope"}
        return httpx.Response(status, json=body, request=request)
    return get


def test_usd_inr_from_yfinance_needs_no_key(monkeypatch):
    monkeypatch.delenv("FREECURRENCYAPI_KEY", raising=False)
    fake_yfinance(monkeypatch, price=83.47)
    assert fetch_usd_inr() == (83.47, "yfinance")


def test_usd_inr_falls_back_to_freecurrencyapi_when_yfinance_fails(monkeypatch):
    monkeypatch.setenv("FREECURRENCYAPI_KEY", KEY)
    fake_yfinance(monkeypatch, error=RuntimeError("down"))
    calls = []
    assert fetch_usd_inr(get=freecurrency(95.91, calls=calls)) == (95.91, "freecurrencyapi")
    assert calls[0][1] == {"apikey": KEY, "base_currency": "USD", "currencies": "INR"}


def test_the_backup_is_not_called_when_yfinance_works(monkeypatch):
    monkeypatch.setenv("FREECURRENCYAPI_KEY", KEY)
    fake_yfinance(monkeypatch, price=95.5)
    calls = []
    assert fetch_usd_inr(get=freecurrency(1.0, calls=calls)) == (95.5, "yfinance") and calls == []


@pytest.mark.parametrize("kwargs", [{"error": RuntimeError("down")}, {"price": 5.0}, {"price": 0.0}, {"price": 9999.0}])
def test_a_bad_yfinance_quote_moves_on_to_the_backup(monkeypatch, kwargs):
    monkeypatch.setenv("FREECURRENCYAPI_KEY", KEY)
    fake_yfinance(monkeypatch, **kwargs)
    assert fetch_usd_inr(get=freecurrency(95.9)) == (95.9, "freecurrencyapi")


@pytest.mark.parametrize("backup", [
    dict(rate=95.9, status=429),                                  # rate limited
    dict(rate=95.9, status=500),
    dict(error=httpx.ConnectError("no route")),
    dict(rate=9999.0),                                            # implausible
    dict(rate=0.0),
])
def test_no_source_means_an_error_not_a_default_rate(monkeypatch, backup):
    monkeypatch.setenv("FREECURRENCYAPI_KEY", KEY)
    fake_yfinance(monkeypatch, error=RuntimeError("down"))
    with pytest.raises(InputError) as err:
        fetch_usd_inr(get=freecurrency(**backup))
    assert err.value.title == "USD/INR unavailable" and "yfinance" in err.value.detail


def test_without_a_key_only_yfinance_is_tried_and_the_error_says_so(monkeypatch):
    monkeypatch.delenv("FREECURRENCYAPI_KEY", raising=False)
    fake_yfinance(monkeypatch, error=RuntimeError("down"))
    with pytest.raises(InputError, match="FREECURRENCYAPI_KEY not set"):
        fetch_usd_inr()
    monkeypatch.setenv("FREECURRENCYAPI_KEY", "# get one at freecurrencyapi.com")   # placeholder = unset
    with pytest.raises(InputError, match="FREECURRENCYAPI_KEY not set"):
        fetch_usd_inr()


def test_the_api_key_never_reaches_an_error_message_or_a_log(monkeypatch, caplog):
    """The key travels in the request URL; httpx puts that URL in its exception text."""
    monkeypatch.setenv("FREECURRENCYAPI_KEY", KEY)
    fake_yfinance(monkeypatch, error=RuntimeError("down"))
    for backup in (dict(rate=1.0, status=429), dict(error=httpx.ConnectError(f"cannot reach ?apikey={KEY}"))):
        with caplog.at_level(logging.DEBUG), pytest.raises(InputError) as err:
            fetch_usd_inr(get=freecurrency(**backup))
        assert KEY not in err.value.detail and KEY not in str(err.value) and KEY not in caplog.text


def fake_groq(monkeypatch, content=None, error=None, calls=None, finish_reason="stop"):
    class Client:
        def __init__(self, api_key, timeout):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            if error:
                raise error
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)])
    fake_module(monkeypatch, "groq", Groq=Client)


THREE = ("The +3.569 mb crude build signals immediate WTI weakness. "
         "The +2.266 mb Cushing build confirms it. A refinery restart is the key risk.")


def analyse_with(monkeypatch, content, finish_reason="stop"):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GROQ_MODEL", "llama-test")
    fake_groq(monkeypatch, content=content, finish_reason=finish_reason)
    return generate_analysis("prompt")


def test_groq_success_keeps_all_three_sentences_and_names_the_model(monkeypatch):
    calls = []
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("GROQ_MODEL", "llama-test")
    fake_groq(monkeypatch, content="  " + THREE + "  ", calls=calls)
    assert generate_analysis("prompt") == (THREE, "groq/llama-test")     # ~150 chars: the old 200 cap would not have saved a real reply
    assert calls[0]["max_tokens"] == 150 and calls[0]["temperature"] == 0.3
    assert calls[0]["messages"][0]["content"] == se.GROQ_SYSTEM_PROMPT


def test_a_normal_length_reply_is_never_cut(monkeypatch):
    reply = ("The +3.569 mb crude inventory surplus signals immediate downward pressure on WTI prices. The +2.266 mb "
             "Cushing build confirms this bearish signal, while the -2.800% drop in refinery utilization removes "
             "demand support. A sudden geopolitical escalation could instantly reverse this price action.")
    assert 250 < len(reply) < se.ANALYSIS_MAX_CHARS
    assert analyse_with(monkeypatch, reply)[0] == reply


def test_an_over_long_reply_is_cut_at_a_sentence_end_never_mid_word(monkeypatch):
    text, _ = analyse_with(monkeypatch, "Sentence number one is here. " * 40)
    assert len(text) <= se.ANALYSIS_MAX_CHARS and text.endswith("here.") and text.count("Sentence") == text.count(".")


def test_a_reply_cut_by_max_tokens_drops_the_unfinished_sentence(monkeypatch):
    text, _ = analyse_with(monkeypatch, "Build of +3.569 mb signals weakness. Cushing +2.266 mb confirms. The refinery uti", "length")
    assert text == "Build of +3.569 mb signals weakness. Cushing +2.266 mb confirms."


def test_decimal_points_are_not_sentence_ends():
    assert se.complete_sentences("Build of +3.569 mb signals weakness. The refinery is at 97.8") == "Build of +3.569 mb signals weakness."
    assert se.complete_sentences("no terminator here") == "no terminator here"
    assert se.complete_sentences("Done! Really? Yes and then") == "Done! Really?"


def test_no_terminator_at_all_falls_back_to_a_hard_cut(monkeypatch):
    text, _ = analyse_with(monkeypatch, "x" * 900)
    assert text == "x" * se.ANALYSIS_MAX_CHARS


def test_groq_failure_or_missing_key_never_blocks(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    fake_groq(monkeypatch, error=RuntimeError("401"))
    assert generate_analysis("p") == ("", "rule_based")
    monkeypatch.setenv("GROQ_API_KEY", "# free at console.groq.com")   # dotenv placeholder = unset
    assert generate_analysis("p") == ("", "rule_based")
    monkeypatch.delenv("GROQ_API_KEY")
    assert generate_analysis("p") == ("", "rule_based")



# -------------------------------------------------------------------------- main

def patch_main(monkeypatch, tmp_path):
    for const, name in (("CONSENSUS_FILE", "consensus.json"), ("API_REPORT_FILE", "api_report.json"),
                        ("EIA_ACTUALS_FILE", "eia_actuals.json"), ("MARKET_FILE", "market.json"),
                        ("SURPRISE_HISTORY_FILE", "surprise_history.json"), ("SIGNAL_FILE", "signal.json")):
        monkeypatch.setattr(se, const, tmp_path / name)
    monkeypatch.setattr(se, "DATA_DIR", tmp_path)
    monkeypatch.setattr(se, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("MCX_CRUDEOIL_LOT_SIZE", raising=False)
    monkeypatch.delenv("MCX_CRUDEOILM_LOT_SIZE", raising=False)
    monkeypatch.delenv("MCX_NATURALGAS_LOT_SIZE", raising=False)
    monkeypatch.delenv("MCX_NATURALGASM_LOT_SIZE", raising=False)
    monkeypatch.delenv("MCX_CRUDEOIL_LOTS", raising=False)
    monkeypatch.delenv("MCX_CRUDEOILM_LOTS", raising=False)
    monkeypatch.delenv("SIGMA_METHOD", raising=False)
    monkeypatch.delenv("FREECURRENCYAPI_KEY", raising=False)
    fake_yfinance(monkeypatch, price=84.0)
    alerts, recorded = [], []
    monkeypatch.setattr(se, "send_error", lambda script, msg: alerts.append((script, msg)))
    monkeypatch.setattr(se, "record_week", lambda consensus, actuals: recorded.append(actuals["release_date"]))
    return alerts, recorded


def test_main_writes_signal_json_records_the_week_and_sends_nothing(monkeypatch, tmp_path):
    alerts, recorded = patch_main(monkeypatch, tmp_path)
    monkeypatch.setenv("MCX_CRUDEOIL_LOTS", "1")
    write(tmp_path)
    assert se.main(["--allow-stale"]) == 0
    saved = json.loads((tmp_path / "signal.json").read_text(encoding="utf-8"))
    assert saved["signal"]["action"] in ("trade", "stand_down") and saved["model_used"] == "rule_based"
    assert saved["sizing"] is not None or saved["signal"]["action"] == "stand_down"
    assert alerts == [] and recorded == ["23-09-2026"]


def test_main_input_error_alerts_telegram_writes_nothing_and_records_nothing(monkeypatch, tmp_path):
    alerts, recorded = patch_main(monkeypatch, tmp_path)
    write(tmp_path, eia={"release_date": "23-09-2026"})
    assert se.main(["--allow-stale"]) == 1
    assert not (tmp_path / "signal.json").exists() and recorded == []
    assert alerts and "Mandatory field missing" in alerts[0][1] and "crude_change_mb" in alerts[0][1]


def test_main_without_enough_history_alerts_instead_of_guessing_sigma(monkeypatch, tmp_path):
    alerts, _ = patch_main(monkeypatch, tmp_path)
    write(tmp_path, rows=history(3))
    assert se.main(["--allow-stale"]) == 1
    assert not (tmp_path / "signal.json").exists() and "Not enough surprise history" in alerts[0][1]


def test_main_unexpected_error_alerts_telegram(monkeypatch, tmp_path):
    alerts, _ = patch_main(monkeypatch, tmp_path)
    write(tmp_path)
    monkeypatch.setattr(se, "build_signal", lambda *a, **k: 1 / 0)
    assert se.main(["--allow-stale"]) == 1
    assert alerts and "ZeroDivisionError" in alerts[0][1]


def test_api_aligns_compares_the_api_surprise_with_the_eia_crude_surprise():
    base = {**INPUTS, "crude_consensus_mb": -3.0, "api_crude_mb": -1.0, "crude_change_mb": 2.0}   # API +2.0 vs consensus, EIA +5.0
    assert build_signal(base, MARKET, SIGMA, 84.0, "fallback", analyse=stub())["calculations"]["api_aligns"] is True
    opposed = {**base, "api_crude_mb": -5.0}                                                       # API -2.0 vs consensus
    assert build_signal(opposed, MARKET, SIGMA, 84.0, "fallback", analyse=stub())["calculations"]["api_aligns"] is False


def test_a_non_numeric_value_is_an_input_error_naming_the_field():
    with pytest.raises(InputError, match="crude_change_mb='N/A'"):
        se._num({"crude_change_mb": "N/A"}, "crude_change_mb")
    assert se._num({}, "x") is None and se._num({"x": "1.23456"}, "x") == 1.235


def test_a_trade_signal_on_a_day_the_evening_session_is_closed_says_so_first():
    s = build_signal({**INPUTS, "release_date": "26-01-2026", "crude_change_mb": 12.0},
                     {**MARKET, "fetched_at": "26-01-2026 18:00"}, SIGMA, 84.0, "fallback", analyse=stub())
    assert s["signal"]["action"] == "stand_down" and s["signal"]["reason"] == "mcx_evening_closed: Republic Day"
    assert s["signal"]["regime"] == 1                                     # the decision is kept for the record
    assert s["schedule"]["mcx_evening_open"] is False and s["schedule"]["mcx_closed_reason"] == "Republic Day"
    assert "EVENING SESSION IS CLOSED" in s["checklist"][0]
    assert signal()["schedule"]["mcx_evening_open"] is True             # an ordinary Wednesday


def test_an_expiry_outside_the_mcx_calendar_is_flagged_first_on_the_checklist():
    s = build_signal({**INPUTS, "release_date": "23-12-2026", "crude_change_mb": 12.0},
                     {**MARKET, "fetched_at": "23-12-2026 18:00"}, SIGMA, 84.0, "fallback", analyse=stub())
    assert s["signal"]["action"] == "trade" and s["option"]["expiry_source"] == "assumed_19th"
    assert "GUESS" in s["checklist"][0] and s["option"]["expiry_date"] in s["checklist"][0]


def test_main_stops_with_an_alert_when_no_usd_inr_source_answers(monkeypatch, tmp_path):
    alerts, recorded = patch_main(monkeypatch, tmp_path)
    fake_yfinance(monkeypatch, error=RuntimeError("down"))
    write(tmp_path)
    assert se.main(["--allow-stale"]) == 1
    assert not (tmp_path / "signal.json").exists() and recorded == []
    assert alerts and "USD/INR unavailable" in alerts[0][1]


def test_a_trade_signal_carries_the_strike_guide_and_the_rupee_block():
    s = signal(market={**MARKET, "usd_inr_trend_pct": 0.62})
    guide = s["option"]["strike_guide"]
    assert guide["futures_level_inr"] == round(89.7 * 84.0) and guide["atm_strike"] % 50 == 0
    assert guide["strike_at_delta_low"] < guide["strike_at_delta_high"]      # a PUT: the deeper the delta, the HIGHER the strike
    assert 0.78 <= guide["delta_at_delta_low_strike"] <= 0.82 and 0.83 <= guide["delta_at_delta_high_strike"] <= 0.87
    rupee = s["currency"]
    assert (rupee["usd_inr_trend_pct"], rupee["direction"], rupee["effect"]) == (0.62, "inr_weakening", "dampens")
    assert any("working against a bearish MCX move" in note for note in rupee["notes"])


def test_a_stand_down_has_no_strike_guide_or_rupee_block():
    s = signal(sigma=5.0)
    assert s["option"] is None and s["currency"] is None


def test_without_a_wti_price_there_is_no_strike_guide_but_the_signal_still_ships():
    s = signal(market={**MARKET, "wti": None})
    assert s["signal"]["action"] == "trade" and "strike_guide" not in s["option"]


def test_main_refuses_a_lot_count_typed_into_a_lot_size_setting(monkeypatch, tmp_path):
    alerts, recorded = patch_main(monkeypatch, tmp_path)
    monkeypatch.setenv("MCX_CRUDEOIL_LOT_SIZE", "2")
    write(tmp_path)
    assert se.main(["--allow-stale"]) == 1 and not (tmp_path / "signal.json").exists() and recorded == []
    assert "MCX_CRUDEOIL_LOTS" in alerts[0][1]
