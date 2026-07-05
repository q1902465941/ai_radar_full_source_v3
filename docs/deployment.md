# Deployment

This repository has two runnable surfaces:

- Production-style stack: FastAPI v2 API (`backend.app.main:app`) plus React
  frontend.
- Legacy stack: `backend.main` via `python run.py`, kept for existing Jinja
  pages and compatibility tests.

Use the production-style stack for deployment.

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

Services:

- Frontend: `http://127.0.0.1:8080`
- Backend API: `http://127.0.0.1:8001`
- API docs: `http://127.0.0.1:8001/api/v2/docs`
- Health: `http://127.0.0.1:8001/api/v2/health`

Compose runs:

1. `python -m alembic upgrade head`
2. Gunicorn with Uvicorn workers on port `8001`
3. Nginx serving the React build on port `8080`

Persistent mounts:

- `./data:/app/data`
- `./logs:/app/logs`

The backend and frontend services both define health checks. The frontend waits
for the backend to become healthy before starting.

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

After deployment:

```bash
curl http://127.0.0.1:8001/api/v2/health
curl http://127.0.0.1:8080/
docker compose ps
```

Expected health response:

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
- Frontend logs with `docker compose logs -f frontend`
- API health endpoint `/api/v2/health`
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
3. Restart with `docker compose restart backend`.
4. Re-run the health and smoke checks.
