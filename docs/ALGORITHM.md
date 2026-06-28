# 算法结构说明

## 核心一：雷达扫描算法

1. MarketSnapshot：价格、5m/15m/1h、成交量、OI、Funding、主动买卖、盘口、ATR、影线。
2. DirectionModel：综合价格方向、OI、主动买卖、盘口，输出 LONG/SHORT/NEUTRAL。
3. ScoreEngine：趋势、成交量、波动率、OI、主动成交、多周期、SM、升温、假突破扣分。
4. HeatTracker：保存每个 symbol 最近8轮 score/rank/oi/sm。
5. SlopeCalculator：线性斜率计算 heat_slope 与 slope_score。
6. FundConfirm：成交量确认 + OI确认 + 主动成交确认，输出 0/3-3/3。
7. FakeBreakout：突破但资金不确认、OI不确认、长影线、Funding过热等。
8. SmartMoney：大资金参与度估算，不是真实交易所字段。
9. DealerRadar：多延/空延/多诱/空诱/洗盘/吸筹/派发/中性。
10. CandidateSelector：Top50展示，Top4/Top5进入 AI。

## 核心二：自动交易 + 自动持仓管理

1. ContextCompressor：只给 AI 单个候选摘要，避免上下文过大。
2. StrategyGenerator：OpenAI/Codex运行时接口；无 Key 时规则策略生成。
3. StrategyValidator：系统保护校验，防 entry/sl/tp 错误。
4. WaitManager：WAIT_ACTIVE / WAIT_DECAYING / WAIT_EXPIRED，避免无限 WAIT。
5. AutoTradingRiskModel：删除固定默认风控，动态计算 margin/leverage/quantity/mode。
6. PaperExecutor / LiveExecutor：执行。
7. PositionManager：Stage1/Stage2，TP1部分止盈、锁盈、TP2、SL、移动止损、手动平仓。
