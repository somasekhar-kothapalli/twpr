"""The TWPR signal engine: the WPSR runbook model -> data/signal.json.

Reads data/consensus.json + api_report.json + eia_actuals.json + market.json +
surprise_history.json. No scraping. Runs right after eia_actuals.py (Wednesday, ~2 min
after the print). Telegram is used for ERRORS only here; telegram_bot.py sends the signal.

The model (docs/WPSR_WEDNESDAY_RUNBOOK.md section 3 and the MCX options adaptation):
TLS -> Z-score gate (|Z| >= 1.25) -> Cushing check (contradiction routes to Regime 2)
-> regime -> expected move -> ITM option, expiry gate and the INR risk of your lot count.
The maths is in app/model.py and app/options.py (pure); this file loads, validates and
assembles. The Groq narrative only writes prose: it can never touch the trade, and if it
fails the signal still ships with analysis "".

Chart-side steps cannot be computed from data and are listed in signal["checklist"]:
the time-spread and gamma filters, the entry retest, the real stop distance, FX/circuit
aborts. The lot table is therefore given per futures stop, not as one number.

Run from the repo root:
    python -m app.signal_engine                  # today's data
    python -m app.signal_engine --allow-stale    # replay data older than 2 days
"""
import argparse
import json
import logging
import re
import sys
import threading

import httpx

from dotenv import load_dotenv

from app import currency, model, ng_options, options
from app.surprise_history import load_history, record_week
from app.utils.common import (API_REPORT_FILE, CONSENSUS_FILE, DATA_DIR, EIA_ACTUALS_FILE, MARKET_FILE,
                              ROOT, SIGNAL_FILE, SURPRISE_HISTORY_FILE, env, fmt_ts, is_stale, now_ist,
                              parse_release_date, setup_logging, write_json)
from app.utils.telegram import send_error

logger = logging.getLogger("twpr.signal_engine")

STALE_DAYS = 2             # consensus / EIA / market data older than this is refused
API_MAX_LEAD_DAYS = 3      # the API report is released 1-2 days BEFORE the EIA report
FREECURRENCY_URL = "https://api.freecurrencyapi.com/v1/latest"
USD_INR_PLAUSIBLE = (50.0, 150.0)  # a quote outside this is bad data, not a rate
USD_INR_TIMEOUT_S = 10
GROQ_TIMEOUT_S = 15.0
ANALYSIS_MAX_CHARS = 450   # 3 sentences / 70 words; replies run ~230-300 chars
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")  # a "." inside "+3.569 mb" is not a sentence end
GROQ_SYSTEM_PROMPT = (
    "You are a crude oil options trading analyst.\n"
    "Write exactly 3 sentences.\n"
    "Sentence 1: What the total liquid surprise means for immediate WTI direction.\n"
    "Sentence 2: The most important confirming or contradicting factor.\n"
    "Sentence 3: One key risk to the signal.\n"
    "Be direct. Use only the numbers given. No disclaimers. Max 70 words total."
)

SCORECARD_THRESHOLDS = {"backwardation_cl1_cl2": 0.30, "crack_321": 22.0, "brent_wti": 5.50}

ALWAYS_CHECK = [
    "Limit orders only on the option chain, never a market order. A missed fill is not a loss.",
    "Fair value: MCX futures should sit near WTI x USD/INR (the futures level in the strike guide). If they are "
    "far apart, or the rupee is gapping, do not trade: the price you would pay is not the WTI move the model measured.",
    "Geopolitical tension elevated? Size down 50% (circuit-limit risk).",
    "Price within $0.15 of a heavy-OI strike (weekly pinning)? Cut the expected move by 40%.",
    "Stop: 1.5 x 1-min ATR or outside VWAP +/-1.5 sigma, whichever is wider (min $0.18-$0.35); "
    "exit the OPTION when its own premium hits the delta-adjusted stop.",
    "Scale out 50% on the first clean thrust, stop to break-even on the rest.",
    "Check the option's bid/ask BEFORE the print. If the spread eats a large share of the expected move, "
    "skip - a wide spread on a deep-ITM strike is a cost paid on entry and again on exit.",
    "The lot table assumes the option loses delta x the futures stop. It ignores the IV crush after the "
    "print (vega): size below the table until you have measured real fills.",
]
ATR_STOP_MULTIPLE = 1.5        # runbook stop: 1.5 x the 1-minute ATR
DEEP_STRIKE_CHECK = ("Deep-ITM strike (delta 0.80-0.85): the books there can be thin. If its bid/ask spread is "
                     "wide, step down to the 0.65-0.70 delta strike rather than paying it; the journal slippage "
                     "will show what 'wide' costs.")
