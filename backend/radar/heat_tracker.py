from __future__ import annotations
from collections import defaultdict, deque

class HeatTracker:
    def __init__(self, window=8):
        self.window=window
        self.score_hist=defaultdict(lambda: deque(maxlen=window))
        self.rank_hist=defaultdict(lambda: deque(maxlen=window))
        self.oi_hist=defaultdict(lambda: deque(maxlen=window))
        self.sm_hist=defaultdict(lambda: deque(maxlen=window))

    def pre_history(self, symbol: str):
        return list(self.score_hist[symbol])

    def update(self, symbol: str, score: float, rank: int, oi: float, sm: float):
        self.score_hist[symbol].append(float(score))
        self.rank_hist[symbol].append(int(rank))
        self.oi_hist[symbol].append(float(oi))
        self.sm_hist[symbol].append(float(sm))

    def histories(self, symbol: str):
        return {
            "score_history": list(self.score_hist[symbol]),
            "rank_history": list(self.rank_hist[symbol]),
            "oi_history": list(self.oi_hist[symbol]),
            "sm_history": list(self.sm_hist[symbol]),
        }

    def slope(self, symbol: str):
        ys=list(self.score_hist[symbol])
        if len(ys) < 2: return 0.0, 0.0
        n=len(ys); xs=list(range(n)); xm=sum(xs)/n; ym=sum(ys)/n
        den=sum((x-xm)**2 for x in xs)
        raw=sum((xs[i]-xm)*(ys[i]-ym) for i in range(n))/(den or 1)
        score=max(0, min(100, raw*18))
        return round(raw,2), round(score,2)

heat_tracker = HeatTracker()
