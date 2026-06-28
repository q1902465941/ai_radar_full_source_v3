# 风控规则

Lifecycle risk rule: scanning is not trading. A minor reverse signal can move a position into DEFENSIVE, but it cannot close the core position unless the thesis is invalidated, risk limits fire, time stop fires, TP2 is hit, or market structure breaks.

## 硬性禁止

- 禁止没有市场假设的策略进入交易。
- 禁止只有信号、没有风险和执行的策略进入交易。
- 禁止只根据技术指标直接给出买卖结论。
- 禁止用样本内回测直接证明可实盘。
- 禁止亏损后通过放宽风控来制造开仓。
- 禁止手续费和滑点未覆盖时开仓。
- 禁止在恢复模式下扩大仓位或增加开仓数量。

## 成本约束

策略必须证明预期收益覆盖：

- 双边 taker 手续费。
- 双边滑点。
- 止损距离带来的亏损。
- 盘口深度不足导致的额外成交成本。

如果目标收益不足以覆盖成本，结论必须是 `WAIT` 或 `REJECT`。

## 仓位约束

- 初始阶段最大同时持仓数为 1。
- 单笔亏损必须先定义，再谈收益。
- 禁止加仓摊平亏损。
- 允许扩大仓位前，必须有稳定样本外表现。
- 连续亏损后必须降级观察，而不是反向重仓。

## 阶段晋级

策略只能按以下顺序晋级：

1. `research`
2. `backtest`
3. `out_of_sample`
4. `paper_probe`
5. `paper_formal`
6. `shadow_live`
7. `live_test_order`

进入下一阶段前必须有报告记录。

## 反方审查否决权

只要风控负责人能提出一个无法被数据解释的致命风险，该策略必须停留在研究或纸面阶段。

典型否决理由：

- 胜率高但盈亏比过低。
- 收益高但回撤和杠杆不可接受。
- 样本太少。
- 只在单个币种有效。
- 成本模型太乐观。
- 高频信号没有盘口流动性验证。
- 日线或隔夜策略没有处理跳空和资金费率。
- 策略依赖瞬时异动，但执行假设无法成交。
