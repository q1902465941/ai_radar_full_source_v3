from backend.models import MarketSnapshot, Direction

def dealer_label(s: MarketSnapshot, direction: Direction, sm_delta: float, fund_confirm: int, fake_risk: str) -> str:
    if direction == "LONG" and s.change_5m > 0 and s.oi_change > 0 and s.taker_buy_ratio > 0.55 and sm_delta >= 0:
        return "多延"
    if direction == "SHORT" and s.change_5m < 0 and s.oi_change > 0 and s.taker_sell_ratio > 0.55 and sm_delta <= 0:
        return "空延"
    if direction == "LONG" and fund_confirm <= 1 and s.wick_ratio > 0.35:
        return "多诱"
    if direction == "SHORT" and fund_confirm <= 1 and s.wick_ratio > 0.35:
        return "空诱"
    if abs(s.change_5m) > 1.5 and s.wick_ratio > 0.45:
        return "洗盘"
    if abs(s.change_15m) < 0.35 and s.oi_change > 0.4 and s.volume_spike > 1.4:
        return "吸筹"
    if s.volume_spike > 2.5 and abs(s.change_5m) < 0.25 and sm_delta < 0:
        return "派发"
    return "中性"
