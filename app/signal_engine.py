"""TWPR signal engine — the 5-step deterministic rule engine.

The rules below are the source of truth. The AI layer (Groq / Ollama) only
writes the narrative fields (key_drivers, risks, reasoning). It never touches
grade, direction, confidence, or the trade recommendation.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field

import httpx
from dotenv import load_dotenv

from common import DATA_DIR, now_utc, read_json, setup_logging, week_ending, write_json
from petrocore_client import PetroCoreClient
from telegram_bot import send_error

load_dotenv()

logger = logging.getLogger(__name__)

CONSENSUS_FILE = DATA_DIR / "consensus.json"
API_REPORT_FILE = DATA_DIR / "api_report.json"
EIA_ACTUAL_FILE = DATA_DIR / "eia_actual.json"
SIGNAL_FILE = DATA_DIR / "signal.json"

SKIP_THRESHOLD_MB = 1.0
GRADE_A_THRESHOLD_MB = 1.5
PRODUCTS_OPPOSE_THRESHOLD_MB = 2.0

GRADE_A_BASE_CONFIDENCE = 75
GRADE_B_BASE_CONFIDENCE = 55
CONFIDENCE_STEP = 5


@dataclass
class Signal:
    """A complete TWPR signal. `grade == 'skip'` means no trade this week."""

    grade: str
    direction: str
    confidence: int
    crude_deviation_mb: float = 0.0
    gasoline_deviation_mb: float = 0.0
    distillate_deviation_mb: float = 0.0
    cushing_mb: float = 0.0
    api_crude_mb: float = 0.0
    cushing_confirms: bool = False
    api_aligns: bool = False
    products_strongly_oppose: bool = False
    option_type: str | None = None
    strike_type: str | None = None
    size_pct: float | None = None
    key_drivers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    reasoning: str = ""
    model_used: str = "rule_based"


def generate_signal(
    crude_deviation_mb: float,
    gasoline_deviation_mb: float,
    distillate_deviation_mb: float,
    cushing_mb: float,
    api_crude_mb: float,
) -> Signal:
    """Run the 5-step rule engine. Same inputs always produce the same output."""
    # Step 1 — grade and direction from the crude deviation.
    if abs(crude_deviation_mb) <= SKIP_THRESHOLD_MB:
        return Signal(
            grade="skip",
            direction="neutral",
            confidence=0,
            crude_deviation_mb=crude_deviation_mb,
            gasoline_deviation_mb=gasoline_deviation_mb,
            distillate_deviation_mb=distillate_deviation_mb,
            cushing_mb=cushing_mb,
            api_crude_mb=api_crude_mb,
        )

    if crude_deviation_mb <= -GRADE_A_THRESHOLD_MB:
        grade, direction = "A", "bullish"
    elif crude_deviation_mb <= -SKIP_THRESHOLD_MB:
        grade, direction = "B", "bullish"
    elif crude_deviation_mb >= GRADE_A_THRESHOLD_MB:
        grade, direction = "A", "bearish"
    else:
        grade, direction = "B", "bearish"

    # Step 2 — Cushing adjustment. A downgrades to B; B never downgrades further.
    cushing_contradicts = (direction == "bullish" and cushing_mb > 0) or (
        direction == "bearish" and cushing_mb < 0
    )
    if cushing_contradicts and grade == "A":
        grade = "B"

    # Step 3 — products check. Noted as a risk; never changes the grade.
    bearish_sign = 1 if direction == "bearish" else -1
    products_strongly_oppose = (
        abs(gasoline_deviation_mb) > PRODUCTS_OPPOSE_THRESHOLD_MB
        and abs(distillate_deviation_mb) > PRODUCTS_OPPOSE_THRESHOLD_MB
        and gasoline_deviation_mb * bearish_sign > 0
        and distillate_deviation_mb * bearish_sign > 0
    )

    # Step 4 — API alignment.
    api_aligns = (direction == "bullish" and api_crude_mb < 0) or (
        direction == "bearish" and api_crude_mb > 0
    )

    # Step 5 — confidence, off the post-adjustment grade. Range 45-85.
    confidence = GRADE_A_BASE_CONFIDENCE if grade == "A" else GRADE_B_BASE_CONFIDENCE
    confidence += -CONFIDENCE_STEP if cushing_contradicts else CONFIDENCE_STEP
    confidence += CONFIDENCE_STEP if api_aligns else -CONFIDENCE_STEP

    option_type = "call" if direction == "bullish" else "put"
    strike_type, size_pct = ("ATM", 2.0) if grade == "A" else ("1-OTM", 1.5)

    return Signal(
        grade=grade,
        direction=direction,
        confidence=confidence,
        crude_deviation_mb=crude_deviation_mb,
        gasoline_deviation_mb=gasoline_deviation_mb,
        distillate_deviation_mb=distillate_deviation_mb,
        cushing_mb=cushing_mb,
        api_crude_mb=api_crude_mb,
        cushing_confirms=not cushing_contradicts,
        api_aligns=api_aligns,
        products_strongly_oppose=products_strongly_oppose,
        option_type=option_type,
        strike_type=strike_type,
        size_pct=size_pct,
    )


def rule_based_narrative(signal: Signal) -> tuple[list[str], list[str], str]:
    """Deterministic drivers/risks/reasoning — the fallback when no AI is configured."""
    if signal.grade == "skip":
        return (
            [f"Crude deviation {signal.crude_deviation_mb:+.3f} mb is inside the skip zone"],
            [],
            "Crude came in within 1.0 mb of consensus. No directional edge — no trade.",
        )

    surprise = "larger draw" if signal.direction == "bullish" else "larger build"
    drivers = [
        f"EIA crude {surprise} than consensus by {abs(signal.crude_deviation_mb):.3f} mb",
        f"Cushing {signal.cushing_mb:+.3f} mb "
        f"{'confirms' if signal.cushing_confirms else 'contradicts'} the direction",
        f"API crude {signal.api_crude_mb:+.3f} mb "
        f"{'aligns with' if signal.api_aligns else 'contradicts'} the direction",
    ]

    risks = []
    if not signal.cushing_confirms:
        risks.append("Cushing moved against the headline crude number")
    if not signal.api_aligns:
        risks.append("API private report pointed the other way")
    if signal.products_strongly_oppose:
        risks.append(
            f"Products moved strongly the same way (gasoline {signal.gasoline_deviation_mb:+.3f} mb,"
            f" distillate {signal.distillate_deviation_mb:+.3f} mb)"
        )
    if signal.grade == "B":
        risks.append("Grade B — reduced size, expect a lower hit rate")
    risks.append("Options buyer: theta and IV crush after the release both work against you")

    reasoning = (
        f"Crude deviation of {signal.crude_deviation_mb:+.3f} mb gives a Grade {signal.grade} "
        f"{signal.direction} signal at {signal.confidence} confidence. "
        f"Trade: buy {signal.strike_type} {signal.option_type}, {signal.size_pct}% of capital. "
        "Hard close 22:30 IST regardless of P&L."
    )
    return drivers, risks, reasoning


def _ai_narrative(signal: Signal, mode: str) -> tuple[list[str], list[str], str] | None:
    """Ask Groq/Ollama to write the narrative. Returns None on any failure."""
    prompt = (
        "You are a commodities analyst. Explain this MCX CrudeOil options signal.\n"
        "The grade, direction and confidence are FIXED — do not dispute or recompute them.\n\n"
        f"{json.dumps(asdict(signal), indent=2)}\n\n"
        'Reply with JSON only: {"key_drivers": [3 short strings], '
        '"risks": [2-4 short strings], "reasoning": "2-3 sentences"}'
    )

    try:
        if mode == "groq":
            from groq import Groq

            model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            completion = Groq(api_key=os.environ["GROQ_API_KEY"]).chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content
        elif mode == "ollama":
            model = os.getenv("OLLAMA_MODEL", "llama3.1")
            response = httpx.post(
                f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
                timeout=60.0,
            )
            response.raise_for_status()
            content = response.json()["response"]
        else:
            return None

        parsed = json.loads(content)
        return (
            [str(d) for d in parsed["key_drivers"]],
            [str(r) for r in parsed["risks"]],
            str(parsed["reasoning"]),
        )
    except Exception as exc:  # noqa: BLE001 — narrative is cosmetic, never fail the signal
        logger.error("AI narrative failed (%s), falling back to rule_based: %s", mode, exc)
        return None


def add_narrative(signal: Signal) -> Signal:
    """Attach drivers/risks/reasoning, using MODEL_MODE with a rule-based fallback."""
    mode = os.getenv("MODEL_MODE", "rule_based").lower()
    result = _ai_narrative(signal, mode) if mode in ("groq", "ollama") else None

    if result is None:
        signal.key_drivers, signal.risks, signal.reasoning = rule_based_narrative(signal)
        signal.model_used = "rule_based"
    else:
        signal.key_drivers, signal.risks, signal.reasoning = result
        signal.model_used = os.getenv("GROQ_MODEL", mode) if mode == "groq" else mode

    return signal


def _require(report: dict | None, name: str, path: object) -> dict:
    """Return the report dict or raise, naming the file that should have produced it."""
    if report is None:
        raise FileNotFoundError(f"{name} missing — expected {path}")
    return report


def main() -> int:
    """Load the three reports, run the rule engine, save and publish the signal."""
    setup_logging()
    try:
        consensus = _require(read_json(CONSENSUS_FILE), "consensus", CONSENSUS_FILE)
        eia = _require(read_json(EIA_ACTUAL_FILE), "EIA actuals", EIA_ACTUAL_FILE)
        # The API report is optional — the pipeline still trades without it.
        api_report = read_json(API_REPORT_FILE) or {}

        crude_deviation = eia["crude_change_mb"] - consensus["crude_consensus_mb"]
        gasoline_deviation = eia["gasoline_change_mb"] - consensus["gasoline_consensus_mb"]
        distillate_deviation = eia["distillate_change_mb"] - consensus["distillate_consensus_mb"]
        api_crude = api_report.get("api_crude_mb", 0.0)

        if not api_report:
            logger.warning("No API report found — treating api_crude_mb as 0.0 (contradicts)")

        signal = add_narrative(
            generate_signal(
                crude_deviation_mb=crude_deviation,
                gasoline_deviation_mb=gasoline_deviation,
                distillate_deviation_mb=distillate_deviation,
                cushing_mb=eia["cushing_stocks_mb"],
                api_crude_mb=api_crude,
            )
        )

        week = eia.get("week_ending") or week_ending()
        payload = {
            "week_ending": week,
            "setup": "TWPR",
            "trade_type": os.getenv("TWPR_TRADE_TYPE", "paper"),
            **asdict(signal),
            "generated_at": now_utc().isoformat(),
        }
        write_json(SIGNAL_FILE, payload)
        logger.info(
            "Signal: Grade %s %s | confidence %s | deviation %+.3f mb",
            signal.grade,
            signal.direction,
            signal.confidence,
            crude_deviation,
        )

        PetroCoreClient().post_signal(payload)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("signal_engine.py failed")
        send_error("signal_engine.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
