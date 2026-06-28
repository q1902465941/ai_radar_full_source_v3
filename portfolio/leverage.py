from __future__ import annotations


class LeverageController:
    def get(self, regime, drawdown):
        base = 2

        if regime == "trend":
            base = 3
        elif regime == "volatile":
            base = 1

        if float(drawdown or 0.0) > 0.1:
            base *= 0.5

        return max(1, min(base, 5))
