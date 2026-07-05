# Landing Status

Last updated: 2026-07-05

This document records the current evidence for landing
`E:\ai_radar_full_source_v3`.

## Current Submission State

- Local branch: `main`
- Remote: `https://github.com/q1902465941/ai_radar_full_source_v3.git`
- Current main branch landing commits have been pushed to `origin/main`.

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

Docker Compose evidence from 2026-07-05:

- `docker compose up --build -d`: completed.
- Backend container: `healthy`, published `0.0.0.0:8001->8001/tcp`.
- Frontend container: `healthy`, published `0.0.0.0:8080->80/tcp`.
- Backend log: Gunicorn listens at `http://0.0.0.0:8001` using
  `uvicorn.workers.UvicornWorker`.
- Backend smoke: `http://127.0.0.1:8001/api/v2/health` returned
  `{"ok":true,"service":"ai-radar-api","version":"v2"}`.
- Frontend smoke: `http://127.0.0.1:8080/` returned HTTP 200 and the React
  root element.

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

## Remaining To Land Fully

No known landing blockers remain. If the local network continues to block the
official Docker Hub endpoints, use the documented base image overrides before
running `docker compose up --build -d`.
