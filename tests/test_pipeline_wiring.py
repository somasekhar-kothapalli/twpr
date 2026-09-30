"""Cross-script wiring: the data contract lives in one place and every stage alerts on failure."""
import ast
import logging
import pathlib

import httpx
import pytest

from app import api_monitor, consensus_fetcher, eia_actuals, signal_engine
from app.utils import common, telegram

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
DATA_FILES = ("consensus.json", "api_report.json", "eia_actuals.json", "market.json", "surprise_history.json", "signal.json")


# ----------------------------------------------------------- one home for file names

def _code_literals(path):
    """String literals used in code (not docstrings), so prose may still mention a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_data_file_names_are_spelled_only_in_common():
    """A rename (eia_actual -> eia_actuals happened) must not leave a stage on the old name."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "common.py" and path.parent.name == "utils":
            continue
        offenders += [f"{path.relative_to(APP)}: {lit!r}" for lit in _code_literals(path)
                      if any(name in lit for name in DATA_FILES)]
    assert offenders == []


def test_each_producer_writes_the_file_the_engine_reads():
    assert consensus_fetcher.CONSENSUS_FILE is common.CONSENSUS_FILE is signal_engine.CONSENSUS_FILE
    assert api_monitor.API_REPORT_FILE is common.API_REPORT_FILE is signal_engine.API_REPORT_FILE
    assert eia_actuals.EIA_ACTUALS_FILE is common.EIA_ACTUALS_FILE is signal_engine.EIA_ACTUALS_FILE
    assert signal_engine.SIGNAL_FILE is common.SIGNAL_FILE


def test_data_files_live_in_the_repo_data_dir():
    for path in (common.CONSENSUS_FILE, common.API_REPORT_FILE, common.EIA_ACTUALS_FILE, common.MARKET_FILE,
                 common.SURPRISE_HISTORY_FILE, common.SIGNAL_FILE):
        assert path.parent == common.DATA_DIR and path.name in DATA_FILES


# ------------------------------------------------------- every stage alerts on failure

CONSENSUS_OK = {"release_date": "30-09-2026", "crude_consensus_mb": -1.6, "gasoline_consensus_mb": -1.4,
                "distillate_consensus_mb": -0.7, "crude_previous_mb": 2.415, "crude_source": "tradingeconomics",
                "gasoline_source": "tradingeconomics", "distillate_source": "tradingeconomics"}
API_OK = {"release_date": "29-09-2026", "api_crude_mb": 1.25, "api_cushing_mb": -0.684,
          "api_gasoline_mb": 1.0, "api_distillate_mb": 2.0}
EIA_OK = {"release_date": "30-09-2026", "crude_change_mb": -0.391, "cushing_change_mb": -0.684,
          "gasoline_change_mb": 1.269, "distillate_change_mb": 2.087, "refinery_util_change_pct": -2.8,
          "source": "tradingeconomics", "won_race": True}

SCRIPTS = [
    pytest.param(consensus_fetcher, "fetch_consensus", CONSENSUS_OK, [], id="consensus_fetcher"),
    pytest.param(api_monitor, "fetch_api_report", API_OK, ["--once"], id="api_monitor"),
    pytest.param(eia_actuals, "fetch_eia_actuals", EIA_OK, ["--once"], id="eia_actuals"),
]


def arm(monkeypatch, module, fetch_name, result, error=None):
    alerts, writes = [], []
    monkeypatch.setattr(module, "send_exception", lambda script, exc: alerts.append((script, exc)))
    monkeypatch.setattr(module, "write_json", lambda path, payload: writes.append((path, payload)))
    if hasattr(module, "cushing_level"):   # eia_actuals looks the level up on eia.gov
        monkeypatch.setattr(module, "cushing_level", lambda change: 23.748)

    def fetch(*args, **kwargs):
        if error:
            raise error
        return dict(result)
    monkeypatch.setattr(module, fetch_name, fetch)
    return alerts, writes


@pytest.mark.parametrize("module,fetch_name,result,argv", SCRIPTS)
def test_failure_exits_1_alerts_telegram_and_writes_nothing(monkeypatch, module, fetch_name, result, argv):
    boom = RuntimeError("no valid values (tradingeconomics: consensus not posted yet)")
    alerts, writes = arm(monkeypatch, module, fetch_name, result, error=boom)
    assert module.main(argv) == 1
    assert alerts == [(module.__name__.split(".")[-1] + ".py", boom)]
    assert writes == []


@pytest.mark.parametrize("module,fetch_name,result,argv", SCRIPTS)
def test_success_writes_and_does_not_alert(monkeypatch, module, fetch_name, result, argv):
    alerts, writes = arm(monkeypatch, module, fetch_name, result)
    assert module.main(argv) == 0
    assert alerts == [] and len(writes) == 1
    assert writes[0][1]["release_date"] == result["release_date"] and "fetched_at" in writes[0][1]


# ------------------------------------------------------------------ telegram module

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def capture_post(monkeypatch, response=None, error=None):
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        if error:
            raise error
        return response or httpx.Response(200)
    monkeypatch.setattr(telegram.httpx, "post", post)
    return sent


def test_send_exception_names_the_script_and_truncates(monkeypatch, configured):
    sent = capture_post(monkeypatch)
    assert telegram.send_exception("eia_actuals.py", RuntimeError("x" * 5000)) is True
    text = sent[0][1]["text"]
    assert text.startswith("⚠️ TWPR ERROR\neia_actuals.py\nRuntimeError: xxx")
    assert len(text) < 1600 and sent[0][1]["chat_id"] == "42"


def test_unconfigured_telegram_returns_false_and_never_raises(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "# fill me in")  # a dotenv placeholder counts as unset
    sent = capture_post(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert telegram.send_exception("api_monitor.py", ValueError("boom")) is False
    assert sent == [] and "ValueError: boom" in caplog.text     # the alert text is still logged


@pytest.mark.parametrize("kwargs", [{"error": httpx.ConnectError("down")}, {"response": httpx.Response(500)}])
def test_a_failing_send_returns_false_and_never_leaks_the_token(monkeypatch, configured, caplog, kwargs):
    capture_post(monkeypatch, **kwargs)
    with caplog.at_level(logging.DEBUG):
        assert telegram.send_message("hi") is False
    assert "SECRET-TOKEN-123" not in caplog.text


def test_setup_logging_silences_httpx_so_the_token_url_is_not_logged():
    common.setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
