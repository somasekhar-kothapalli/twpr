"""Tests for signal_engine.main()'s input handling — the file-reading layer.

The rule engine itself is covered by test_signal_engine.py. These cover the way
main() assembles its three inputs, where a stale file can silently produce a
plausible but wrong signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import signal_engine as se  # noqa: E402

WEEK = "2026-09-04"

EIA = {
    "week_ending": WEEK,
    "crude_change_mb": -0.391,
    "cushing_stocks_mb": -0.684,
    "gasoline_change_mb": 2.669,
    "distillate_change_mb": 2.787,
}
CONSENSUS = {
    "week_ending": WEEK,
    "crude_consensus_mb": -1.6,
    "gasoline_consensus_mb": 0.0,
    "distillate_consensus_mb": 0.0,
    "crude_previous_mb": -2.0,
}
API_REPORT = {"week_ending": WEEK, "api_crude_mb": 1.25}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Point signal_engine at a temp data dir and stub out the side effects."""
    written = {}

    monkeypatch.setattr(se, "CONSENSUS_FILE", tmp_path / "consensus.json")
    monkeypatch.setattr(se, "EIA_ACTUAL_FILE", tmp_path / "eia_actual.json")
    monkeypatch.setattr(se, "API_REPORT_FILE", tmp_path / "api_report.json")
    monkeypatch.setattr(se, "SIGNAL_FILE", tmp_path / "signal.json")
    monkeypatch.setattr(se, "write_json", lambda path, payload: written.update(payload))
    monkeypatch.setattr(se.PetroCoreClient, "post_signal", lambda self, payload: True)
    monkeypatch.setattr(se, "send_error", lambda name, exc: False)
    monkeypatch.setenv("MODEL_MODE", "rule_based")

    def run(consensus=CONSENSUS, eia=EIA, api_report=API_REPORT):
        import json

        for name, payload in (
            ("consensus.json", consensus),
            ("eia_actual.json", eia),
            ("api_report.json", api_report),
        ):
            if payload is not None:
                (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
        written.clear()
        return se.main(), written

    return run


def test_reference_week_end_to_end(pipeline):
    """The committed reference week, through main() rather than the engine alone."""
    code, signal = pipeline()
    assert code == 0
    assert signal["grade"] == "B"
    assert signal["direction"] == "bearish"
    assert signal["confidence"] == 55
    assert signal["crude_deviation_mb"] == pytest.approx(1.209, abs=1e-9)
    assert signal["week_ending"] == WEEK


def test_stale_consensus_is_refused_not_mixed(pipeline):
    """A consensus from another week must never be subtracted from these actuals.

    Regression: consensus for 2026-09-18 (-0.6) against 2026-09-04 actuals
    (-0.391) gives +0.209 — inside the skip zone — so a Grade B bearish trade
    silently became "no trade".
    """
    stale = {**CONSENSUS, "week_ending": "2026-09-18", "crude_consensus_mb": -0.6}
    code, signal = pipeline(consensus=stale)
    assert code == 1  # refused
    assert signal == {}  # nothing written


def test_wrong_week_api_report_is_ignored(pipeline):
    """An API report from another week must not move confidence."""
    code, signal = pipeline(api_report={**API_REPORT, "week_ending": "2026-08-28"})
    assert code == 0
    assert signal["grade"] == "B"
    # api_crude_mb falls back to 0.0, which counts as contradicting: 55 -> 45.
    assert signal["confidence"] == 45
    assert signal["api_aligns"] is False


def test_missing_api_report_still_trades(pipeline):
    """The API report is optional — a missing one costs 5 confidence, not the week."""
    code, signal = pipeline(api_report=None)
    assert code == 0
    assert signal["grade"] == "B"
    assert signal["confidence"] == 45


def test_missing_consensus_fails_loudly(pipeline):
    code, signal = pipeline(consensus=None)
    assert code == 1
    assert signal == {}


def test_missing_eia_actuals_fails_loudly(pipeline):
    code, signal = pipeline(eia=None)
    assert code == 1
    assert signal == {}
