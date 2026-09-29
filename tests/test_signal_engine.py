import datetime
import json
import sys
import types

import pytest

from app import signal_engine as se
from app.signal_engine import (InputError, apply_cushing_adjustment, apply_grade_logic, build_signal,
                               calculate_confidence, calculate_deviations, check_api_alignment, check_products,
                               fetch_usd_inr, generate_analysis, get_expected_move, get_trade_recommendation,
                               load_inputs)

# The Sep 4 2026 reference week (data/reference_week.json in the old pipeline).
REFERENCE = {
    "release_date": "30-09-2026",
    "crude_consensus_mb": -1.6, "gasoline_consensus_mb": -1.4, "distillate_consensus_mb": -0.7,
    "crude_change_mb": -0.391, "cushing_change_mb": -0.684, "gasoline_change_mb": 1.269,
    "distillate_change_mb": 2.087, "refinery_util_change_pct": -2.8,
    "api_crude_mb": 1.25, "api_cushing_mb": -0.684,
}


# ------------------------------------------------------------------ pure rules

def test_deviations_round_to_3dp_and_product_none_propagates():
    d = calculate_deviations(-0.391, -1.6, 1.269, -1.4, 2.087, -0.7)
    assert d == {"crude_deviation_mb": 1.209, "gasoline_deviation_mb": 2.669, "distillate_deviation_mb": 2.787}
    d = calculate_deviations(-0.391, -1.6, None, -1.4, 2.087, None)
    assert d["gasoline_deviation_mb"] is None and d["distillate_deviation_mb"] is None


@pytest.mark.parametrize("deviation,expected", [
    (0.0, ("skip", "neutral")), (1.0, ("skip", "neutral")), (-1.0, ("skip", "neutral")),   # inclusive skip zone
    (1.001, ("B", "bearish")), (-1.001, ("B", "bullish")),
    (1.499, ("B", "bearish")), (1.5, ("A", "bearish")),                                     # inclusive Grade A
    (-1.499, ("B", "bullish")), (-1.5, ("A", "bullish")), (3.569, ("A", "bearish")),
])
def test_grade_logic_boundaries(deviation, expected):
    assert apply_grade_logic(deviation) == expected


@pytest.mark.parametrize("grade,direction,cushing,expected", [
    ("A", "bullish", 0.5, ("B", True)),     # a build contradicts bullish -> downgrade
    ("A", "bearish", -0.5, ("B", True)),    # a draw contradicts bearish -> downgrade
    ("A", "bullish", -0.5, ("A", False)),
    ("A", "bearish", 0.5, ("A", False)),
    ("B", "bullish", 0.5, ("B", True)),     # B is the floor
    ("A", "bullish", 0.0, ("A", False)),    # exactly 0.0 contradicts nothing
    ("A", "bearish", None, ("A", None)),    # missing -> no adjustment
    ("skip", "neutral", 1.0, ("skip", None)),
])
def test_cushing_adjustment(grade, direction, cushing, expected):
    assert apply_cushing_adjustment(grade, direction, cushing) == expected


@pytest.mark.parametrize("direction,gas,dist,expected", [
    ("bearish", 2.669, 2.787, True),        # the reference week: same-sign surprise
    ("bearish", 2.001, 2.001, True),
    ("bearish", 2.0, 2.787, False),         # strictly greater than 2.0
    ("bearish", 2.669, 1.9, False),         # BOTH must exceed
    ("bearish", -2.5, -2.5, False),         # opposite sign does not count
    ("bullish", -2.5, -3.0, True),
    ("bullish", 2.5, 2.5, False),
    ("bearish", None, 2.787, None), ("bullish", -3.0, None, None), ("neutral", 5.0, 5.0, None),
])
def test_products(direction, gas, dist, expected):
    assert check_products(direction, gas, dist) is expected


@pytest.mark.parametrize("direction,api,expected", [
    ("bullish", -0.5, True), ("bullish", 0.5, False), ("bearish", 0.5, True), ("bearish", -0.5, False),
    ("bullish", 0.0, False), ("bearish", 0.0, False),
])
def test_api_alignment(direction, api, expected):
    assert check_api_alignment(direction, api) is expected


