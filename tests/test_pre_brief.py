import datetime
import json

import pytest

from app import pre_brief as pb
from app.signal_engine import InputError

CONSENSUS = {"release_date": "30-09-2026", "crude_consensus_mb": -1.6, "gasoline_consensus_mb": -1.4,
             "distillate_consensus_mb": -0.7}
API = {"release_date": "29-09-2026", "api_crude_mb": 1.019}
MARKET = {"wti": 89.87, "ovx": 53.74, "atr_20": 4.936, "cl1_cl2": 2.21, "crack_321": 62.22, "brent_wti": 6.61,
          "overnight_rally_usd": 0.76, "usd_inr_trend_pct": 0.3, "fetched_at": "30-09-2026 11:00"}
DAY = datetime.date(2026, 9, 30)


def brief(**overrides):
    args = dict(consensus=CONSENSUS, api=API, market=MARKET, sigma=4.56, weeks=9, method="mad", release_day=DAY)
    args.update(overrides)
    return pb.build_brief(**args)


def test_the_brief_says_how_big_the_surprise_must_be_before_the_model_trades():
    text = brief()
    tls = 1.25 * 4.56
    assert f"|TLS| must reach {tls:.2f} mb (Z 1.25 x sigma 4.56, mad, 9 weeks)" in text
    assert f"a crude build past {-1.6 + tls:+.2f} mb or a draw past {-1.6 - tls:+.2f} mb" in text
    assert "gasoline weight 0.80, distillate 0.50" in text                 # September weights


def test_the_brief_carries_the_schedule_the_option_and_the_market():
    text = brief()
    for expected in (
        "PRE-BRIEF - EIA report 30-09-2026",
        "Print 20:00 IST (10:30 ET) | time stop 20:35 | hard exit 22:30 | MCX close 23:30",
        "Consensus: crude -1.600 mb | gasoline -1.400 mb | distillate -0.700 mb",
        "API (29-09-2026): crude +1.019 mb = +2.619 mb vs consensus",
        "WTI 89.87 | OVX 53.7 -> delta 0.80-0.85 (above 35: deep ITM) | ATR20 4.94",
        "Option: expiry 15-10-2026 (15d)",
        "Scorecard: CL1-CL2 +2.21 (>0.3: yes) | 3:2:1 crack +62.22 (>22.0: yes) | Brent-WTI +6.61 (>5.5: yes)",
        "USD/INR +0.30% over 5 sessions",
    ):
        assert expected in text, expected
    assert "PRE-POSITIONED" not in text and "ARMED" not in text and "CLOSED" not in text


def test_an_api_move_beyond_three_mb_is_flagged_and_arms_regime_3_only_with_a_rally():
    big_draw = {**API, "api_crude_mb": -5.0}                               # -3.4 mb vs consensus
    assert "PRE-POSITIONED (beyond 3.0 mb)" in brief(api=big_draw)
    assert "ARMED" not in brief(api=big_draw)                              # rally 0.76: not enough
    assert "ARMED" in brief(api=big_draw, market={**MARKET, "overnight_rally_usd": 1.4})
    assert "ARMED" not in brief(api={**API, "api_crude_mb": 5.0}, market={**MARKET, "overnight_rally_usd": 1.4})  # a build


def test_missing_scorecard_values_read_na_not_a_crash():
    text = brief(market={**MARKET, "cl1_cl2": None, "overnight_rally_usd": None, "usd_inr_trend_pct": None})
    assert "CL1-CL2 N/A (>0.3: n/a)" in text and "Overnight rally (API to now): N/A" in text
    assert "USD/INR" not in text


def test_a_closed_evening_session_leads_the_message():
    text = brief(evening=(False, "Republic Day"), release_day=datetime.date(2026, 1, 26))
    assert "!! MCX EVENING SESSION CLOSED on 30-09-2026 (Republic Day)" in text


def test_an_assumed_expiry_is_marked():
    assert "ASSUMED - check the chain" in brief(release_day=datetime.date(2027, 1, 6))


# --------------------------------------------------------------------------------- loading and main

def write(tmp_path, consensus=CONSENSUS, api=API, market=MARKET):
    for name, content in (("consensus.json", consensus), ("api_report.json", api), ("market.json", market)):
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    return tmp_path


TODAY = datetime.date(2026, 9, 30)


def test_load_returns_validated_dicts(tmp_path):
    consensus, api, market = pb.load_brief_inputs(write(tmp_path), TODAY)
    assert consensus["crude_consensus_mb"] == -1.6 and api["api_crude_mb"] == 1.019 and market["ovx"] == 53.74


@pytest.mark.parametrize("kwargs,title", [
    (dict(consensus={**CONSENSUS, "release_date": "16-09-2026"}), "Stale data"),
    (dict(api={**API, "release_date": "01-09-2026"}), "Stale data"),
    (dict(api={**API, "release_date": "01-10-2026"}), "Release date mismatch"),
    (dict(api={"release_date": "29-09-2026"}), "Mandatory field missing"),
])
def test_bad_inputs_are_refused(tmp_path, kwargs, title):
    with pytest.raises(InputError) as err:
        pb.load_brief_inputs(write(tmp_path, **kwargs), TODAY)
    assert err.value.title == title


def arm(monkeypatch, tmp_path):
    monkeypatch.setattr(pb, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(pb, "load_brief_inputs", lambda **kw: (CONSENSUS, API, MARKET))
    monkeypatch.setattr(pb, "load_sigma", lambda release_date, method="mad": (4.56, 9))
    monkeypatch.setattr(pb, "sigma_method", lambda: "mad")
    sent, errors = [], []
    monkeypatch.setattr(pb, "send_message", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pb, "send_error", lambda script, msg: errors.append((script, msg)))
    return sent, errors


def test_main_sends_the_brief(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path)
    assert pb.main([]) == 0 and len(sent) == 1 and "PRE-BRIEF" in sent[0] and errors == []


def test_main_print_sends_nothing(monkeypatch, tmp_path, capsys):
    sent, _ = arm(monkeypatch, tmp_path)
    assert pb.main(["--print"]) == 0 and sent == []
    assert "PRE-BRIEF" in capsys.readouterr().out


def test_main_alerts_when_an_input_is_bad(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path)

    def bad(**kw):
        raise InputError("Stale data", "consensus is old")
    monkeypatch.setattr(pb, "load_brief_inputs", bad)
    assert pb.main([]) == 1 and sent == [] and "Stale data" in errors[0][1]
