# AI Radar Full Source v3

AI Radar is a local trading research and execution-control system for radar
scans, strategy research, paper/live readiness gates, and an operational
React dashboard.

The production-style path is the v2 API and React frontend:

- Backend API: `backend.app.main:app`
- Local backend entry: `python run_v2.py`
- Frontend app: `frontend/`
- Docker Compose entry: `docker compose up --build`

The legacy Jinja app still exists at `python run.py` for old pages and
compatibility checks.

## Local Development

Backend:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run_v2.py
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

Open:

- Frontend dev app: `http://127.0.0.1:5173`
- Backend health: `http://127.0.0.1:8001/api/v2/health`
- Backend docs: `http://127.0.0.1:8001/api/v2/docs`

## Docker Compose

```bash
docker compose up --build
```

Open:

- Frontend: `http://127.0.0.1:8080`
- Backend API: `http://127.0.0.1:8001`
- Health: `http://127.0.0.1:8001/api/v2/health`

Compose mounts `./data` and `./logs` for persistent runtime state. Secrets
must stay in `.env`; `.env` is ignored by git.

## Safety Defaults

The default configuration is not live trading:

```env
TRADE_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_USE_TEST_ORDER=true
ATTACH_PROTECTION_ORDERS=true
```

Real Binance Futures orders require explicit live configuration plus readiness
gates. PRG and live readiness can block live execution even when trading mode is
set to live.

## Verification

Run before committing or deploying:

```bash
.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test -- --run
npm run build
```

Deployment smoke tests:

```bash
curl http://127.0.0.1:8001/api/v2/health
curl http://127.0.0.1:8080/
```

See `docs/deployment.md` for the full deployment checklist and rollback path.
