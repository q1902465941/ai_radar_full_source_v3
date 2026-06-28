from __future__ import annotations
from dataclasses import dataclass, asdict
import time

@dataclass
class WaitState:
    symbol: str
    strategy_id: str
    wait_type: str
    wait_rounds: int
    first_wait_ms: int
    last_check_ms: int
    wait_start_price: float
    wait_start_score: float
    wait_start_rank: int
    status: str

class WaitManager:
    def __init__(self):
        self.states: dict[str, WaitState] = {}

    def evaluate(self, item, plan):
        key=item.symbol
        now=int(time.time()*1000)
        st=self.states.get(key)
        if not st:
            st=WaitState(item.symbol, plan.strategy_id, plan.wait_type or "WAIT_FOR_CONFIRMATION",0,now,now,item.price,item.score,item.rank,"WAIT_ACTIVE")
        st.wait_rounds += 1
        st.last_check_ms = now
        # hard expiration
        age_s=(now-st.first_wait_ms)/1000
        if st.wait_rounds >= 5 or age_s > max(60, plan.expire_after_seconds):
            st.status="WAIT_EXPIRED"
            self.states.pop(key, None)
            return {"decision":"EXPIRED", "reason":"wait_timeout_or_max_rounds", "state":asdict(st)}
        # decay conditions
        if item.fake_breakout_risk == "HIGH" or item.fund_confirm_count < min(3, item.fund_confirm_total) or item.rank > 25:
            st.status="WAIT_EXPIRED"; self.states.pop(key, None)
            return {"decision":"EXPIRED", "reason":"signal_decayed", "state":asdict(st)}
        # paper observe if still strong but AI waits too long
        if st.wait_rounds >= 3 and item.score >= 60 and item.fund_confirm_count >= min(3, item.fund_confirm_total):
            st.status="WAIT_DECAYING"; self.states[key]=st
            return {"decision":"PAPER_OBSERVE", "reason":"wait_too_long_but_signal_valid", "state":asdict(st)}
        self.states[key]=st
        return {"decision":"KEEP_WAITING", "reason":"wait_active", "state":asdict(st)}

wait_manager = WaitManager()
