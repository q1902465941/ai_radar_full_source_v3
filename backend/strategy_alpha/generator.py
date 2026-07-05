from __future__ import annotations

import hashlib
import json
import random
from typing import Any

from backend.models import now_ms


PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "min_score": (30.0, 95.0),
    "max_wick_ratio": (0.05, 0.8),
    "min_fund_confirm": (1.0, 3.0),
    "min_direction_confirmations": (3.0, 8.0),
    "min_volume_spike": (0.8, 3.5),
    "min_depth_alignment": (0.0, 0.5),
    "min_taker_ratio": (0.5, 0.75),
    "tp_r": (1.0, 3.5),
    "risk_pct": (0.006, 0.025),
    "horizon_steps": (3.0, 72.0),
}
FAKE_RISK_LEVELS = ("LOW", "MEDIUM")


class StrategyAlphaGenerator:
    def __init__(self, seed: int | None = None) -> None:
        self.random = random.Random(seed)

    def generate(self, count: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        while len(out) < max(1, int(count)):
            params = self._params()
            strategy_id = strategy_alpha_id(params, salt=str(len(out)))
            if strategy_id in seen:
                continue
            seen.add(strategy_id)
            out.append(
                {
                    "strategy_id": strategy_id,
                    "source": "strategy_alpha",
                    "status": "RESEARCH_ALPHA",
                    "params": params,
                    "created_at": now_ms(),
                }
            )
        return out

    def _params(self) -> dict[str, Any]:
        return {
            "min_score": round(self.random.uniform(*PARAM_BOUNDS["min_score"]), 2),
            "max_wick_ratio": round(self.random.uniform(*PARAM_BOUNDS["max_wick_ratio"]), 4),
            "min_fund_confirm": self.random.randint(int(PARAM_BOUNDS["min_fund_confirm"][0]), int(PARAM_BOUNDS["min_fund_confirm"][1])),
            "min_direction_confirmations": self.random.randint(
                int(PARAM_BOUNDS["min_direction_confirmations"][0]),
                int(PARAM_BOUNDS["min_direction_confirmations"][1]),
            ),
            "min_volume_spike": round(self.random.uniform(*PARAM_BOUNDS["min_volume_spike"]), 4),
            "min_depth_alignment": round(self.random.uniform(*PARAM_BOUNDS["min_depth_alignment"]), 4),
            "min_taker_ratio": round(self.random.uniform(*PARAM_BOUNDS["min_taker_ratio"]), 4),
            "max_fake_breakout_risk": self.random.choice(FAKE_RISK_LEVELS),
            "tp_r": round(self.random.uniform(*PARAM_BOUNDS["tp_r"]), 4),
            "risk_pct": round(self.random.uniform(*PARAM_BOUNDS["risk_pct"]), 6),
            "horizon_steps": self.random.randint(int(PARAM_BOUNDS["horizon_steps"][0]), int(PARAM_BOUNDS["horizon_steps"][1])),
        }


def strategy_alpha_id(params: dict[str, Any], *, salt: str = "") -> str:
    raw = json.dumps({"params": params, "salt": salt}, sort_keys=True, ensure_ascii=True)
    return "alpha_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
