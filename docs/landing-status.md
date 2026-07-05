# Landing Status

Last updated: 2026-07-05

This document records the current evidence for landing
`E:\ai_radar_full_source_v3`.

## Current Submission State

- Local branch: `main`
- Remote: `https://github.com/q1902465941/ai_radar_full_source_v3.git`
- Previous main branch landing commits have been pushed to `origin/main`.
- The Docker monitor restoration commit is local and pending push because
  GitHub HTTPS access from this machine is currently failing with connection
  resets/timeouts.

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

Evidence from the latest run:

- Backend tests: `341 passed`
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

Docker Compose evidence from 2026-07-05 after restoring the detailed
monitoring site as the default browser surface and fixing mainnet market data:

- `docker compose up --build -d`: completed.
- Backend container: `healthy`, published `0.0.0.0:8001->8001/tcp`,
  running `backend.main:app`.
- v2 API container: `healthy`, published `0.0.0.0:8002->8002/tcp`,
  running `backend.app.main:app`.
- Frontend container: `healthy`, published `0.0.0.0:8080->80/tcp`.
- Backend smoke: `http://127.0.0.1:8080/api/state` returned market state.
- v2 API smoke: `http://127.0.0.1:8002/api/v2/health` returned
  `{"ok":true,"service":"ai-radar-api","version":"v2"}`.
- Proxied v2 smoke: `http://127.0.0.1:8080/api/v2/health` returned
  `{"ok":true,"service":"ai-radar-api","version":"v2"}`.
- Browser smoke: `http://127.0.0.1:8080/` redirects to `/radar`, renders the
  legacy `猎妖人 AI Radar` monitoring page, includes `AI RADAR SYSTEM`, and no
  longer includes `AI Radar Control Center`.
- Docker landing verification: `scripts/verify_docker_stack.ps1` completed.
- Market data: `/api/state` reported `market_data_source=mainnet`; monitored
  BTC price drift versus Binance USD-M Futures mainnet public ticker stayed
  within the verification threshold.
- Radar scan: `/api/radar/scan-now` and `/api/radar` returned non-empty top50
  data with `market_refresh.degraded=false`.

## Controlled Paper Closed Loop

The current Docker runtime is configured for local rule-based paper sampling,
not Codex-required entry:

```env
AI_ENABLED=false
AI_STRATEGY_PROVIDER=rule
REQUIRE_CODEX_STRATEGY_FOR_ENTRY=false
LIVE_TRADING_ENABLED=false
```

Evidence from 2026-07-05:

- `/api/system/readiness` status: `DEGRADED`, not `BLOCKED`.
- Codex entry gate: `required_for_entry=false`.
- Paper loop guard: `ok=true`, reason `paper_closed_loop_sampling`.
- Codex-related wait/paper-entry blockers: none.
- If a normal paper position is already open, readiness can still report
  `ai_not_invoked` or `open_position_exists`; that is a capacity/position
  management wait state, not a Codex or market-data startup blocker.
- Controlled paper acceptance endpoint:
  `/api/trade-director/acceptance/paper-cycle`.
- Acceptance result: `ok=true`, `real_order_allowed=false`.
- Completed stages: scan candidate, cyqnt evidence, strategy plan, risk model,
  paper open, position manager, paper close, learning open recorded, and
  learning close recorded.

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
