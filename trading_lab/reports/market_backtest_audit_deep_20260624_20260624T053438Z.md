# Market Backtest

- generated_at: 2026-06-24T05:34:38.538000+00:00
- passed: False
- blockers: market_backtest_win_rate_low, market_backtest_holdout_win_rate_low, market_backtest_profit_factor_low, market_backtest_net_pnl_not_positive, market_backtest_holdout_pnl_not_positive
- symbols_loaded: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, SPCXUSDT, MUUSDT, SOXLUSDT, SNDKUSDT, XAGUSDT, XAUUSDT, HEIUSDT, BEATUSDT, SLXUSDT, SYNUSDT, BLESSUSDT, QNTXUSDT, ARXUSDT, HYPEUSDT, SKHYNIXUSDT, ZECUSDT
- span_days: 14.0
- signals: 421
- trades: 241
- win_rate: 0.4938
- profit_factor: 0.7096
- net_pnl_r: -41.192615
- holdout_trades: 73
- holdout_win_rate: 0.4384
- holdout_net_pnl_r: -21.346358

Policy: closed signal candle, next candle open entry, fees and slippage included, TP1 closes half, Stage 2 uses locked stop and ATR trailing, ambiguous SL/TP candles count as stop first.