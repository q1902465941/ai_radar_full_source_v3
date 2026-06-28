# Jesse Research Environment

This project keeps Jesse in a separate research/backtest environment.
Do not install Jesse into the main `.venv` used by the live trading service.

## Create the Environment

From the repository root:

```powershell
python -m venv trading_lab\.venv_jesse
trading_lab\.venv_jesse\Scripts\python.exe -m pip install --timeout 120 --retries 10 --prefer-binary -r trading_lab\requirements-jesse.txt
```

## Verify

```powershell
trading_lab\.venv_jesse\Scripts\python.exe -m pip check
trading_lab\.venv_jesse\Scripts\jesse.exe --help
```

## Boundary

- Main trading environment: `..\.venv`
- Jesse research environment: `.venv_jesse`
- Jesse is research/backtest evidence only.
- Jesse must not place live orders from this system.
- Use `jesse run` for the Jesse research server; do not use `install-live` here.
- Backtests must include fees, slippage, candle timeframe, warmup length, long/short geometry, stops, targets, and position sizing.
