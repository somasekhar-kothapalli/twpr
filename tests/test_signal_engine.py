import datetime
import json
import sys
import types

import pytest

from app import model
from app import signal_engine as se
from app.signal_engine import (InputError, build_signal, fetch_usd_inr, generate_analysis, load_equity, load_inputs,
                               load_market, load_sigma)

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


def signal(sigma=SIGMA, market=None, equity=None, max_lots=None, analyse=None, **overrides):
    return build_signal({**INPUTS, **overrides}, market or MARKET, sigma, 84.0, "fallback", equity, max_lots,
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
    assert s["schedule"]["release_ist"] == "20:00" and s["schedule"]["hard_exit_ist"] == "22:30"


def test_expected_move_is_negative_for_a_build_and_flags_the_sanity_band():
    move = signal()["expected_move"]
    assert move["wti_usd"] == pytest.approx(-2.005, abs=0.01) and move["mcx_inr"] == -168   # at 84.0
    assert (move["anchor_low_usd"], move["anchor_high_usd"]) == (-0.33, -0.67)                # 0.15 / 0.30 per mb
    assert (move["anchor_low_inr"], move["anchor_high_inr"]) == (-28, -56)
    assert move["per_mb_usd"] > 0.30 and move["sanity_ok"] is False        # OVX 54 pushes it past 0.15-0.30
    calm = signal(market={**MARKET, "ovx": 30.0, "atr_20": 2.0})
    assert calm["expected_move"]["sanity_ok"] is True and calm["option"]["ovx_deepened"] is False


def test_small_surprise_relative_to_sigma_stands_down_with_no_trade_detail():
    s = signal(sigma=5.0)
    assert s["signal"] == {"action": "stand_down", "regime": None, "direction": "neutral",
                           "option_type": "NONE", "strike_type": "NONE"}
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


def test_sizing_uses_the_mid_delta_and_the_lot_cap():
    s = signal(equity=1_000_000.0)
    assert s["sizing"]["risk_inr"] == 10_000 and s["sizing"]["delta_used"] == 0.825
    assert s["sizing"]["lots_by_futures_stop_usd"] == {"0.18": 8, "0.25": 5, "0.35": 4}     # at USD/INR 84
    capped = signal(equity=1_000_000.0, max_lots=1)["sizing"]
    assert set(capped["lots_by_futures_stop_usd"].values()) == {1} and capped["max_lots"] == 1
    assert signal()["sizing"] is None


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


def test_account_settings_are_optional_but_never_silently_wrong(monkeypatch):
    monkeypatch.delenv("ACCOUNT_EQUITY_INR", raising=False)
    monkeypatch.delenv("MAX_LOTS", raising=False)
    assert load_equity() == (None, None)
    monkeypatch.setenv("ACCOUNT_EQUITY_INR", "1,000,000")
    monkeypatch.setenv("MAX_LOTS", "1")
    assert load_equity() == (1_000_000.0, 1)
    monkeypatch.setenv("ACCOUNT_EQUITY_INR", "# your capital in INR")      # dotenv placeholder = unset
    assert load_equity()[0] is None
    for bad in ("ten lakh", "-5", "0"):
        monkeypatch.setenv("ACCOUNT_EQUITY_INR", bad)
        with pytest.raises(InputError, match="ACCOUNT_EQUITY_INR"):
            load_equity()


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


def test_usd_inr_from_yfinance(monkeypatch):
    fake_yfinance(monkeypatch, price=83.47)
    assert fetch_usd_inr() == (83.47, "yfinance")


@pytest.mark.parametrize("kwargs", [{"error": RuntimeError("down")}, {"price": 5.0}, {"price": 0.0}, {"price": 9999.0}])
def test_usd_inr_falls_back(monkeypatch, kwargs):
    fake_yfinance(monkeypatch, **kwargs)
    assert fetch_usd_inr() == (84.0, "fallback")


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
    monkeypatch.delenv("ACCOUNT_EQUITY_INR", raising=False)
    monkeypatch.delenv("MAX_LOTS", raising=False)
    monkeypatch.delenv("SIGMA_METHOD", raising=False)
    fake_yfinance(monkeypatch, price=84.0)
    alerts, recorded = [], []
    monkeypatch.setattr(se, "send_error", lambda script, msg: alerts.append((script, msg)))
    monkeypatch.setattr(se, "record_week", lambda consensus, actuals: recorded.append(actuals["release_date"]))
    return alerts, recorded


def test_main_writes_signal_json_records_the_week_and_sends_nothing(monkeypatch, tmp_path):
    alerts, recorded = patch_main(monkeypatch, tmp_path)
    monkeypatch.setenv("ACCOUNT_EQUITY_INR", "1000000")
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
