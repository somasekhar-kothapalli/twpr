"""The TWPR signal engine: 5-step rules -> data/signal.json.

Reads data/consensus.json + data/api_report.json + data/eia_actuals.json. No
scraping. Runs right after eia_actuals.py (Wednesday ~20:02 IST) and must finish
in well under 60 s. Telegram is used for ERRORS only.

The rule engine is pure (the functions below take plain values, no I/O, no clock,
no randomness). The only external calls are the USD/INR quote and the Groq
narrative, and the narrative can never touch grade, direction, confidence or the
trade recommendation: if Groq fails the signal still ships with analysis "".

Run from the repo root:
    python -m app.signal_engine                  # today's data
    python -m app.signal_engine --allow-stale    # replay data older than 2 days
"""
import argparse
import json
import logging
import sys
import threading

from dotenv import load_dotenv

from app.utils.common import (API_REPORT_FILE, CONSENSUS_FILE, DATA_DIR, EIA_ACTUALS_FILE, ROOT, SIGNAL_FILE, env,
                              fmt_ts, is_stale, now_ist, parse_release_date, setup_logging, write_json)
from app.utils.telegram import send_error

logger = logging.getLogger("twpr.signal_engine")

SKIP_MB = 1.0              # |crude deviation| <= this: skip (inclusive)
GRADE_A_MB = 1.5           # |crude deviation| >= this: Grade A (inclusive)
PRODUCTS_OPPOSE_MB = 2.0
BASE_CONFIDENCE = {"A": 75, "B": 55}
CONFIDENCE_STEP = 5
CONFIDENCE_MIN, CONFIDENCE_MAX = 40, 85
MOVE_RANGES = {
    "A": {"wti_low": 1.5, "wti_high": 3.0},
    "B": {"wti_low": 0.8, "wti_high": 1.5},
    "skip": {"wti_low": 0.0, "wti_high": 0.0},
}

STALE_DAYS = 2             # consensus / EIA data older than this is refused
API_MAX_LEAD_DAYS = 3      # the API report is released 1-2 days BEFORE the EIA report
FALLBACK_USD_INR = 84.0
USD_INR_PLAUSIBLE = (50.0, 150.0)  # a quote outside this is bad data, not a rate
USD_INR_TIMEOUT_S = 10
GROQ_TIMEOUT_S = 15.0
ANALYSIS_MAX_CHARS = 200
GROQ_SYSTEM_PROMPT = (
    "You are a crude oil options trading analyst.\n"
    "Write exactly 3 sentences.\n"
    "Sentence 1: What the crude deviation means for immediate WTI direction.\n"
    "Sentence 2: The most important confirming or contradicting factor.\n"
    "Sentence 3: One key risk to the signal.\n"
    "Be direct. Use numbers. No disclaimers. Max 70 words total."
)


# --------------------------------------------------------------------------- rules

def calculate_deviations(crude_change, crude_consensus, gasoline_change, gasoline_consensus,
                         distillate_change, distillate_consensus):
    """actual - consensus, rounded to 3 dp. Positive = bearish surprise (bigger build
    than expected). Product deviations are None if either input is None."""
    def deviation(actual, consensus):
        return None if actual is None or consensus is None else round(actual - consensus, 3)

    return {
        "crude_deviation_mb": round(crude_change - crude_consensus, 3),
        "gasoline_deviation_mb": deviation(gasoline_change, gasoline_consensus),
        "distillate_deviation_mb": deviation(distillate_change, distillate_consensus),
    }


def apply_grade_logic(crude_deviation):
    """(grade, direction). Both thresholds are inclusive: exactly +/-1.0 is a skip,
    exactly +/-1.5 is Grade A."""
    if abs(crude_deviation) <= SKIP_MB:
        return "skip", "neutral"
    grade = "A" if abs(crude_deviation) >= GRADE_A_MB else "B"
    return grade, "bullish" if crude_deviation < 0 else "bearish"


def apply_cushing_adjustment(grade, direction, cushing_change_mb):
    """(grade, cushing_contradicts). A Cushing build contradicts a bullish signal, a
    draw contradicts a bearish one (exactly 0.0 contradicts nothing). A contradiction
    downgrades A to B; B is the floor. None when Cushing is missing."""
    if grade == "skip" or cushing_change_mb is None:
        return grade, None
    contradicts = (direction == "bullish" and cushing_change_mb > 0) or (
        direction == "bearish" and cushing_change_mb < 0)
    if contradicts and grade == "A":
        grade = "B"
    return grade, contradicts


