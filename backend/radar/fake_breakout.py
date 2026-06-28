from backend.models import MarketSnapshot, Direction

def fake_breakout(snapshot: MarketSnapshot, direction: Direction) -> tuple[str,float]:
    breakout = abs(snapshot.change_5m) > 0.7 or abs(snapshot.change_15m) > 1.2
    wick_ratio = _current_wick_ratio(snapshot)
    score=0
    if breakout and snapshot.volume_spike < 1.4: score += 25
    if breakout and snapshot.oi_change <= 0: score += 25
    if direction == "LONG" and snapshot.taker_buy_ratio < 0.50: score += 20
    if direction == "SHORT" and snapshot.taker_sell_ratio < 0.50: score += 20
    if wick_ratio > 0.45: score += 20
    if abs(snapshot.funding_rate) > 0.0005: score += 10
    if score < 30: return "LOW", score
    if score < 65: return "MEDIUM", score
    return "HIGH", score


def _current_wick_ratio(snapshot: MarketSnapshot) -> float:
    metrics = getattr(snapshot, "structure_metrics", {}) or {}
    if isinstance(metrics, dict) and "current_wick_ratio" in metrics:
        try:
            return max(0.0, float(metrics.get("current_wick_ratio") or 0.0))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(snapshot.wick_ratio or 0.0))
