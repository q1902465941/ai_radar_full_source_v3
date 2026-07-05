from __future__ import annotations

from typing import Any

from backend.models import now_ms
from backend.strategy_alpha.generator import strategy_alpha_id


class SeedBank:
    """Warm-start seed strategies for alpha evolution.

    Seeds are research hypotheses only. They are stored as strategy_alpha
    records and must pass replay evaluation and promotion policy before they
    can influence PRG.
    """

    def get_initial_strategies(self) -> list[dict[str, Any]]:
        return [
            self._strategy(
                "momentum",
                {
                    "lookback": 20,
                    "threshold": 1.2,
                    "min_score": 72.0,
                    "max_wick_ratio": 0.35,
                    "min_fund_confirm": 3,
                    "min_direction_confirmations": 6,
                    "min_volume_spike": 1.6,
                    "min_depth_alignment": 0.12,
                    "min_taker_ratio": 0.6,
                    "max_fake_breakout_risk": "LOW",
                    "tp_r": 1.6,
                    "risk_pct": 0.01,
                    "horizon_steps": 20,
                },
            ),
            self._strategy(
                "mean_reversion",
                {
                    "lookback": 15,
                    "threshold": 2.0,
                    "min_score": 65.0,
                    "max_wick_ratio": 0.45,
                    "min_fund_confirm": 2,
                    "min_direction_confirmations": 5,
                    "min_volume_spike": 1.2,
                    "min_depth_alignment": 0.05,
                    "min_taker_ratio": 0.55,
                    "max_fake_breakout_risk": "LOW",
                    "tp_r": 1.2,
                    "risk_pct": 0.008,
                    "horizon_steps": 15,
                },
            ),
            self._strategy(
                "breakout",
                {
                    "lookback": 30,
                    "threshold": 1.5,
                    "min_score": 78.0,
                    "max_wick_ratio": 0.25,
                    "min_fund_confirm": 3,
                    "min_direction_confirmations": 7,
                    "min_volume_spike": 2.0,
                    "min_depth_alignment": 0.18,
                    "min_taker_ratio": 0.62,
                    "max_fake_breakout_risk": "LOW",
                    "tp_r": 2.0,
                    "risk_pct": 0.012,
                    "horizon_steps": 30,
                },
            ),
            self._strategy(
                "radar_flow",
                {
                    "lookback": 47,
                    "threshold": 2.75,
                    "min_score": 54.343145,
                    "max_wick_ratio": 0.797299,
                    "min_fund_confirm": 2,
                    "min_direction_confirmations": 3,
                    "min_volume_spike": 2.736315,
                    "min_depth_alignment": 0.095421,
                    "min_taker_ratio": 0.549618,
                    "max_fake_breakout_risk": "LOW",
                    "tp_r": 2.764717,
                    "risk_pct": 0.011957,
                    "horizon_steps": 47,
                },
            ),
        ]

    def _strategy(self, alpha_type: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = dict(params)
        payload["alpha_type"] = alpha_type
        return {
            "strategy_id": strategy_alpha_id(payload, salt=f"seed:{alpha_type}"),
            "alpha_type": alpha_type,
            "source": "strategy_alpha_seed",
            "status": "RESEARCH_ALPHA",
            "params": payload,
            "created_at": now_ms(),
        }


seed_bank = SeedBank()
