from __future__ import annotations


class LearningEngine:
    def __init__(self):
        self.weights = {}
        self.events = []

    def update(self, trade):
        reward = float(trade.get("pnl", 0.0))

        strategy_id = trade["strategy"]

        self.events.append(dict(trade))
        self.update_weights(strategy_id, reward)

    def update_weights(self, sid, reward):
        self.weights[sid] = self.weights.get(sid, 1.0)

        self.weights[sid] *= (1 + float(reward) * 0.001)
        self.weights[sid] = round(self.weights[sid], 10)
