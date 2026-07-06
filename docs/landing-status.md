# Landing Status

Last updated: 2026-07-06

This document records the current evidence for landing
`E:\ai_radar_full_source_v3`.

## Current Submission State

- Local branch: `main`
- Remote: `https://github.com/q1902465941/ai_radar_full_source_v3.git`
- Main branch landing commits are pushed to `origin/main` when GitHub HTTPS is
  reachable from this machine. Use `git status --short --branch` as the
  authoritative local/remote divergence check.

This file is the tracking record for the submission and Docker acceptance
evidence.

## Verified Local Landing Path

Use this path when Docker Desktop is not ready:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_local.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_local_stack.ps1
```

Script paths: `scripts/verify_local.ps1`, `scripts/start_local_stack.ps1`,
and `scripts/stop_local_stack.ps1`.

Evidence from the latest local run:

- Backend tests: `373 passed`
- Frontend tests: `5 files / 8 tests passed`
- Frontend production build: passed
- Backend smoke: `/api/v2/health` returned service `ai-radar-api`
- Frontend preview smoke: app shell served from Vite preview
- Non-default local stack ports `8011/4183`: start, smoke, stop passed

Default local stack URLs:

- Frontend: `http://127.0.0.1:4173/`
- Backend health: `http://127.0.0.1:8001/api/v2/health`

## CI Verification

GitHub Actions workflow: `.github/workflows/ci.yml`

It runs on `push` to `main` and on pull requests. The workflow installs backend
and frontend dependencies, checks `docker compose config --quiet`, and runs
`scripts/verify_local.ps1`.

Latest observed branch result:

- Branch badge: `CI - passing`
- GitHub Actions run `28738839909` completed successfully for commit
  `209e034078205b67b233e97cccb2488009d94989`.
- Job `landing-verification` completed successfully, including dependency
  install, `docker compose config --quiet`, and `scripts/verify_local.ps1`.

## Docker Compose Status

Compose syntax is valid:

```powershell
docker compose config --quiet
```

Before building or starting Docker Compose, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1
```

Script path: `scripts/check_docker_prereqs.ps1`.

Docker landing verification script: `scripts/verify_docker_stack.ps1`.

Resolved machine-level issue:

- The prior WSL optional component blocker was cleared by running the helper,
  approving elevation, and rebooting.
- Docker Desktop now reports a running daemon and Compose support.
- The helper script remains available for machines that still report
  `WSL_OPTIONAL_COMPONENT_REQUIRED`:

Script path: `scripts/enable_wsl_prereq.ps1`.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_wsl_prereq.ps1
```

After running that from an elevated PowerShell and rebooting, re-run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1
docker compose up --build
```

Current Docker Hub network finding on this machine:

- Direct Docker Hub pulls for `python:3.12-slim`, `node:24-alpine`, and
  `nginx:1.29-alpine` failed while fetching anonymous tokens from
  `auth.docker.io`.
- DNS returned unreachable `2a03:2880:*:face:b00c:*` IPv6 records for Docker
  Hub endpoints, and direct host `curl` to Docker Hub timed out.
- `docker.m.daocloud.io` successfully pulled the three required base images.

Compose build supports explicit base image overrides:

```powershell
$env:PYTHON_IMAGE='docker.m.daocloud.io/library/python:3.12-slim'
$env:NODE_IMAGE='docker.m.daocloud.io/library/node:24-alpine'
$env:NGINX_IMAGE='docker.m.daocloud.io/library/nginx:1.29-alpine'
docker compose up --build -d
```

Docker Compose evidence from 2026-07-06 after restoring the detailed
monitoring site as the default browser surface, fixing Docker persistence, and
hardening mainnet market data:

- `docker compose up --build -d`: completed.
- Backend container: `healthy`, published `0.0.0.0:8001->8001/tcp`,
  running `backend.main:app`.
- v2 API container: `healthy`, published `0.0.0.0:8002->8002/tcp`,
  running `backend.app.main:app`.
- v2 API multi-worker startup: SQLite `create_all` now tolerates the
  concurrent `table already exists` race seen when both gunicorn workers boot
  at the same time.
- Frontend container: `healthy`, published `0.0.0.0:8080->80/tcp`.
- Runtime database path: `data/ai_radar.db`, backed by the mounted
  `./data:/app/data` volume. Compose now uses `DOCKER_DB_PATH` so a Windows
  host `DB_PATH=E:/...` value cannot make Linux containers write to an
  unmounted path.
- Backend smoke: `http://127.0.0.1:8080/api/state` returned market state.
- v2 API smoke: `http://127.0.0.1:8002/api/v2/health` returned
  `{"ok":true,"service":"ai-radar-api","version":"v2"}`.
- Proxied v2 smoke: `http://127.0.0.1:8080/api/v2/health` returned
  `{"ok":true,"service":"ai-radar-api","version":"v2"}`.
- Browser smoke: `http://127.0.0.1:8080/` redirects to `/radar`, renders the
  legacy `猎妖人 AI Radar` monitoring page, includes `AI RADAR SYSTEM`, and no
  longer includes `AI Radar Control Center`.
