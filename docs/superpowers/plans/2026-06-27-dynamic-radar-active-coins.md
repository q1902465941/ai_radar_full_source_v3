# Dynamic Radar Active Coins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn radar discovery into full-market anomaly detection plus a short-lived active coin watchlist.

**Architecture:** Keep all-market ticker discovery cheap, store abnormal symbols in an `ActiveCoinRegistry`, expose dynamic stream intent through a manager, and let `RadarEngine` rank only fresh active candidates with explicit lifecycle diagnostics.

**Tech Stack:** Python 3.12, FastAPI, pytest, Binance Futures REST/WebSocket data.

## Global Constraints

- Do not send orders from radar discovery.
- Full-market discovery must use ticker rows before expensive kline/depth/OI calls.
- Active candidates expire after an idle timeout and enter cooldown after removal.
- Existing `RadarItem.market_structure` and website structure evidence must keep working.

---

### Task 1: Active Coin Registry

**Files:**
- Create: `backend/radar/active_coins.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `ActiveCoinRegistry.update_candidates(symbols, now, reason_by_symbol=None) -> list[ActiveCoin]`
- Produces: `ActiveCoinRegistry.expire_idle(now) -> list[ActiveCoin]`
- Produces: `ActiveCoinRegistry.active_symbols() -> list[str]`
- Produces: `ActiveCoinRegistry.diagnostics() -> dict`

- [ ] Write tests for add, refresh, idle expiry, cooldown.
- [ ] Run targeted tests and see them fail.
- [ ] Implement registry.
- [ ] Run targeted tests and see them pass.

### Task 2: Dynamic Stream Manager

**Files:**
- Create: `backend/market/dynamic_symbol_stream.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `ActiveCoinRegistry.active_symbols()`
- Produces: `dynamic_symbol_stream.sync(symbols, now) -> dict`
- Produces: `dynamic_symbol_stream.diagnostics() -> dict`

- [ ] Write tests for subscribe/unsubscribe bookkeeping.
- [ ] Implement stream lifecycle bookkeeping first; real socket consumers can attach to the same state later.
- [ ] Run targeted tests.

### Task 3: Discovery Integration

**Files:**
- Modify: `backend/market/binance_factor_source.py`
- Modify: `backend/radar/radar_engine.py`
- Modify: `backend/main.py`
- Test: `tests/test_core.py`

**Interfaces:**
- `BinanceFactorSource.get_snapshots()` updates active coins from ticker rows before selecting expensive symbols.
- `RadarEngine.scan_status()` includes active coin diagnostics.
- `/api/radar` includes active coin diagnostics.

- [ ] Write tests that ticker anomalies enter active coins and stale symbols expire.
- [ ] Implement candidate discovery using 24h ticker rows plus settings thresholds.
- [ ] Use active symbols as priority input for snapshot loading.
- [ ] Expose diagnostics.
- [ ] Run full tests.

### Task 4: Website Visibility

**Files:**
- Modify: `backend/web/static/app.js`
- Modify: `backend/web/static/app.css`
- Test: `node --check backend/web/static/app.js`

**Interfaces:**
- Consumes: `data.active_coins` from `/api/radar`
- Shows active count and stream intent.

- [ ] Add a compact active coin summary to the radar page.
- [ ] Run JS syntax check.
- [ ] Verify `/radar` in browser.
