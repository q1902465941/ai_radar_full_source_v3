from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional, Any
import time, uuid

Direction = Literal["LONG", "SHORT", "NEUTRAL"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
Action = Literal["WAIT", "OPEN_LONG", "OPEN_SHORT", "PAPER_OBSERVE"]


def now_ms() -> int:
    return int(time.time() * 1000)

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    change_5m: float
    change_15m: float
    change_1h: float
    volume_spike: float
    oi_change: float
    funding_rate: float
    taker_buy_ratio: float
    taker_sell_ratio: float
    depth_imbalance: float
    atr_pct: float
    wick_ratio: float
    structure_metrics: dict[str, Any] = field(default_factory=dict)
    ts_ms: int = field(default_factory=now_ms)

@dataclass
class RadarItem:
    rank: int
    symbol: str
    base_asset: str
    price: float
    direction: Direction
    stage: str
    trigger_mode: str
    score: float
    score_history: list[float]
    rank_history: list[int]
    heat_slope: float
    slope_score: float
    fake_breakout_risk: RiskLevel
    change_5m: float
    change_15m: float
    change_1h: float
    oi_change: float
    fund_confirm_count: int
    fund_confirm_total: int
    dealer_radar: str
    sm_position: float
    sm_delta: float
    volume_spike: float = 0.0
    funding_rate: float = 0.0
    taker_buy_ratio: float = 0.5
    taker_sell_ratio: float = 0.5
    depth_imbalance: float = 0.0
    atr_pct: float = 0.0
    wick_ratio: float = 0.0
    ai_candidate: bool = False
    score_features: dict[str, Any] = field(default_factory=dict)
    score_explain: dict[str, Any] = field(default_factory=dict)
    market_structure: dict[str, Any] = field(default_factory=dict)
    ts_ms: int = field(default_factory=now_ms)

    def asdict(self):
        return asdict(self)

@dataclass
class StrategyPlan:
    strategy_id: str
    action: Action
    symbol: str
    side: Direction
    entry_zone_low: float
    entry_zone_high: float
    ideal_entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    confidence: float
    reason: str
    wait_type: str = ""
    expire_after_seconds: int = 180
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass
class ExecutionPlan:
    decision: str
    mode: str
    symbol: str
    side: Direction
    dynamic_margin: float
    dynamic_leverage: int
    quantity: float
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp1_close_ratio: float
    tp2_close_ratio: float
    management_mode: str
    cooldown_after_trade: int
    reason: str
    notional: float = 0.0
    risk_usdt: float = 0.0
    risk_pct: float = 0.0
    strategy_contract: dict[str, Any] = field(default_factory=dict)

@dataclass
class PositionDecision:
    ts_ms: int
    action: str
    reason: str
    defense_level: str
    thesis_alive: bool
    adverse_r: float
    favorable_r: float
    mfe_r: float
    mae_r: float
    noise_budget_pct: float
    noise_budget_r: float
    reduce_ratio: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def asdict(self):
        return asdict(self)

@dataclass
class PositionPolicyReview:
    ts_ms: int
    action: str
    thesis_alive: bool
    confidence: float
    reason: str
    noise_interpretation: str
    invalidation: str
    reduce_ratio: float = 0.0
    stop_loss: float = 0.0
    learning_note: str = ""
    safety_note: str = ""
    provider: str = ""
    status: str = "ok"

    def asdict(self):
        return asdict(self)

@dataclass
class Position:
    position_id: str
    strategy_id: str
    source_signal_id: str
    symbol: str
    side: Direction
    status: str
    stage: str
    score: float
    entry_price: float
    current_price: float
    quantity: float
    initial_quantity: float
    margin: float
    leverage: int
    stop_loss: float
    tp1: float
    tp2: float
    best_price: float
    initial_stop_loss: float = 0.0
    initial_risk_unit: float = 0.0
    notional: float = 0.0
    entry_fee: float = 0.0
    realized_fee: float = 0.0
    realized_gross_pnl: float = 0.0
    risk_usdt: float = 0.0
    risk_pct: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    roi: float = 0.0
    price_source: str = ""
    price_age_seconds: float = 0.0
    price_stale: bool = False
    price_error: str = ""
    price_bid: float = 0.0
    price_ask: float = 0.0
    last_price_update_ms: int = 0
    close_reason: str = ""
    open_time: int = field(default_factory=now_ms)
    close_time: Optional[int] = None
    strategy_contract: dict[str, Any] = field(default_factory=dict)
    lock_status: str = "初始止损"
    lifecycle_state: str = "PROTECTING"
    mfe: float = 0.0
    mae: float = 0.0
    defense_level: str = "NORMAL"
    thesis_alive: bool = True
    noise_budget_pct: float = 0.0
    noise_budget_r: float = 0.0
    adverse_r: float = 0.0
    favorable_r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    last_decision: dict[str, Any] = field(default_factory=dict)
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    last_ai_review: dict[str, Any] = field(default_factory=dict)
    ai_review_log: list[dict[str, Any]] = field(default_factory=list)
    exchange_open_order: dict[str, Any] = field(default_factory=dict)
    exchange_stop_order: dict[str, Any] = field(default_factory=dict)
    exchange_tp_order: dict[str, Any] = field(default_factory=dict)
    exchange_close_order: dict[str, Any] = field(default_factory=dict)

    def asdict(self):
        return asdict(self)

@dataclass
class ClosedPosition:
    position_id: str
    strategy_id: str
    symbol: str
    side: Direction
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    pnl: float
    roi: float
    close_reason: str
    score_at_entry: float
    open_time: int
    close_time: int
    source_signal_id: str
    notional: float = 0.0
    gross_pnl: float = 0.0
    fee: float = 0.0
    risk_usdt: float = 0.0
    risk_pct: float = 0.0
    cost_model_version: str = "net_v1"
    strategy_contract: dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "CLOSED"
    mfe: float = 0.0
    mae: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    hold_time_ms: int = 0
    exit_decision: dict[str, Any] = field(default_factory=dict)
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    last_ai_review: dict[str, Any] = field(default_factory=dict)
    ai_review_log: list[dict[str, Any]] = field(default_factory=list)
    exchange_open_order: dict[str, Any] = field(default_factory=dict)
    exchange_stop_order: dict[str, Any] = field(default_factory=dict)
    exchange_tp_order: dict[str, Any] = field(default_factory=dict)
    exchange_close_order: dict[str, Any] = field(default_factory=dict)

    def asdict(self):
        return asdict(self)