def check_products(direction, gasoline_deviation, distillate_deviation):
    """True if BOTH product deviations exceed 2.0 mb in the direction of the crude
    surprise (bearish: both > +2.0, bullish: both < -2.0). None if data is missing.
    A flag only - it never changes the grade.

    Same-sign rule, matching the validated reference week (bearish, +2.669/+2.787 ->
    true) and the old engine. Not the mirrored rule the original prompt's formula
    text described, which contradicted its own sample output."""
    if direction == "neutral" or gasoline_deviation is None or distillate_deviation is None:
        return None
    sign = 1 if direction == "bearish" else -1
    return all(dev * sign > PRODUCTS_OPPOSE_MB for dev in (gasoline_deviation, distillate_deviation))


def check_api_alignment(direction, api_crude_mb):
    """True if the API crude change points the same way as the signal (bullish + draw,
    bearish + build)."""
    return (direction == "bullish" and api_crude_mb < 0) or (direction == "bearish" and api_crude_mb > 0)


def calculate_confidence(grade, cushing_contradicts, api_aligns):
    """0 for a skip; else base (A 75 / B 55) +/-5 for Cushing (None = no adjustment)
    and +/-5 for the API, clamped to 40-85. Computed from the grade AFTER the Cushing
    downgrade."""
    if grade == "skip":
        return 0
    confidence = BASE_CONFIDENCE[grade]
    if cushing_contradicts is not None:
        confidence += -CONFIDENCE_STEP if cushing_contradicts else CONFIDENCE_STEP
    confidence += CONFIDENCE_STEP if api_aligns else -CONFIDENCE_STEP
    return max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, confidence))


def get_trade_recommendation(grade, direction):
    if grade == "skip":
        return {"option_type": "NONE", "strike_type": "NONE", "size_pct": 0.0}
    option_type = "CALL" if direction == "bullish" else "PUT"
    strike_type, size_pct = ("ATM", 2.0) if grade == "A" else ("1-OTM", 1.5)
    return {"option_type": option_type, "strike_type": strike_type, "size_pct": size_pct}


def get_expected_move(grade, direction, usd_inr):
    """Rule-based WTI move range and its MCX equivalent in INR. For bearish the range
    is negative, smaller move first (B: -0.8 then -1.5)."""
    sign = -1 if direction == "bearish" else 1
    wti_low = round(sign * MOVE_RANGES[grade]["wti_low"], 1)
    wti_high = round(sign * MOVE_RANGES[grade]["wti_high"], 1)
    return {
        "wti_low": wti_low,
        "wti_high": wti_high,
        "mcx_low": int(round(wti_low * usd_inr, 0)),
        "mcx_high": int(round(wti_high * usd_inr, 0)),
        "usd_inr": round(usd_inr, 2),
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
    if not data.get("release_date"):
        raise InputError("release_date missing", f"{label} ({path.name}) has no release_date")
    try:
        parse_release_date(data["release_date"])
    except ValueError as exc:
        raise InputError("release_date invalid", f"{label} ({path.name}): {data['release_date']!r} is not DD-MM-YYYY") from exc
    return data


def _num(data, key):
    value = data.get(key)
    return None if value is None else round(float(value), 3)


def load_inputs(data_dir=None, today=None, allow_stale=False):
    """The flat inputs dict (spec key names) plus `release_date`, or raise InputError."""
    data_dir = data_dir or DATA_DIR
    consensus = _load(data_dir / CONSENSUS_FILE.name, "consensus")
    api = _load(data_dir / API_REPORT_FILE.name, "API report")
    eia = _load(data_dir / EIA_ACTUALS_FILE.name, "EIA actuals")

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
        "gasoline_change_mb": _num(eia, "gasoline_change_mb"),
        "distillate_change_mb": _num(eia, "distillate_change_mb"),
        "refinery_util_change_pct": _num(eia, "refinery_util_change_pct"),
        "api_crude_mb": _num(api, "api_crude_mb"),
        "api_cushing_mb": _num(api, "api_cushing_mb"),
    }
    missing = [f"{key} ({label})" for key, label in ((("crude_consensus_mb", CONSENSUS_FILE.name),
                                                      ("crude_change_mb", EIA_ACTUALS_FILE.name),
                                                      ("api_crude_mb", API_REPORT_FILE.name))) if inputs[key] is None]
    if missing:
        raise InputError("Mandatory field missing", ", ".join(missing))
    return inputs


# ------------------------------------------------------------------ external calls

