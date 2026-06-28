def trigger_mode(score, slope_score, volume_spike, oi_change, fake_risk):
    if fake_risk == "HIGH": return "假突破预警"
    if slope_score >= 80: return "评分加速"
    if volume_spike >= 2.6: return "量能点火"
    if abs(oi_change) >= 1.3: return "主力异动"
    if score >= 70: return "趋势突破"
    return "常规监控"

def stage_for(prev_score_hist, score, fund_confirm, trigger):
    if len(prev_score_hist) < 1: return "新出现"
    if trigger in ["评分加速", "量能点火"] and fund_confirm >= 2: return "确认中"
    if len(prev_score_hist) >= 2 and score > prev_score_hist[-1] > prev_score_hist[-2]: return "升温中"
    return "观察中"
