# Deployment

This repository has two runnable surfaces:

- Production-style stack: FastAPI v2 API (`backend.app.main:app`) plus React
  frontend migration surface.
- Legacy stack: `backend.main` via `python run.py`, kept for existing Jinja
  monitoring pages and compatibility tests.

Use Docker Compose for deployment. It exposes the detailed legacy monitoring
site on `8080` and runs the v2 API as a parallel service so migration checks
remain available.

## Prerequisites

- Python 3.12
- Node.js compatible with `frontend/package-lock.json`
- Docker and Docker Compose for container deployment
- A local `.env` created from `.env.example`

Do not commit `.env`, runtime databases, logs, or build outputs.

## Local Runbook

Backend:

```bash
cd E:\ai_radar_full_source_v3
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run_v2.py
```

Frontend:

```bash
cd E:\ai_radar_full_source_v3\frontend
npm ci
npm run dev
```

Production-style local stack without Docker:

```powershell
cd E:\ai_radar_full_source_v3
powershell -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_local_stack.ps1
```

If ports are occupied, pass `-BackendPort` and `-FrontendPort` to both scripts.

Local URLs:

- Frontend dev server: `http://127.0.0.1:5173`
- Frontend local stack: `http://127.0.0.1:4173`
- Backend health: `http://127.0.0.1:8001/api/v2/health`
- Backend docs: `http://127.0.0.1:8001/api/v2/docs`

## Docker Compose

Build and start:

```bash
cd E:\ai_radar_full_source_v3
docker compose up --build
```

If the local network resolves Docker Hub endpoints incorrectly or blocks
anonymous pulls, set base image overrides before building. This keeps the
Dockerfiles on official defaults while allowing a verified mirror path:

```powershell
$env:PYTHON_IMAGE='docker.m.daocloud.io/library/python:3.12-slim'
$env:NODE_IMAGE='docker.m.daocloud.io/library/node:24-alpine'
$env:NGINX_IMAGE='docker.m.daocloud.io/library/nginx:1.29-alpine'
docker compose up --build -d
```

Services:

- Monitoring site: `http://127.0.0.1:8080`
- Legacy backend API: `http://127.0.0.1:8001`
- Backend v2 API: `http://127.0.0.1:8002`
- v2 API docs: `http://127.0.0.1:8002/api/v2/docs`
- v2 health: `http://127.0.0.1:8002/api/v2/health`
- Proxied v2 health: `http://127.0.0.1:8080/api/v2/health`

Compose runs:

1. `python -m alembic upgrade head`
2. Legacy monitoring app `backend.main:app` on port `8001`
3. v2 API app `backend.app.main:app` on port `8002`
4. Nginx on port `8080`, proxying the monitoring site by default and
   `/api/v2/` to the v2 API

Persistent mounts:

- `./data:/app/data`
- `./logs:/app/logs`

The backend, api-v2, and frontend services define health checks. The frontend
waits for both backend services to become healthy before starting.

## Required Verification

Before deployment:

```bash
.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test -- --run
npm run build
```

Equivalent scripted local verification:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_local.ps1
```

Before running Compose on Windows, check Docker Desktop and WSL prerequisites:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1
```

If the check reports `WSL_OPTIONAL_COMPONENT_REQUIRED`, run the helper below,
approve the Windows elevation prompt, reboot, and then run the prerequisite
check again:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_wsl_prereq.ps1
```

After deployment:

```bash
curl http://127.0.0.1:8080/radar
curl http://127.0.0.1:8080/api/state
curl http://127.0.0.1:8002/api/v2/health
curl http://127.0.0.1:8080/api/v2/health
docker compose ps
```

Expected v2 health response:

```json
{"ok":true,"service":"ai-radar-api","version":"v2"}
```

## Safety Gates

Default `.env.example` keeps live trading disabled:

```env
TRADE_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_USE_TEST_ORDER=true
ATTACH_PROTECTION_ORDERS=true
```

Real live execution must pass:

- Live readiness phase checks
- PRG scoring
- Exchange reconciliation
- Production acceptance evidence
- Protection-order requirements

Scanning is not trading. Scan and strategy-alpha output are evidence sources,
not permission to place live orders.

## Observability

Watch:

- Container health in `docker compose ps`
- Backend logs with `docker compose logs -f backend`
- v2 API logs with `docker compose logs -f api-v2`
- Frontend logs with `docker compose logs -f frontend`
- Monitoring state endpoint `/api/state`
- v2 API health endpoint `/api/v2/health`
- Readiness-related API output before any supervised live validation

## Rollback

For a bad local/container deployment:

```bash
docker compose down
git log --oneline -5
git revert <bad_commit_sha>
docker compose up --build
```

For config-caused issues:

1. Set `LIVE_TRADING_ENABLED=false`.
2. Restore the previous `.env` values.
3. Restart with `docker compose restart backend api-v2 frontend`.
4. Re-run the health and smoke checks.
