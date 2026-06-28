from __future__ import annotations


class PortfolioManager:
    def __init__(self):
        self.positions = {}
        self.trades = []

    def update(self, trade):
        symbol = trade["symbol"]

        self.positions[symbol] = self.positions.get(symbol, 0) + float(trade.get("qty", 0.0))
        self.trades.append(dict(trade))
