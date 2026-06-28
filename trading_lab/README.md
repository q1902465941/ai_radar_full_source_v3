# Trading Lab

Lifecycle rule: scanning is not trading. Scan output is evidence for a candidate, not a buy/sell/close command. See `position_lifecycle.md`.

这个目录是 AI Radar 的策略研究仓库。它的目的不是让 Codex 预测市场，而是让 Codex 严格执行一套策略研究流程：先提出市场假设，再设计验证方法，最后由风控反方审查。

每个策略必须产出研究报告。禁止只产出收益曲线，禁止只凭技术指标给出买卖结论。

## 目录

- `principles.md`: 交易研究原则。
- `strategy_template.md`: 每个策略必须使用的研究报告模板。
- `risk_rules.md`: 风控、仓位、实盘晋级规则。
- `codex_strategy_research_prompt.md`: 策略 AI 的固定行为提示词。
- `backtester/`: 回测框架和回测规范。
- `data/`: 数据口径、样本定义、数据质量说明。
- `reports/`: 每个策略的研究报告。

## 强制三分法

每个策略必须区分三件事：

1. 信号：什么时候入场，信号来自什么市场机制。
2. 风险：判断错了亏多少，什么时候必须承认失效。
3. 执行：如何真实成交，成本、滑点、盘口流动性是否允许。

很多亏损系统的问题不是没有信号，而是只有信号，没有风险和执行。

## 固定审查角色

每个策略必须同时通过两个角色：

- 角色 A：策略研究员。提出市场假设、收益来源、验证方法。
- 角色 B：风控负责人。专门找策略会死在哪里。

好的交易系统不是因为它看起来聪明，而是因为它知道自己什么时候很蠢。

## 策略晋级

一个策略从研究到真实交易必须经过：

1. `research`: 完成研究报告。
2. `backtest`: 完成样本内回测和基础成本建模。
3. `out_of_sample`: 完成样本外验证。
4. `paper_probe`: 小心采样，验证信号是否还能工作。
5. `paper_formal`: 正式纸面闭环，统计胜率、盈亏比、回撤。
6. `shadow_live`: 只观察真实行情和盘口，不真实下单。
7. `live_test_order`: 仅在用户明确批准后进行极小实盘测试单。

任何阶段失败，都必须回到研究报告里解释原因，而不是直接调低风控阈值。
