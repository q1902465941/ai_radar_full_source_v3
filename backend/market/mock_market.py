from __future__ import annotations
import random, math, time
from backend.models import MarketSnapshot

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","ONDOUSDT","STGUSDT","HEIUSDT","LITUSDT","NFPUSDT","SUIUSDT","WLDUSDT","FETUSDT","ENAUSDT","FIDAUSDT","BEATUSDT","ALLOUSDT","XLMUSDT","IDUSDT","VVVUSDT","BILLUSDT","TIAUSDT","SEIUSDT","ARBUSDT","OPUSDT","INJUSDT","JTOUSDT","PYTHUSDT","ORDIUSDT","MEMEUSDT","PEOPLEUSDT","DOGEUSDT","1000PEPEUSDT","1000BONKUSDT","1000SHIBUSDT","LINKUSDT","AAVEUSDT","UNIUSDT","AVAXUSDT","NEARUSDT","APTUSDT","ATOMUSDT","FILUSDT","GALAUSDT","JASMYUSDT","LDOUSDT","MANTAUSDT","PENDLEUSDT","ARKMUSDT","ALTUSDT","PIXELUSDT","STRKUSDT","ZROUSDT","NOTUSDT","IOUSDT","BOMEUSDT","WIFUSDT"
]

BASE_PRICE = {
    "BTCUSDT": 73889.8, "ETHUSDT": 2025.01, "BNBUSDT": 725.88, "SOLUSDT": 182.67,
}

class MockMarketData:
    def __init__(self):
        self.tick = 0
        self.prices = {s: BASE_PRICE.get(s, random.uniform(0.02, 5.0)) for s in SYMBOLS}
        self.bias = {s: random.uniform(-1,1) for s in SYMBOLS}

    def symbols(self):
        return SYMBOLS[:]

    def _step_price(self, symbol):
        base_vol = 0.0008 if symbol in BASE_PRICE else random.uniform(0.001, 0.008)
        wave = math.sin((self.tick + hash(symbol)%100)/7) * base_vol * 4
        shock = random.gauss(0, base_vol)
        drift = self.bias[symbol] * base_vol * 0.2
        self.prices[symbol] = max(0.00001, self.prices[symbol] * (1 + wave + shock + drift))
        return self.prices[symbol]

    async def get_snapshots(self) -> list[MarketSnapshot]:
        self.tick += 1
        rows=[]
        hot_symbols={"ONDOUSDT", "STGUSDT", "HEIUSDT", "LITUSDT", "FIDAUSDT", "ENAUSDT"}
        for s in self.symbols():
            price=self._step_price(s)
            hot = s in hot_symbols
            trend = math.sin((self.tick + hash(s)%80)/6)
            amp = 1.6 if hot else 0.8
            ch5 = trend * random.uniform(0.1, 1.2) * amp
            ch15 = trend * random.uniform(0.2, 2.0) * amp + random.uniform(-0.3,0.3)
            ch1h = trend * random.uniform(0.2, 3.0) * amp + random.uniform(-0.6,0.6)
            vol_spike = max(0.2, random.lognormvariate(0.2 if hot else -0.1, 0.45))
            oi_change = trend * random.uniform(0, 1.8) * (1.3 if hot else 0.8)
            buy_ratio = max(0.05, min(0.95, 0.5 + trend*0.18 + random.uniform(-0.12,0.12)))
            sell_ratio = 1-buy_ratio
            depth = max(-1,min(1, trend*0.5 + random.uniform(-0.4,0.4)))
            rows.append(MarketSnapshot(
                symbol=s, price=round(price, 8), change_5m=round(ch5,2), change_15m=round(ch15,2), change_1h=round(ch1h,2),
                volume_spike=round(vol_spike,2), oi_change=round(oi_change,2), funding_rate=round(random.uniform(-0.0006,0.0006),6),
                taker_buy_ratio=round(buy_ratio,3), taker_sell_ratio=round(sell_ratio,3), depth_imbalance=round(depth,3),
                atr_pct=round(random.uniform(0.25,1.8),2), wick_ratio=round(random.uniform(0.05,0.55),2)
            ))
        return rows

market = MockMarketData()