@pytest.mark.parametrize("grade,cushing,api,expected", [
    ("skip", None, False, 0),
    ("A", False, True, 85), ("A", True, True, 75), ("A", False, False, 75), ("A", True, False, 65),
    ("B", False, True, 65), ("B", True, True, 55), ("B", True, False, 45),
    ("B", None, True, 60), ("B", None, False, 50),          # missing Cushing: no adjustment
])
def test_confidence(grade, cushing, api, expected):
    assert calculate_confidence(grade, cushing, api) == expected


def test_confidence_is_clamped(monkeypatch):
    monkeypatch.setattr(se, "CONFIDENCE_MAX", 80)
    assert calculate_confidence("A", False, True) == 80
    monkeypatch.setattr(se, "CONFIDENCE_MIN", 50)
    assert calculate_confidence("B", True, False) == 50


def test_trade_recommendation():
    assert get_trade_recommendation("A", "bullish") == {"option_type": "CALL", "strike_type": "ATM", "size_pct": 2.0}
    assert get_trade_recommendation("A", "bearish") == {"option_type": "PUT", "strike_type": "ATM", "size_pct": 2.0}
    assert get_trade_recommendation("B", "bearish") == {"option_type": "PUT", "strike_type": "1-OTM", "size_pct": 1.5}
    assert get_trade_recommendation("skip", "neutral") == {"option_type": "NONE", "strike_type": "NONE", "size_pct": 0.0}


def test_expected_move():
    assert get_expected_move("B", "bearish", 84.0) == {
        "wti_low": -0.8, "wti_high": -1.5, "mcx_low": -67, "mcx_high": -126, "usd_inr": 84.0}
    assert get_expected_move("A", "bullish", 84.0)["mcx_high"] == 252
    move = get_expected_move("skip", "neutral", 84.0)
    assert (move["wti_low"], move["mcx_low"], move["mcx_high"]) == (0.0, 0, 0)


# -------------------------------------------------------------- assembled signal

def stub(text="analysis", model="groq/test"):
    return lambda prompt: (text, model)


def test_reference_week_end_to_end():
    signal = build_signal(REFERENCE, 84.0, "yfinance", analyse=stub(), generated_at="30-09-2026 20:05")
    assert signal["signal"] == {"grade": "B", "direction": "bearish", "confidence": 55,
                                "option_type": "PUT", "strike_type": "1-OTM", "size_pct": 1.5}
    assert signal["calculations"] == {
        "crude_deviation_mb": 1.209, "gasoline_deviation_mb": 2.669, "distillate_deviation_mb": 2.787,
        "cushing_contradicts": True, "products_oppose": True, "api_aligns": True}
    assert signal["expected_move"] == {"wti_low": -0.8, "wti_high": -1.5, "mcx_low": -67, "mcx_high": -126,
                                       "usd_inr": 84.0, "usd_inr_source": "yfinance"}
    assert signal["release_date"] == "30-09-2026" and signal["generated_at"] == "30-09-2026 20:05"
    assert list(signal) == ["release_date", "generated_at", "inputs", "calculations", "signal",
                            "expected_move", "analysis", "model_used"]
    assert "release_date" not in signal["inputs"] and signal["inputs"]["refinery_util_change_pct"] == -2.8


def test_skip_week_has_no_trade_and_null_flags():
    inputs = {**REFERENCE, "crude_change_mb": -1.2}     # deviation +0.4 -> inside the skip zone
    s = build_signal(inputs, 84.0, "fallback", analyse=stub())
    assert s["signal"] == {"grade": "skip", "direction": "neutral", "confidence": 0,
                           "option_type": "NONE", "strike_type": "NONE", "size_pct": 0.0}
    assert s["calculations"]["cushing_contradicts"] is None and s["calculations"]["api_aligns"] is None
    assert s["calculations"]["products_oppose"] is None
    assert (s["expected_move"]["wti_low"], s["expected_move"]["mcx_high"]) == (0.0, 0)


