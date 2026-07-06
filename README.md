# AI Radar Full Source v3

AI Radar is a local trading research and execution-control system for radar
scans, strategy research, paper/live readiness gates, and an operational
monitoring dashboard.

The repository currently has two runnable surfaces:

- Detailed monitoring site: `backend.main:app`
- Backend v2 API: `backend.app.main:app`
- Local backend entry: `python run_v2.py`
- React migration app: `frontend/`
- Docker Compose entry: `docker compose up --build`

Docker Compose keeps the detailed legacy Jinja monitoring site as the default
browser surface on port `8080`, while the v2 API remains available in a
parallel service for migration and API checks.

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

Legacy monitoring site:

```bash
python run.py
```

Open `http://127.0.0.1:8001/radar`.

## Docker Compose

```bash
docker compose up --build
```

If Docker Hub image pulls fail on `auth.docker.io` or
`registry-1.docker.io`, the Compose build supports explicit base image
overrides:

```powershell
$env:PYTHON_IMAGE='docker.m.daocloud.io/library/python:3.12-slim'
$env:NODE_IMAGE='docker.m.daocloud.io/library/node:24-alpine'
$env:NGINX_IMAGE='docker.m.daocloud.io/library/nginx:1.29-alpine'
docker compose up --build -d
```

Open:

- Monitoring site: `http://127.0.0.1:8080`
- Legacy backend API: `http://127.0.0.1:8001`
- Backend v2 API: `http://127.0.0.1:8002`
- v2 health: `http://127.0.0.1:8002/api/v2/health`
- Proxied v2 health: `http://127.0.0.1:8080/api/v2/health`

Compose mounts `./data` and `./logs` for persistent runtime state. Secrets
must stay in `.env`; `.env` is ignored by git.

Docker Compose passes market runtime settings from `.env`. The landing default
is Binance USD-M Futures public mainnet data:

```env
MARKET_DATA_MODE=binance
BINANCE_TESTNET=false
BINANCE_MARKET_FALLBACK_TESTNET=false
BINANCE_ASCII_SYMBOLS_ONLY=true
DOCKER_DB_PATH=data/ai_radar.db
AI_ENABLED=false
AI_STRATEGY_PROVIDER=rule
REQUIRE_CODEX_STRATEGY_FOR_ENTRY=false
```

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

Or run the local verification script from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_local.ps1
```

Check Docker Desktop and WSL prerequisites before Compose deployment:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_docker_prereqs.ps1
```

If the check reports `WSL_OPTIONAL_COMPONENT_REQUIRED`, run the helper below,
approve the Windows elevation prompt, then reboot:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_wsl_prereq.ps1
```

Run a local production-style stack without Docker:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_local_stack.ps1
```

Use `-BackendPort` and `-FrontendPort` when the defaults `8011` or `4183`
are already occupied.

Deployment smoke tests:

```bash
curl http://127.0.0.1:8080/radar
curl http://127.0.0.1:8080/api/state
curl http://127.0.0.1:8002/api/v2/health
```

Full Docker landing verification:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_docker_stack.ps1
```

The Docker verifier waits for a fresh radar scan, checks mainnet market source,
compares BTC price, BTC 24h percentage change, and radar top samples against
Binance USD-M Futures mainnet tickers, verifies that monitor symbols are
supported USD-M ASCII contracts, verifies that the active ticker pool covers
Binance's highest-priority liquidity-adjusted movers, checks that paper
graduation progress is visible in readiness, and runs the controlled paper
closed-loop acceptance.

Controlled paper closed-loop acceptance:

```powershell
$token = (Select-String .env '^API_TOKEN=').Line -replace '^API_TOKEN=',''
Invoke-RestMethod -Method Post `
  -Headers @{ 'X-API-Token' = $token } `
  -Uri 'http://127.0.0.1:8080/api/trade-director/acceptance/paper-cycle'
```

The acceptance response should have `ok=true` and `real_order_allowed=false`.

See `docs/deployment.md` for the full deployment checklist and rollback path.
