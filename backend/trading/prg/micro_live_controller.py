from __future__ import annotations

from typing import Any

MICRO_LIVE_MAX_CAPITAL_USD = 100.0
MICRO_LIVE_MAX_DAILY_LOSS_USD = 5.0


class MicroLiveController:
    def __init__(self) -> None:
        self.capital = 0.0
        self.max_loss = MICRO_LIVE_MAX_DAILY_LOSS_USD

    def start(self, capital: float) -> dict[str, Any]:
        value = float(capital)
        if value > MICRO_LIVE_MAX_CAPITAL_USD:
            raise RuntimeError("MICRO_LIVE_CAPITAL_TOO_HIGH")
        self.capital = value
        self.max_loss = MICRO_LIVE_MAX_DAILY_LOSS_USD
        return {
            "mode": "MICRO_LIVE",
            "capital": self.capital,
            "max_loss": self.max_loss,
        }


micro_live_controller = MicroLiveController()
