from collections import defaultdict
from backend.models import MarketSnapshot

def clamp(v, lo=0, hi=100): return max(lo, min(hi, v))

class SmartMoneyModel:
    def __init__(self):
        self.prev={}

    def estimate(self, s: MarketSnapshot):
        oi_abnormal = clamp(abs(s.oi_change)/2.5*100)
        depth_score = clamp((abs(s.depth_imbalance))*100)
        vol_score = clamp((s.volume_spike-0.5)/3*100)
        taker_score = clamp(abs(s.taker_buy_ratio-0.5)/0.2*100)
        funding_div = clamp(abs(s.funding_rate)/0.0008*100)
        pos = oi_abnormal*0.25 + depth_score*0.15 + vol_score*0.2 + taker_score*0.25 + funding_div*0.15
        pos = round(clamp(pos), 2)
        delta = round(pos - self.prev.get(s.symbol, pos), 2)
        self.prev[s.symbol]=pos
        return pos, delta

smart_money = SmartMoneyModel()
