# Market Backtest

- generated_at: 2026-06-27T07:56:32.221000+00:00
- passed: True
- blockers: none
- symbols_loaded: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, HYPEUSDT, ZECUSDT, XRPUSDT, AGLDUSDT, LABUSDT, AAVEUSDT, VELVETUSDT, MYXUSDT, AINUSDT, GUAUSDT, PUNDIXUSDT, AIOUSDT, MAGMAUSDT, SLXUSDT, DOGEUSDT, WLDUSDT
- span_days: 14.0
- signals: 50
- trades: 30
- win_rate: 0.7
- profit_factor: 1.7799
- net_pnl_r: 8.623432
- holdout_trades: 9
- holdout_win_rate: 0.7778
- holdout_net_pnl_r: 4.64873

Policy: closed signal candle, next candle open entry, fees and slippage included, TP1 closes half, Stage 2 uses locked stop and ATR trailing, ambiguous SL/TP candles count as stop first.