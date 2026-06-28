# AI Radar Project Instructions

When working on trading strategy logic in this project, treat Codex as a strict strategy research process executor, not a market predictor.

Any strategy analysis must first explain the market hypothesis, then design the validation method. Do not give buy/sell conclusions only from technical indicators.

Every strategy must separate:

- Signal: when to enter and what market mechanism supports the entry.
- Risk: how much can be lost, where the idea is wrong, and when to stop.
- Execution: how a real fill would happen after fees, slippage, liquidity, and position limits.
- Position lifecycle: how the trade is managed after entry, including hold, reduce, add, exit, time stop, and review.

Scanning is not trading. Scan results are evidence for candidate opportunities, not buy/sell commands.

Every strategy must include source of return, failure conditions, trading cost, slippage, position sizing, maximum drawdown, out-of-sample testing, overfitting risk, hold logic, reduce logic, add logic, exit logic, time stop, and review metrics. If the logic cannot be explained clearly, state that the strategy has insufficient trading basis.

Do not close the core position for a minor reverse signal alone. Exit only when the trade thesis is invalidated, risk limits fire, time stop fires, or market structure breaks.

Use MFE, MAE, R_multiple, max drawdown, and hold time to learn whether the system exited too early, held losers too long, or used the wrong stop.

Use two fixed review roles:

- Role A: strategy researcher, proposing the logic and validation plan.
- Role B: risk officer, finding where the strategy can fail.

Every researched strategy must produce a report under `trading_lab/reports/` using `trading_lab/strategy_template.md`.

## Event-Driven Hedge Fund Backend Rules

When upgrading the event-driven trading backend:

1. Do not change UI, frontend, dashboard pages, web templates/static files, or public API schemas.
2. Every executable strategy must be registered through the strategy universe.
3. Execution is live-only when enabled: real Binance Futures orders require `TRADE_MODE=live`, `LIVE_TRADING_ENABLED=true`, and `LIVE_USE_TEST_ORDER=false`.
4. Learning updates must be driven by trade events, not raw scan output.
5. Portfolio state must update immediately from trade events.
6. Risk parity controls capital allocation across ranked strategies.
7. Dynamic leverage controls risk exposure from market regime and drawdown.