- Docker landing verification: `scripts/verify_docker_stack.ps1` completed.
- Latest Docker verification checked 8 radar prices with worst drift
  `KMNOUSDT 0.312%`, checked 77 monitor/active symbols as supported USD-M
  ASCII contracts, matched `10/10` active ticker priority candidates, reported
  paper graduation `real_closed=0/30 missing=30`, and completed the controlled
  paper closed loop.
- Latest browser smoke showed the 24h major-market cards, `Graduation`, the AI
  candidate queue, and the scan evidence matrix with no browser console errors.
- Market data: `/api/state` reported `market_data_source=mainnet`; monitored
  BTC price drift versus Binance USD-M Futures mainnet public ticker stayed
  within the verification threshold.
- Major-market details: `/api/state` now exposes `change_24h`,
  `change_source=ws_ticker_24h`, bid/ask, price age, and 24h quote volume for
  the topbar market cards. Browser evidence showed BTC/ETH/BNB/SOL rendering
  visible `24h` percentage changes instead of silent `0%` values.
- Radar pricing: `scripts/verify_docker_stack.ps1` now waits for a fresh
  `last_scan_id` before accepting `/api/radar`, then checks the first radar
  symbols against Binance mainnet ticker data with a 1% default drift limit.
  It also checks BTC 24h percentage-change drift against Binance with a 0.25
  percentage-point default limit.
- Active ticker pool: ticker anomaly discovery now ranks candidates by
  liquidity-adjusted move before they enter the active pool, and the active
  registry replaces lower-priority entries when capacity is full. The Docker
  verifier recomputes Binance USD-M Futures high-priority movers from
  `/fapi/v1/ticker/24hr` plus `/fapi/v1/exchangeInfo` and checks that the
  local active pool covers the top external candidates. It also rejects
  monitor symbols that are not supported USD-M ASCII contracts. Latest
  evidence: top external coverage `10/10`, symbol support check `77`.
- Graduation visibility: `/api/system/readiness` exposes
  `paper_learning.graduation_progress`, including real closed samples with
  radar context, required sample count, missing sample count, replay ratio,
  market-backtest availability, and the next requirement. The monitor
  readiness cards render this as `Graduation` so the live blocker is a visible
  evidence gap, not an opaque `DEGRADED` status.
- WebSocket ticker source: all-market ticker uses the Binance USD-M Futures
  `/market/ws/!ticker@arr` routed path and filters post-CM-migration ticker
  rows to USD-M rows (`st=1`). Runtime evidence after rebuild showed
  `refresh_source=ws_ticker`, `market_refresh.degraded=false`, and no
  non-ASCII symbols in radar top50 or active pool.
- Radar scan: `/api/radar/scan-now` and `/api/radar` returned non-empty top50
  data with `market_refresh.degraded=false`, `refresh_source=ws_ticker`,
  active pool `120`, and top50 count `50`.

## Controlled Paper Closed Loop

The current Docker runtime is configured for local rule-based paper sampling,
not Codex-required entry:

```env
AI_ENABLED=false
AI_STRATEGY_PROVIDER=rule
REQUIRE_CODEX_STRATEGY_FOR_ENTRY=false
LIVE_TRADING_ENABLED=false
```

Evidence from 2026-07-06:

- `/api/system/readiness` status: `DEGRADED`, not `BLOCKED`. Latest check after
  Docker verification showed 6 readiness blockers, all still visible as
  wait/live-graduation gates rather than Docker startup failure.
- Codex entry gate: `required_for_entry=false`.
- Paper loop guard: `ok=true`, reason `paper_closed_loop_sampling`.
- Codex-related wait/paper-entry blockers: none.
- Paper graduation progress is visible in readiness. Current mounted Docker DB
  evidence after the latest rebuild showed `real_closed=0/30`, `missing=30`,
  and trust `LOW`, so the paper/shadow loop is usable while live graduation
  remains blocked.
- If a normal paper position is already open, readiness can still report
  `ai_not_invoked` or `open_position_exists`; that is a capacity/position
  management wait state, not a Codex or market-data startup blocker.
- Controlled paper acceptance endpoint:
  `/api/trade-director/acceptance/paper-cycle`.
- Acceptance result: `ok=true`, `real_order_allowed=false`.
- Completed stages: scan candidate, cyqnt evidence, strategy plan, risk model,
  paper open, position manager, paper close, learning open recorded, and
  learning close recorded.
- The Docker verifier checks `open_test_positions_after=[]`; a separate normal
  paper position can still exist while the controlled acceptance position is
  opened, closed, and recorded.

## Safety State

Default execution remains paper-only:

```env
TRADE_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_USE_TEST_ORDER=true
ATTACH_PROTECTION_ORDERS=true
```

Live order paths remain guarded by live readiness, PRG, exchange
reconciliation, production acceptance, and protection-order checks.

## Remaining To Graduate Beyond Paper

The runnable paper closed loop is now landed. Full live graduation is still
blocked by readiness conditions that should not be bypassed:

- Learning data is not production-grade yet.
- Closed paper/live sample count is still low for live readiness.
- Paper win rate, recent win rate, and PnL gates still require more evidence.
- Exchange reconciliation and PRG live eligibility still need clean evidence.

If the local network continues to block the official Docker Hub endpoints, use
the documented base image overrides before running `docker compose up --build -d`.
