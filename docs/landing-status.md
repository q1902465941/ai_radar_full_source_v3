# Landing Status

Last updated: 2026-07-05

This document records the current evidence for landing
`E:\ai_radar_full_source_v3` and the remaining machine-level blocker.

## Current Submission State

- Local branch: `main`
- Remote: `https://github.com/q1902465941/ai_radar_full_source_v3.git`
- Code landing commits through `ci: add landing verification workflow` have
  been pushed to `origin/main`.

This file is the tracking record for the submission and remaining machine-level
blocker.

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

Current blocker on this machine:

- WSL optional component is unavailable.
- Docker Desktop daemon/API returns 500 for `docker info`.
- The helper script can request elevation and run the required WSL command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_wsl_prereq.ps1
```

After running that from an elevated PowerShell and rebooting, re-run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1
docker compose up --build
```

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

1. Run `scripts/enable_wsl_prereq.ps1`, approve elevation, and reboot the
   machine.
2. Verify Docker prerequisites pass.
3. Run `docker compose up --build`.
4. Smoke test `http://127.0.0.1:8080/` and
   `http://127.0.0.1:8001/api/v2/health`.