TIME_SPREAD_CHECK = ("Bullish only: a flat-price rally of $0.40 or more needs CL1-CL2 to widen $0.02-$0.04, "
                     "otherwise reject the long.")
REGIME_CHECK = {
    1: ["Regime 1: enter on a limit retest of the broken pre-release boundary (P_high long / P_low short), "
        "aligned CVD. No retest = no trade."],
    2: ["Regime 2 (FADE, lower conviction): do NOT trade the initial spike. Wait for a stall at prior daily "
        "high/low or Value Area, then a 1-min close back inside the pre-release range. "
        "Targets: opposite side of the range, then session POC. Size down without a clean confirmation candle."],
    3: ["Regime 3 (sell the fact): the overnight rally is confirmed (see calculations.overnight_rally_usd); "
        "short only on a cross below the 09:00 ET cash-open VWAP."],
}


# ------------------------------------------------------------------------ inputs

class InputError(Exception):
    """An input problem that stops the run; `title` heads the Telegram alert."""

    def __init__(self, title, detail):
        super().__init__(f"{title}: {detail}")
        self.title, self.detail = title, detail


def _load(path, label):
    if not path.exists():
        raise InputError("Input file missing", f"{label} ({path.name}) not found - did its script run?")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError("Invalid JSON", f"{label} ({path.name}): {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("Invalid JSON", f"{label} ({path.name}) is not a JSON object")
    return data


def _load_dated(path, label):
    data = _load(path, label)
    if not data.get("release_date"):
        raise InputError("release_date missing", f"{label} ({path.name}) has no release_date")
    try:
        parse_release_date(data["release_date"])
    except ValueError as exc:
        raise InputError("release_date invalid", f"{label} ({path.name}): {data['release_date']!r} is not DD-MM-YYYY") from exc
    return data


def _num(data, key):
    value = data.get(key)
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        raise InputError("Invalid value", f"{key}={value!r} is not a number") from None


MANDATORY = (("crude_consensus_mb", CONSENSUS_FILE), ("gasoline_consensus_mb", CONSENSUS_FILE),
             ("distillate_consensus_mb", CONSENSUS_FILE), ("crude_change_mb", EIA_ACTUALS_FILE),
             ("cushing_change_mb", EIA_ACTUALS_FILE), ("gasoline_change_mb", EIA_ACTUALS_FILE),
             ("distillate_change_mb", EIA_ACTUALS_FILE),
             ("api_crude_mb", API_REPORT_FILE))


def load_inputs(data_dir=None, today=None, allow_stale=False):
    """The flat inputs dict (spec key names) plus `release_date`, or raise InputError."""
    data_dir = data_dir or DATA_DIR
    consensus = _load_dated(data_dir / CONSENSUS_FILE.name, "consensus")
    api = _load_dated(data_dir / API_REPORT_FILE.name, "API report")
    eia = _load_dated(data_dir / EIA_ACTUALS_FILE.name, "EIA actuals")

    if not allow_stale:
        for label, data, limit in (("consensus", consensus, STALE_DAYS), ("EIA actuals", eia, STALE_DAYS),
                                   ("API report", api, STALE_DAYS + API_MAX_LEAD_DAYS)):
            if is_stale(data["release_date"], limit, today):
                raise InputError("Stale data", f"{label} is for {data['release_date']}, more than {limit} days old "
                                 "(use --allow-stale to replay)")

    # Consensus and EIA describe the same Wednesday report. The API report is
    # published the day BEFORE it (Tuesday), so its date is earlier, never equal.
    if consensus["release_date"] != eia["release_date"]:
        raise InputError("Release date mismatch", f"consensus {consensus['release_date']} vs "
                         f"EIA actuals {eia['release_date']}")
    lead = (parse_release_date(eia["release_date"]) - parse_release_date(api["release_date"])).days
    if not 0 <= lead <= API_MAX_LEAD_DAYS:
        raise InputError("Release date mismatch", f"API report {api['release_date']} is not the report released "
                         f"0-{API_MAX_LEAD_DAYS} days before EIA {eia['release_date']}")

    inputs = {
        "release_date": eia["release_date"],
        "crude_consensus_mb": _num(consensus, "crude_consensus_mb"),
        "gasoline_consensus_mb": _num(consensus, "gasoline_consensus_mb"),
        "distillate_consensus_mb": _num(consensus, "distillate_consensus_mb"),
        "crude_change_mb": _num(eia, "crude_change_mb"),
        "cushing_change_mb": _num(eia, "cushing_change_mb"),
        "cushing_level_mb": _num(eia, "cushing_level_mb"),
        "gasoline_change_mb": _num(eia, "gasoline_change_mb"),
        "distillate_change_mb": _num(eia, "distillate_change_mb"),
        "refinery_util_change_pct": _num(eia, "refinery_util_change_pct"),
        "api_crude_mb": _num(api, "api_crude_mb"),
    }
    missing = [f"{key} ({path.name})" for key, path in MANDATORY if inputs[key] is None]
    if missing:
        raise InputError("Mandatory field missing", ", ".join(missing))
    return inputs


def load_market(data_dir=None, today=None, allow_stale=False):
    """market.json as a dict with atr_20 and ovx guaranteed, or raise InputError."""
    data_dir = data_dir or DATA_DIR
    market = _load(data_dir / MARKET_FILE.name, "market data")
    missing = [key for key in ("atr_20", "ovx") if market.get(key) is None]
    if missing:
        raise InputError("Mandatory field missing", f"{', '.join(missing)} ({MARKET_FILE.name})")
    fetched = str(market.get("fetched_at", "")).split(" ")[0]
    try:
        stale = is_stale(fetched, STALE_DAYS, today)
    except ValueError as exc:
        raise InputError("fetched_at invalid", f"market data ({MARKET_FILE.name}): {market.get('fetched_at')!r}") from exc
    if stale and not allow_stale:
        raise InputError("Stale data", f"market data was fetched {market['fetched_at']}, more than {STALE_DAYS} days "
                         "old - run python -m app.market_data (or --allow-stale to replay)")
    return market


LOT_SIZE_SETTINGS = {"CRUDEOIL": "MCX_CRUDEOIL_LOT_SIZE", "CRUDEOILM": "MCX_CRUDEOILM_LOT_SIZE",
                     "NATURALGAS": "MCX_NATURALGAS_LOT_SIZE", "NATURALGASM": "MCX_NATURALGASM_LOT_SIZE"}
LOT_COUNT_SETTINGS = {"CRUDEOIL": "MCX_CRUDEOIL_LOTS", "CRUDEOILM": "MCX_CRUDEOILM_LOTS"}
# Natural gas is record-only (docs/V0_2_SCOPE.md): its lot counts are validated but kept apart, so the crude
# sizing never sees them.
NG_LOT_COUNT_SETTINGS = {"NATURALGAS": "MCX_NATURALGAS_LOTS", "NATURALGASM": "MCX_NATURALGASM_LOTS"}


def _whole_number(name, what):
    """The setting as an int of at least 1, None if unset; InputError otherwise."""
    raw = env(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise InputError("Invalid setting", f"{name}={raw!r} is not a whole number of {what}") from None
    if value < 1:
        raise InputError("Invalid setting", f"{name} must be at least 1, got {raw!r}")
    return value


def load_lot_sizes():
    """{contract: barrels (or mmBtu) per lot or None}: the exchange's contract sizes, from MCX_CRUDEOIL_LOT_SIZE
    (100 bbl), MCX_CRUDEOILM_LOT_SIZE (10 bbl), MCX_NATURALGAS_LOT_SIZE (1250 MMBtu) and MCX_NATURALGASM_LOT_SIZE
    (250 MMBtu; natural gas is record-only, nothing reads them). A value that is not the contract size MCX publishes is refused: the usual
    cause is putting the NUMBER OF LOTS here (that goes in MCX_CRUDEOIL_LOTS / MCX_CRUDEOILM_LOTS)."""
    sizes = {contract: _whole_number(name, "barrels") for contract, name in LOT_SIZE_SETTINGS.items()}
    for contract, size in sizes.items():
        expected = options.CONTRACT_BARRELS.get(contract) or ng_options.CONTRACT_MMBTU.get(contract)
        if size is not None and expected is not None and size != expected:
            unit = "barrels" if contract in options.CONTRACT_BARRELS else "MMBtu"
            counts = {**LOT_COUNT_SETTINGS, **NG_LOT_COUNT_SETTINGS}
            raise InputError("Invalid setting", f"{LOT_SIZE_SETTINGS[contract]}={size}: MCX's {contract} lot is {expected} "
                             f"{unit}. To set how many lots you trade, use {counts[contract]}.")
    return sizes


def load_lot_counts():
    """{contract: lots or None}: how many lots you trade per signal, from MCX_CRUDEOIL_LOTS and
    MCX_CRUDEOILM_LOTS. Whole numbers of at least 1."""
    return {contract: _whole_number(name, "lots") for contract, name in LOT_COUNT_SETTINGS.items()}


def load_ng_lot_counts():
    """{contract: lots or None} from MCX_NATURALGAS_LOTS and MCX_NATURALGASM_LOTS. Nothing uses them yet."""
    return {contract: _whole_number(name, "lots") for contract, name in NG_LOT_COUNT_SETTINGS.items()}


def sigma_method():
    """SIGMA_METHOD from .env: "mad" (default, robust) or "std" (plain std dev)."""
    method = (env("SIGMA_METHOD", "mad") or "mad").lower()
    if method not in model.SIGMA_METHODS:
        raise InputError("Invalid setting", f"SIGMA_METHOD={method!r} must be one of {model.SIGMA_METHODS}")
    return method


def load_center(release_date, data_dir=None):
    """The median weekly TLS of the last 12 weeks (excluding the week traded), or None if there is too little
    history. Informational: the demeaned Z shown beside the raw one."""
    data_dir = data_dir or DATA_DIR
    history = [r for r in load_history(data_dir / SURPRISE_HISTORY_FILE.name) if r["release_date"] != release_date]
    try:
        return model.tls_center(history)
    except ValueError:
        return None


def load_sigma(release_date, data_dir=None, method="mad"):
    """(sigma_forecast, weeks used) from the surprise history, excluding the week being traded."""
    data_dir = data_dir or DATA_DIR
    history = [r for r in load_history(data_dir / SURPRISE_HISTORY_FILE.name) if r["release_date"] != release_date]
    try:
        return model.sigma_forecast(history, method=method), min(len(history), model.SIGMA_WEEKS)
    except ValueError as exc:
        raise InputError("Not enough surprise history", str(exc)) from exc


# ------------------------------------------------------------------ external calls

def _usd_inr_from_yfinance():
    result = {}

    def work():
        try:
            import yfinance as yf
            result["rate"] = float(yf.Ticker("INR=X").fast_info["lastPrice"])
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            result["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(USD_INR_TIMEOUT_S)
    if "rate" not in result:
        raise RuntimeError(f"{type(result['error']).__name__}" if "error" in result else "timed out")
    return result["rate"]


def _usd_inr_from_freecurrencyapi(key, get):
    response = get(FREECURRENCY_URL, params={"apikey": key, "base_currency": "USD", "currencies": "INR"},
                   timeout=USD_INR_TIMEOUT_S)
    response.raise_for_status()
    return float(response.json()["data"]["INR"])


def fetch_usd_inr(get=httpx.get):
    """(rate, source): yfinance first, then FreeCurrencyAPI (needs FREECURRENCYAPI_KEY). A quote outside
    50-150 is bad data, not a rate. If neither source delivers, raise InputError: there is deliberately NO
    default rate (the old 84.0 was 12% off the live ~96 and every rupee figure inherits that error).

    Failure text names the exception type and, for HTTP errors, the status code only: the FreeCurrencyAPI
    key travels in the URL, so str(exc) must never reach a log or a Telegram alert."""
    key = env("FREECURRENCYAPI_KEY")
    sources = [("yfinance", _usd_inr_from_yfinance)]
    if key:
        sources.append(("freecurrencyapi", lambda: _usd_inr_from_freecurrencyapi(key, get)))
    problems = [] if key else ["freecurrencyapi: FREECURRENCYAPI_KEY not set"]
    for name, fetch in sources:
        try:
            rate = fetch()
        except httpx.HTTPStatusError as exc:
            problems.append(f"{name}: HTTP {exc.response.status_code}")
            continue
        except Exception as exc:  # noqa: BLE001 - any failure means try the next source
            problems.append(f"{name}: {exc if isinstance(exc, RuntimeError) else type(exc).__name__}")
            continue
        if USD_INR_PLAUSIBLE[0] < rate < USD_INR_PLAUSIBLE[1]:
            logger.info("USD/INR %.2f (%s)", rate, name)
            return rate, name
        problems.append(f"{name}: implausible {rate}")
    for problem in problems:
        logger.warning("USD/INR: %s", problem)
    raise InputError("USD/INR unavailable", "; ".join(problems))


def fmt_optional(value):
    return f"{value:+.3f}" if value is not None else "N/A"


def build_prompt(inputs, calc, signal):
    status = calc["cushing_status"]
    return (
        f"Release: {inputs['release_date']}\n"
        f"Decision: {signal['action']} | Regime: {signal['regime']} | Direction: {signal['direction']}\n"
        f"Total liquid surprise: {calc['tls_mb']:+.3f} mb (Z {calc['z_tls']:+.2f}); "
        f"{'a stock BUILD is bearish for WTI' if calc['tls_mb'] > 0 else 'a stock DRAW is bullish for WTI'}\n"
        f"Crude surprise: {calc['crude_surprise_mb']:+.3f} mb\n"
        f"Gasoline surprise: {calc['gasoline_surprise_mb']:+.3f} mb\n"
        f"Distillate surprise: {calc['distillate_surprise_mb']:+.3f} mb\n"
        f"Cushing change: {fmt_optional(inputs['cushing_change_mb'])} mb ({status})\n"
        f"API crude: {inputs['api_crude_mb']:+.3f} mb\n"
        f"Refinery util change: {fmt_optional(inputs['refinery_util_change_pct'])}%"
    )


def complete_sentences(text):
    """`text` cut back to its last complete sentence (unchanged if it has none)."""
    ends = list(_SENTENCE_END.finditer(text))
    return text[:ends[-1].end()] if ends else text


def generate_analysis(user_prompt):
    """(analysis, model_used) from Groq, or ("", "rule_based") if the key is missing or
    anything fails. The only place a model is involved, and it only writes prose."""
    api_key = env("GROQ_API_KEY")
    if not api_key:
        logger.info("GROQ_API_KEY not set - no narrative")
        return "", "rule_based"
    model_name = env("GROQ_MODEL", "llama-3.3-70b-versatile")
    try:
        from groq import Groq
        response = Groq(api_key=api_key, timeout=GROQ_TIMEOUT_S).chat.completions.create(
            model=model_name, max_tokens=150, temperature=0.3,
            messages=[{"role": "system", "content": GROQ_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
        )
        choice = response.choices[0]
        text = choice.message.content.strip() # type: ignore
        if len(text) > ANALYSIS_MAX_CHARS or getattr(choice, "finish_reason", None) == "length":
            # over the cap, or the model ran out of tokens mid-sentence: never ship half a sentence
            text = complete_sentences(text[:ANALYSIS_MAX_CHARS]).strip()  # type: ignore
    except Exception as exc:  # noqa: BLE001 - the signal ships without a narrative
        logger.warning("Groq failed (%s: %s) - no narrative", type(exc).__name__, exc)
        return "", "rule_based"
    return (text, f"groq/{model_name}") if text else ("", "rule_based")


# ------------------------------------------------------------------------ assembly

def scorecard(market, api_surprise):
    """Pre-release conditioning scorecard: True when the threshold is met, None when unknown.
    Informational - the runbook calls it a filter but does not say how a miss changes the trade."""
    def above(value, threshold):
        return None if value is None else value > threshold
    return {
        "backwardation": above(market.get("cl1_cl2"), SCORECARD_THRESHOLDS["backwardation_cl1_cl2"]),
        "crack_321": above(market.get("crack_321"), SCORECARD_THRESHOLDS["crack_321"]),
        "brent_wti": above(market.get("brent_wti"), SCORECARD_THRESHOLDS["brent_wti"]),
        "api_prepositioned": abs(api_surprise) > model.API_PREPOSITIONED_MB,
    }


def build_signal(inputs, market, sigma, usd_inr, usd_inr_source, lots=None,
                 analyse=generate_analysis, generated_at=None, sigma_method="mad", tls_center=None):
    """The full signal.json payload from validated inputs, market data and sigma_forecast. `tls_center` (the
    median recent TLS) only adds the informational demeaned Z."""
    release_day = parse_release_date(inputs["release_date"])
    month = release_day.month
    surprises = {
        "crude": round(inputs["crude_change_mb"] - inputs["crude_consensus_mb"], 3),
        "gasoline": round(inputs["gasoline_change_mb"] - inputs["gasoline_consensus_mb"], 3),
        "distillate": round(inputs["distillate_change_mb"] - inputs["distillate_consensus_mb"], 3),
    }
    tls = model.tls(surprises["crude"], surprises["gasoline"], surprises["distillate"], month)
    z = model.z_score(tls, sigma)
    contradicts = model.cushing_contradicts(tls, inputs["cushing_change_mb"])
    rally = market.get("overnight_rally_usd")
    multiplier = model.cushing_multiplier(inputs["cushing_level_mb"])
    beta = model.beta_vol(market["atr_20"], market["ovx"])
    api_surprise = round(inputs["api_crude_mb"] - inputs["crude_consensus_mb"], 3)
    regime, direction = model.classify(tls, z, contradicts, inputs["crude_change_mb"],
                                       inputs["crude_consensus_mb"], inputs["api_crude_mb"], rally)
    setup3 = model.regime3_setup(tls, inputs["crude_change_mb"], inputs["crude_consensus_mb"], inputs["api_crude_mb"])

    calc = {
        "crude_surprise_mb": surprises["crude"], "gasoline_surprise_mb": surprises["gasoline"],
        "distillate_surprise_mb": surprises["distillate"],
        "omega_gasoline": model.omega_gasoline(month), "omega_distillate": model.omega_distillate(month),
        "tls_mb": round(tls, 3), "sigma_forecast_mb": round(sigma, 3), "sigma_method": sigma_method, "z_tls": round(z, 2),
        "cushing_contradicts": contradicts, "cushing_status": model.cushing_status(tls, inputs["cushing_change_mb"]),
        "cushing_multiplier": round(multiplier, 3),
        "regime3_setup": setup3, "overnight_rally_usd": rally,
        "cushing_level_known": inputs["cushing_level_mb"] is not None,
        "api_surprise_mb": api_surprise, "api_aligns": (api_surprise > 0) == (surprises["crude"] > 0),   # API vs EIA crude surprise (both against consensus)
        "beta_vol": round(beta, 3),
        "tls_center_mb": None if tls_center is None else round(tls_center, 3),
        "z_tls_demeaned": None if tls_center is None else round((tls - tls_center) / sigma, 2),
    }
    signal = {"action": "trade" if regime else "stand_down", "regime": regime, "direction": direction,
              "option_type": "NONE", "strike_type": "NONE",
              "reason": None if regime else "z_below_gate"}   # why it is a stand-down (None for a trade)
    move = option = sizing = None
    checklist = []
    if regime:
        signal["option_type"] = "CALL" if direction == "bullish" else "PUT"
        signal["strike_type"] = "ITM"
        low, high, deepened = options.target_delta(market["ovx"])
        option = {"delta_low": low, "delta_high": high, "ovx_deepened": deepened, "ovx": market["ovx"],
                  **options.pick_expiry(release_day)}
        if market.get("wti"):   # no option chain: an estimate of where the target-delta strikes sit
            option["strike_guide"] = options.strike_guidance(
                market["wti"] * usd_inr, market["ovx"], max(option["days_to_expiry"], 1), (low, high),
                signal["option_type"])
        if lots:   # {contract: lots} for the contracts you configured
            atr_stop = round(ATR_STOP_MULTIPLE * market["atr_1m"], 2) if market.get("atr_1m") else None
            wide = atr_stop is not None and atr_stop > max(options.STOP_BRACKET_USD)  # a normal swing beats the bracket
            sizing = options.sizing(lots, usd_inr, (low, high),
                                    (*options.STOP_BRACKET_USD, atr_stop) if wide else options.STOP_BRACKET_USD)
            sizing["atr_1m_stop_usd"] = atr_stop if wide else None
        if regime == 1:   # Regimes 2 and 3 target chart levels, not a modelled move
            usd = model.expected_move_usd(tls, beta, multiplier)
            anchor = model.anchor_move_usd(tls)
            move = {"wti_usd": round(usd, 2), "mcx_inr": int(round(usd * usd_inr)),
                    "per_mb_usd": round(abs(usd) / abs(tls), 3), "sanity_ok": model.sanity_ok(tls, usd),
                    "anchor_low_usd": anchor[0], "anchor_high_usd": anchor[1],
                    "anchor_low_inr": int(round(anchor[0] * usd_inr)), "anchor_high_inr": int(round(anchor[1] * usd_inr)),
                    "band": options.band_context(anchor[1] * usd_inr, market["wti"] * usd_inr) if market.get("wti") else None}
        checklist = REGIME_CHECK[regime] + ([TIME_SPREAD_CHECK] if regime == 1 and direction == "bullish" else []) + ALWAYS_CHECK
        if option["ovx_deepened"]:
            checklist.append(DEEP_STRIKE_CHECK)
        if option["expiry_source"] != "mcx_calendar":
            checklist.insert(0, f"The expiry date {option['expiry_date']} is a GUESS (that month is not in the MCX "
                             "calendar loaded: the 19th, or the business day before): check it on your chain.")
        if setup3 and regime != 3:
            checklist.insert(0, "Regime 3 inventory setup is present but the overnight rally is "
                             + ("unknown" if rally is None else f"only {rally:+.2f} USD (needs more than +1.00)")
                             + ", so it was not fired.")
    schedule = options.release_schedule(release_day)
    evening_open, closed_reason = options.mcx_evening_session(release_day)
    schedule["mcx_evening_open"], schedule["mcx_closed_reason"] = evening_open, closed_reason
    if regime and not evening_open:
        checklist.insert(0, f"MCX's EVENING SESSION IS CLOSED on {inputs['release_date']} ({closed_reason}): "
                            "this signal cannot be traded today.")
        # Anything that reads only `action` must not act on it: regime/direction stay for the record.
        signal["action"], signal["reason"] = "stand_down", f"mcx_evening_closed: {closed_reason}"
    fx = {"usd_inr": round(usd_inr, 2), "usd_inr_source": usd_inr_source}

    if regime:
        analysis, model_used = analyse(build_prompt(inputs, calc, signal))
    else:   # nothing to explain, and a model asked to explain noise will invent a direction
        analysis, model_used = "", "rule_based"
    return {
        "release_date": inputs["release_date"],
        "generated_at": generated_at or fmt_ts(now_ist()),
        "inputs": {**{k: v for k, v in inputs.items() if k != "release_date"},
                   "atr_20": market["atr_20"], "ovx": market["ovx"], **fx},
        "calculations": calc,
        "signal": signal,
        "expected_move": None if move is None else {**move, **fx},
        "option": option,
        "currency": currency.context(market.get("usd_inr_trend_pct"), direction) if regime else None,
        "sizing": sizing,
        "scorecard": scorecard(market, api_surprise),
        "schedule": schedule,
        "checklist": checklist,
        "analysis": analysis,
        "model_used": model_used,
    }


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Generate the TWPR signal")
    parser.add_argument("--allow-stale", action="store_true", help="accept input files older than %d days (replay)" % STALE_DAYS)
    args = parser.parse_args(argv)
    try:
        inputs = load_inputs(allow_stale=args.allow_stale)
        market = load_market(allow_stale=args.allow_stale)
        method = sigma_method()
        sigma, weeks = load_sigma(inputs["release_date"], method=method)
        load_lot_sizes()   # refuses a lot size that is not MCX's (the usual mistake: a lot COUNT in a SIZE setting)
        lots = {contract: n for contract, n in load_lot_counts().items() if n}
        if not lots:
            logger.warning("MCX_CRUDEOIL_LOTS / MCX_CRUDEOILM_LOTS not set - no lot count in the signal")
        usd_inr, source = fetch_usd_inr()
        signal = build_signal(inputs, market, sigma, usd_inr, source, lots=lots or None, sigma_method=method,
                              tls_center=load_center(inputs["release_date"]))
        write_json(SIGNAL_FILE, signal)
        record_week(inputs, inputs)   # this week's surprises join the history (no-op if already there)
        s, c = signal["signal"], signal["calculations"]
        logger.info("signal for %s: %s | regime %s %s | TLS %+.3f Z %+.2f (sigma %.3f %s, %d wks) | %s %s | model %s",
                    signal["release_date"], s["action"], s["regime"], s["direction"], c["tls_mb"], c["z_tls"],
                    c["sigma_forecast_mb"], c["sigma_method"], weeks, s["option_type"], s["strike_type"], signal["model_used"])
        return 0
    except InputError as exc:
        logger.error("%s", exc)
        send_error("signal_engine.py", f"{exc.title}\n{exc.detail}")
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("signal_engine failed")
        send_error("signal_engine.py", f"Unexpected {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