def test_grade_a_downgraded_by_cushing_uses_the_downgraded_grade_for_everything():
    inputs = {**REFERENCE, "crude_change_mb": 2.0}      # deviation +3.6 -> A bearish; cushing draw contradicts
    s = build_signal(inputs, 84.0, "fallback", analyse=stub())
    assert s["signal"]["grade"] == "B" and s["signal"]["confidence"] == 55
    assert (s["signal"]["strike_type"], s["signal"]["size_pct"], s["expected_move"]["wti_high"]) == ("1-OTM", 1.5, -1.5)


def test_missing_optional_inputs_are_null_not_zero():
    inputs = {**REFERENCE, "gasoline_change_mb": None, "distillate_consensus_mb": None,
              "cushing_change_mb": None, "refinery_util_change_pct": None, "api_cushing_mb": None}
    s = build_signal(inputs, 84.0, "fallback", analyse=stub())
    assert s["calculations"]["gasoline_deviation_mb"] is None and s["calculations"]["distillate_deviation_mb"] is None
    assert s["calculations"]["products_oppose"] is None and s["calculations"]["cushing_contradicts"] is None
    assert s["signal"]["confidence"] == 60             # B 55, no Cushing adjustment, API aligns +5
    assert s["inputs"]["cushing_change_mb"] is None
    json.dumps(s, allow_nan=False)                     # serialisable, no NaN


def test_prompt_uses_na_for_missing_values():
    seen = []
    build_signal({**REFERENCE, "cushing_change_mb": None, "refinery_util_change_pct": None}, 84.0, "fallback",
                 analyse=lambda p: seen.append(p) or ("", "rule_based"))
    assert "Cushing: N/A mb (unknown)" in seen[0] and "Refinery util change: N/A%" in seen[0]
    assert "Crude deviation: +1.209 mb" in seen[0] and "API crude: +1.250 mb (aligns)" in seen[0]


# ----------------------------------------------------------------- input loading

CONSENSUS = {"release_date": "30-09-2026", "crude_consensus_mb": -1.6,
             "gasoline_consensus_mb": -1.4, "distillate_consensus_mb": -0.7}
API = {"release_date": "29-09-2026", "api_crude_mb": 1.25, "api_cushing_mb": -0.684}
EIA = {"release_date": "30-09-2026", "crude_change_mb": -0.391, "cushing_change_mb": -0.684,
       "gasoline_change_mb": 1.269, "distillate_change_mb": 2.087, "refinery_util_change_pct": -2.8}
TODAY = datetime.date(2026, 9, 30)


def write(tmp_path, consensus=None, api=None, eia=None):
    files = {"consensus.json": CONSENSUS if consensus is None else consensus,
             "api_report.json": API if api is None else api,
             "eia_actuals.json": EIA if eia is None else eia}
    for name, content in files.items():
        (tmp_path / name).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return tmp_path


def test_load_inputs_happy_path_and_api_release_is_the_day_before(tmp_path):
    inputs = load_inputs(write(tmp_path), TODAY)
    assert inputs["release_date"] == "30-09-2026" and inputs["crude_change_mb"] == -0.391
    assert inputs["api_crude_mb"] == 1.25 and inputs["refinery_util_change_pct"] == -2.8


