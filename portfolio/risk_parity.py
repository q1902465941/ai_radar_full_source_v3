from __future__ import annotations


class RiskParityAllocator:
    def allocate(self, strategies, capital):
        if not strategies:
            return {}

        inv_risk = {}

        for s in strategies:
            inv_risk[s["name"]] = 1 / (float(s.get("volatility", 0.0)) + 1e-6)

        total = sum(inv_risk.values())
        if total <= 0:
            return {s["name"]: 0.0 for s in strategies}

        allocation = {}

        for k, v in inv_risk.items():
            allocation[k] = float(capital) * (v / total)

        return allocation
