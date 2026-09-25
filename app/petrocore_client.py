"""Shared HTTP client for posting TWPR data to the PetroCore backend.

PetroCore is optional. If PETROCORE_URL is unset the client logs a warning and
skips the POST — every script must keep working standalone with JSON files as
the system of record.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 2
BACKOFF_SECONDS = 2.0


class PetroCoreClient:
    """POSTs TWPR payloads to PetroCore. Never raises — failures are logged."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("PETROCORE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("PETROCORE_API_KEY", "")
        self.enabled = bool(self.base_url)
        if not self.enabled:
            logger.warning("PETROCORE_URL not set — PetroCore POSTs will be skipped")

    def _post(self, path: str, payload: dict[str, Any]) -> bool:
        """POST payload to path. Returns True on 2xx, False on skip or failure."""
        if not self.enabled:
            logger.warning("PetroCore disabled — skipping POST %s", path)
            return False

        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}

        for attempt in range(MAX_RETRIES + 1):
            started = time.monotonic()
            try:
                response = httpx.post(
                    url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                elapsed_ms = (time.monotonic() - started) * 1000
                logger.error(
                    "POST %s failed after %.0fms (attempt %d/%d): %s",
                    url, elapsed_ms, attempt + 1, MAX_RETRIES + 1, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_SECONDS)
                    continue
                return False

            elapsed_ms = (time.monotonic() - started) * 1000
            logger.info("POST %s -> %d (%.0fms)", url, response.status_code, elapsed_ms)

            if response.status_code < 300:
                return True

            # Retry server errors only; 4xx is our bug and will not fix itself.
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS)
                continue

            logger.error("POST %s rejected: %d %s", url, response.status_code, response.text[:500])
            return False

        return False

    def post_consensus(self, payload: dict[str, Any]) -> bool:
        """POST the Tuesday analyst consensus survey."""
        return self._post("/api/v1/twpr/consensus", payload)

    def post_api_report(self, payload: dict[str, Any]) -> bool:
        """POST the API (American Petroleum Institute) private inventory report."""
        return self._post("/api/v1/twpr/api-report", payload)

    def post_eia_report(self, payload: dict[str, Any]) -> bool:
        """POST the EIA WPSR actuals."""
        return self._post("/api/v1/twpr/eia-report", payload)

    def post_signal(self, payload: dict[str, Any]) -> bool:
        """POST the generated TWPR signal."""
        return self._post("/api/v1/twpr/signal", payload)

    def post_market_data(self, payload: dict[str, Any]) -> bool:
        """POST one day of market data."""
        return self._post("/api/v1/market/daily", payload)

    def post_trade(self, payload: dict[str, Any]) -> bool:
        """POST a completed or exited trade."""
        return self._post("/api/v1/twpr/trade", payload)
