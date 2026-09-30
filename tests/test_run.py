import sys
from types import SimpleNamespace

import pytest

from app import run


def names(stages):
    return [m for m, _ in stages]


def test_the_live_pre_phase_is_consensus_api_market_then_the_brief():
    stages = run.plan("pre")
    assert names(stages) == ["consensus_fetcher", "api_monitor", "market_data", "pre_brief"]
    assert dict(stages)["api_monitor"] == ["--once"]                   # fail fast: never sit polling in the pre phase


def test_the_live_print_phase_is_actuals_signal_alert_with_no_replay_flags():
    stages = run.plan("print")
    assert names(stages) == ["eia_actuals", "signal_engine", "telegram_bot"]
    assert all(argv == [] for _, argv in stages)                       # today's report; nothing is marked stale


def test_all_is_pre_then_print_in_order():
    assert run.plan("all") == run.plan("pre") + run.plan("print")


def test_a_replay_passes_the_release_date_the_tuesday_and_the_stale_flags():
    stages = dict(run.plan("all", "23-09-2026"))
    assert stages["consensus_fetcher"] == ["--date", "23-09-2026"]
    assert stages["api_monitor"] == ["--date", "22-09-2026", "--once"]        # the report is the day before
    assert stages["market_data"] == ["--date", "23-09-2026"]
    assert stages["pre_brief"] == ["--allow-stale", "--print"]                # printed, never sent for an old week
    assert stages["eia_actuals"] == ["--date", "23-09-2026", "--once"]
    assert stages["signal_engine"] == ["--allow-stale"] and stages["telegram_bot"] == ["--allow-stale"]


def test_a_replay_date_across_a_month_boundary():
    assert dict(run.plan("pre", "01-10-2026"))["api_monitor"] == ["--date", "30-09-2026", "--once"]


def test_a_bad_replay_date_is_an_error():
    with pytest.raises(ValueError):
        run.plan("pre", "2026-09-23")


class Runner:
    def __init__(self, codes=None):
        self.calls, self.codes = [], codes or {}

    def __call__(self, command, cwd=None, env=None):
        self.calls.append(command)
        module = command[2].split(".")[-1]
        assert env["PYTHONIOENCODING"] == "utf-8"
        return SimpleNamespace(returncode=self.codes.get(module, 0))


def test_every_stage_runs_as_its_own_module_process_in_order():
    runner = Runner()
    assert run.run_stages(run.plan("print"), runner=runner) == 0
    assert [c[:3] for c in runner.calls] == [[sys.executable, "-m", f"app.{m}"] for m in
                                              ("eia_actuals", "signal_engine", "telegram_bot")]


def test_the_first_failure_stops_the_chain_and_alerts_with_what_was_skipped():
    runner, alerts = Runner({"signal_engine": 1}), []
    code = run.run_stages(run.plan("print"), runner=runner, alert=lambda script, msg: alerts.append((script, msg)))
    assert code == 1 and len(runner.calls) == 2                          # the Telegram alert stage never ran
    assert alerts == [("run.py", "stage signal_engine exited 1; skipped: telegram_bot")]


def test_a_failure_in_the_last_stage_says_nothing_was_skipped():
    alerts = []
    run.run_stages(run.plan("print"), runner=Runner({"telegram_bot": 3}), alert=lambda s, m: alerts.append(m))
    assert alerts == ["stage telegram_bot exited 3; skipped: nothing"]


def test_the_exit_code_of_the_failing_stage_is_returned():
    assert run.run_stages(run.plan("pre"), runner=Runner({"market_data": 7}), alert=lambda s, m: None) == 7


def test_dry_run_runs_nothing():
    runner = Runner()
    assert run.run_stages(run.plan("all"), runner=runner, dry_run=True) == 0 and runner.calls == []


def test_main_wires_watch_only_after_the_print_phase(monkeypatch):
    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    runner = Runner()
    assert run.main(["print", "--watch"], runner=runner) == 0
    assert runner.calls[-1][2] == "app.watch" and runner.calls[-1][3:] == []
    runner = Runner()
    assert run.main(["pre", "--watch"], runner=runner) == 0
    assert "app.watch" not in [c[2] for c in runner.calls]              # nothing to watch before the print


def test_a_replay_watch_replays_too(monkeypatch):
    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    runner = Runner()
    run.main(["all", "--replay", "23-09-2026", "--watch"], runner=runner)
    assert runner.calls[-1][2:] == ["app.watch", "--allow-stale"]


def test_main_rejects_a_bad_replay_date_as_an_argument_error(monkeypatch):
    monkeypatch.setattr(run, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        run.main(["all", "--replay", "yesterday"], runner=Runner())