def fetch_usd_inr():
    """(rate, source): the yfinance quote, or the fallback if it fails, hangs or is implausible."""
    result = {}

    def work():
        try:
            import yfinance as yf
            result["rate"] = float(yf.Ticker("INR=X").fast_info["lastPrice"])
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            result["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(USD_INR_TIMEOUT_S)
    rate = result.get("rate")
    if rate is not None and USD_INR_PLAUSIBLE[0] < rate < USD_INR_PLAUSIBLE[1]:
        logger.info("USD/INR %.2f (yfinance)", rate)
        return rate, "yfinance"
    logger.warning("USD/INR unavailable (%s) - using fallback %.1f",
                   result.get("error") or (f"implausible {rate}" if rate is not None else "timed out"), FALLBACK_USD_INR)
    return FALLBACK_USD_INR, "fallback"


def fmt_optional(value):
    return f"{value:+.3f}" if value is not None else "N/A"


def build_prompt(inputs, calc, grade, direction, confidence, cushing_contradicts, api_aligns):
    cushing_status = "contradicts" if cushing_contradicts else "confirms" if cushing_contradicts is False else "unknown"
    api_status = "aligns" if api_aligns else "contradicts" if api_aligns is False else "n/a"
    return (
        f"Release: {inputs['release_date']}\n"
        f"Crude deviation: {calc['crude_deviation_mb']:+.3f} mb\n"
        f"Direction: {direction} | Grade: {grade} | Confidence: {confidence}%\n"
        f"Cushing: {fmt_optional(inputs['cushing_change_mb'])} mb ({cushing_status})\n"
        f"Gasoline deviation: {fmt_optional(calc['gasoline_deviation_mb'])} mb\n"
        f"Distillate deviation: {fmt_optional(calc['distillate_deviation_mb'])} mb\n"
        f"API crude: {inputs['api_crude_mb']:+.3f} mb ({api_status})\n"
        f"Refinery util change: {fmt_optional(inputs['refinery_util_change_pct'])}%\n"
        f"Products oppose: {calc['products_oppose']}"
    )


def generate_analysis(user_prompt):
    """(analysis, model_used) from Groq, or ("", "rule_based") if the key is missing or
    anything fails. The only place a model is involved, and it only writes prose."""
    api_key = env("GROQ_API_KEY")
    if not api_key:
        logger.info("GROQ_API_KEY not set - no narrative")
        return "", "rule_based"
    model = env("GROQ_MODEL", "llama-3.3-70b-versatile")
    try:
        from groq import Groq
        response = Groq(api_key=api_key, timeout=GROQ_TIMEOUT_S).chat.completions.create(
            model=model, max_tokens=150, temperature=0.3,
            messages=[{"role": "system", "content": GROQ_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
        )
        text = response.choices[0].message.content.strip()[:ANALYSIS_MAX_CHARS].strip() # type: ignore
    except Exception as exc:  # noqa: BLE001 - the signal ships without a narrative
        logger.warning("Groq failed (%s: %s) - no narrative", type(exc).__name__, exc)
        return "", "rule_based"
    return (text, f"groq/{model}") if text else ("", "rule_based")


# ------------------------------------------------------------------------ assembly

def build_signal(inputs, usd_inr, usd_inr_source, analyse=generate_analysis, generated_at=None):
    """The full signal.json payload from validated inputs."""
    calc = calculate_deviations(
        inputs["crude_change_mb"], inputs["crude_consensus_mb"],
        inputs["gasoline_change_mb"], inputs["gasoline_consensus_mb"],
        inputs["distillate_change_mb"], inputs["distillate_consensus_mb"])
    grade, direction = apply_grade_logic(calc["crude_deviation_mb"])

    cushing_contradicts = products_oppose = api_aligns = None
    if grade != "skip":  # steps 3-6 are skipped for a skip
        grade, cushing_contradicts = apply_cushing_adjustment(grade, direction, inputs["cushing_change_mb"])
        products_oppose = check_products(direction, calc["gasoline_deviation_mb"], calc["distillate_deviation_mb"])
        api_aligns = check_api_alignment(direction, inputs["api_crude_mb"])
    confidence = calculate_confidence(grade, cushing_contradicts, api_aligns)
    calc.update({"cushing_contradicts": cushing_contradicts, "products_oppose": products_oppose,
                 "api_aligns": api_aligns})

    trade = get_trade_recommendation(grade, direction)
    move = get_expected_move(grade, direction, usd_inr)
    move["usd_inr_source"] = usd_inr_source
    analysis, model_used = analyse(build_prompt(inputs, calc, grade, direction, confidence,
                                                cushing_contradicts, api_aligns))
    return {
        "release_date": inputs["release_date"],
        "generated_at": generated_at or fmt_ts(now_ist()),
        "inputs": {k: v for k, v in inputs.items() if k != "release_date"},
        "calculations": calc,
        "signal": {"grade": grade, "direction": direction, "confidence": confidence, **trade},
        "expected_move": move,
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
        usd_inr, source = fetch_usd_inr()
        signal = build_signal(inputs, usd_inr, source)
        write_json(SIGNAL_FILE, signal)
        s = signal["signal"]
        logger.info("signal for %s: grade %s %s | confidence %d | %s %s %.1f%% | model %s",
                    signal["release_date"], s["grade"], s["direction"], s["confidence"], s["option_type"],
                    s["strike_type"], s["size_pct"], signal["model_used"])
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