@pytest.mark.parametrize("which,content,title", [
    ("consensus", "{not json", "Invalid JSON"),
    ("api", "[1, 2]", "Invalid JSON"),
    ("eia", {"crude_change_mb": 1}, "release_date missing"),
    ("eia", {"release_date": "2026-09-30", "crude_change_mb": 1}, "release_date invalid"),
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
    assert load_inputs(tmp_path, datetime.date(2026, 10, 5), allow_stale=True)["release_date"] == "30-09-2026"


@pytest.mark.parametrize("which,content", [
    ("consensus", {"release_date": "23-09-2026", "crude_consensus_mb": -1.6}),   # last week's consensus
    ("api", {"release_date": "22-09-2026", "api_crude_mb": 1.25}),               # last week's API report
    ("api", {"release_date": "01-10-2026", "api_crude_mb": 1.25}),               # API AFTER the EIA release
])
def test_release_date_cross_validation(tmp_path, which, content):
    with pytest.raises(InputError) as err:
        load_inputs(write(tmp_path, **{which: content}), TODAY, allow_stale=True)
    assert err.value.title == "Release date mismatch"


@pytest.mark.parametrize("which,content,field", [
    ("consensus", {"release_date": "30-09-2026", "crude_consensus_mb": None}, "crude_consensus_mb"),
    ("eia", {"release_date": "30-09-2026"}, "crude_change_mb"),
    ("api", {"release_date": "29-09-2026", "api_crude_mb": None}, "api_crude_mb"),
])
def test_mandatory_fields(tmp_path, which, content, field):
    with pytest.raises(InputError, match=field) as err:
        load_inputs(write(tmp_path, **{which: content}), TODAY)
    assert err.value.title == "Mandatory field missing"


def test_optional_fields_may_be_missing(tmp_path):
    bare = write(tmp_path, consensus={"release_date": "30-09-2026", "crude_consensus_mb": -1.6},
                 eia={"release_date": "30-09-2026", "crude_change_mb": -0.391},
                 api={"release_date": "29-09-2026", "api_crude_mb": 1.25})
    inputs = load_inputs(bare, TODAY)
    assert inputs["gasoline_consensus_mb"] is None and inputs["cushing_change_mb"] is None
    assert inputs["api_cushing_mb"] is None and inputs["refinery_util_change_pct"] is None


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


def test_ai_output_cannot_change_the_signal():
    lying = lambda prompt: ("STRONG BUY, grade A, confidence 99", "groq/x")
    honest = build_signal(REFERENCE, 84.0, "fallback", analyse=stub(), generated_at="t")
    lied = build_signal(REFERENCE, 84.0, "fallback", analyse=lying, generated_at="t")
    assert lied["signal"] == honest["signal"] and lied["calculations"] == honest["calculations"]


# -------------------------------------------------------------------------- main

def patch_main(monkeypatch, tmp_path):
    for const, name in (("CONSENSUS_FILE", "consensus.json"), ("API_REPORT_FILE", "api_report.json"),
                        ("EIA_ACTUALS_FILE", "eia_actuals.json"), ("SIGNAL_FILE", "signal.json")):
        monkeypatch.setattr(se, const, tmp_path / name)
    monkeypatch.setattr(se, "DATA_DIR", tmp_path)
    monkeypatch.setattr(se, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    fake_yfinance(monkeypatch, price=84.0)
    alerts = []
    monkeypatch.setattr(se, "send_error", lambda script, msg: alerts.append((script, msg)))
    return alerts


def test_main_writes_signal_json_and_sends_nothing_on_success(monkeypatch, tmp_path):
    alerts = patch_main(monkeypatch, tmp_path)
    write(tmp_path)
    assert se.main(["--allow-stale"]) == 0
    saved = json.loads((tmp_path / "signal.json").read_text(encoding="utf-8"))
    assert saved["signal"]["grade"] == "B" and saved["model_used"] == "rule_based" and saved["analysis"] == ""
    assert alerts == []


def test_main_input_error_alerts_telegram_and_writes_nothing(monkeypatch, tmp_path):
    alerts = patch_main(monkeypatch, tmp_path)
    write(tmp_path, eia={"release_date": "30-09-2026"})
    assert se.main(["--allow-stale"]) == 1
    assert not (tmp_path / "signal.json").exists()
    assert alerts and "Mandatory field missing" in alerts[0][1] and "crude_change_mb" in alerts[0][1]


def test_main_unexpected_error_alerts_telegram(monkeypatch, tmp_path):
    alerts = patch_main(monkeypatch, tmp_path)
    write(tmp_path)
    monkeypatch.setattr(se, "build_signal", lambda *a, **k: 1 / 0)
    assert se.main(["--allow-stale"]) == 1
    assert alerts and "ZeroDivisionError" in alerts[0][1]
