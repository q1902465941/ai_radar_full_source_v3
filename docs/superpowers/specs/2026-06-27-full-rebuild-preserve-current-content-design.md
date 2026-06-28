# AI Radar Full Rebuild While Preserving Current Content

Date: 2026-06-27

## Goal

Refactor `E:\ai_radar_full_source_v3` into a complete, separated web application while preserving the current project content and runtime behavior.

This is not a rewrite from an empty project. The current radar logic, AI strategy logic, trading controls, position management, learning modules, settings, local data, and existing pages must remain available while the new architecture is built and verified.

## Current State

The project is a Python FastAPI application with Jinja templates and static assets:

- Backend entry: `backend/main.py`
- Existing page templates: `backend/web/templates/`
- Existing frontend script and style: `backend/web/static/app.js`, `backend/web/static/app.css`
- Persistence layer: `backend/storage/db.py`
- Current database: SQLite through `settings.db_path`
- Tests: `tests/test_core.py`

Key observations:

- `backend/main.py` currently mixes app creation, page routing, API routing, background loops, settings writes, and service calls.
- The frontend is not independent. It is served as Jinja templates plus one large JavaScript file and one large CSS file.
- SQLite persistence exists, but most business records are stored as JSON payloads. This is useful for compatibility, but weak for long-term querying, migration, and observability.
- Some endpoints can trigger slow work directly from request handling, including scans, AI diagnostics, and position management. This can make pages feel blocked or laggy.
- The project is not currently a Git repository, so design and implementation changes cannot be committed until Git is initialized or the project is moved into a repository.

## Non-Goals

The first rebuild phase will not:

- Replace the radar scoring algorithm.
- Replace the trading and risk-control rules.
- Enable live trading by default.
- Remove the current Jinja pages.
- Delete current SQLite data.
- Rewrite AI provider clients from scratch.
- Change exchange behavior without explicit verification.

## Architecture Direction

The refactor will create a parallel, layered application structure while keeping the current modules usable.

Target structure:

```text
E:\ai_radar_full_source_v3
+- backend/
|  +- app/
|  |  +- main.py
|  |  +- api/
|  |  +- core/
|  |  +- services/
|  |  +- ai/
|  |  +- db/
|  |  +- workers/
|  |  +- schemas/
|  +- migrations/
|  +- web/
|  +- tests/
+- frontend/
|  +- src/
|  |  +- api/
|  |  +- components/
|  |  +- pages/
|  |  +- stores/
|  |  +- styles/
|  +- package.json
+- database/
|  +- schema.md
|  +- seed/
+- docs/
+- docker-compose.yml
```

Existing modules under `backend/radar`, `backend/trading`, `backend/positions`, `backend/learning`, `backend/market`, `backend/exchange`, and `backend/ai_strategy` remain the source of truth during the migration. New service wrappers will call into them instead of duplicating their behavior.

## Backend Design

The new backend layer will expose clean API groups:

- `api/radar.py`: radar state, scan task creation, scan task status, radar item reads.
- `api/positions.py`: open positions, closed positions, manual close, position summaries.
- `api/trading.py`: autotrade status, run-once task, start/stop controls, diagnostics.
- `api/ai.py`: AI task creation, AI task status, strategy QA, decision history.
- `api/settings.py`: config reads and safe config updates.
- `api/learning.py`: memory, attribution, calibration, strategies, evolution tasks.
- `api/account.py`: account summary, exchange positions, reconciliation.

`backend/app/main.py` should only assemble the app:

- Create FastAPI instance.
- Register middleware.
- Register routers.
- Register startup/shutdown hooks.
- Mount static assets only for the old compatibility pages if needed.

Slow work should move behind task-style APIs:

```text
POST /api/radar/scans       -> returns scan_id
GET  /api/radar/scans/{id}  -> returns status/result

POST /api/ai/tasks          -> returns task_id
GET  /api/ai/tasks/{id}     -> returns status/result

POST /api/trading/run-once  -> returns task_id
GET  /api/tasks/{id}        -> returns status/result
```

This keeps the web UI responsive and prevents user requests from waiting on long-running work.

## Frontend Design

The new frontend will be an independent Vite + React + TypeScript app.

Initial pages:

- Overview dashboard
- Radar center
- Positions
- Strategy AI
- Settings
- Learning and diagnostics, either as a dedicated page or nested tabs

Frontend behavior:

- Fetch data through typed API clients in `frontend/src/api/`.
- Keep page state in focused stores or hooks, not global DOM mutation.
- Use task status polling for slow actions.
- Use table virtualization or pagination for large radar/history views.
- Avoid full-page refresh loops.
- Use local optimistic loading states for buttons and panels.
- Keep live trading controls visually separate and disabled unless backend safety state permits them.

The old Jinja templates remain available during migration. The new frontend should initially run alongside them, then replace pages one by one after verification.

## AI Design

AI becomes a backend decision service, not a direct frontend integration.

Existing provider clients are preserved:

- `backend/ai_strategy/codex_cli_strategy_client.py`
- `backend/ai_strategy/deepseek_strategy_client.py`
- `backend/ai_strategy/openai_strategy_client.py`
- `backend/ai_strategy/strategy_qa.py`
- `backend/ai_strategy/context_compressor.py`

The new AI layer wraps them with:

- A unified `AIService`.
- Task records for every slow AI call.
- Provider/model metadata.
- Prompt and context summaries.
- Raw output storage.
- Parsed decision storage.
- Validation results.
- Error and timeout records.

AI may recommend:

- `WAIT`
- `OPEN_LONG`
- `OPEN_SHORT`
- Entry zone
- Stop loss
- Take-profit targets
- Confidence
- Rationale

AI must not directly place trades. Every AI decision must pass deterministic backend checks:

- Strategy contract validation
- Radar evidence quality
- Fake breakout and wick checks
- Funding and direction confirmation
- Max open positions
- Max risk and margin rules
- Exchange readiness
- Live-trading enablement state
- Manual/live safety gates

## Database Design

PostgreSQL is the target production database. SQLite may remain for local compatibility during migration.

The first database phase should add SQLAlchemy 2.0 and Alembic migrations while preserving JSON payload compatibility.

Core tables:

- `radar_scans`
- `radar_items`
- `market_snapshots`
- `positions`
- `closed_positions`
- `orders`
- `ai_tasks`
- `ai_decisions`
- `strategy_runs`
- `settings`
- `audit_logs`
- `background_tasks`

Important rule:

Structured columns should be added for frequently queried fields, but original payload JSON should also be preserved. This allows compatibility with existing code while enabling better queries and observability.

Example:

```text
positions
- id
- position_id
- symbol
- side
- status
- strategy_id
- entry_price
- current_price
- quantity
- margin
- open_time
- updated_at
- payload_json
```

## Data Migration

Migration must be conservative:

1. Back up the current SQLite database file.
2. Create new structured tables.
3. Read existing JSON payload rows.
4. Populate structured columns and preserve original JSON.
5. Validate row counts and sample records.
6. Keep the old SQLite database unchanged until the new database is verified.

No destructive migration is allowed in the first implementation phase.

## Compatibility Strategy

The old and new systems will coexist temporarily.

Compatibility rules:

- Current `python run.py` should continue to work until a replacement command is verified.
- Existing endpoints should remain available while new endpoints are added.
- Existing templates should not be removed during the first phase.
- New routers may call existing services directly.
- New frontend should use new APIs where available and compatibility APIs where needed.
- API response shapes must be documented before replacing a page.

## Performance Strategy

The rebuild should reduce perceived lag through:

- Task-based handling for scans, AI calls, and evolution runs.
- Cached read endpoints for dashboard panels.
- Short response budgets for page refresh calls.
- Background refresh loops owned by the backend, not repeated page-triggered expensive work.
- Frontend polling intervals based on task state instead of fixed aggressive refreshes.
- Stable table rendering with pagination or virtualization.
- Reduced DOM churn compared with the current large `innerHTML` updates.

## Security and Safety

Security rules:

- API tokens and exchange secrets stay server-side.
- Frontend never receives full secrets.
- Write endpoints keep token-based authorization or a stronger replacement.
- Live trading remains disabled by default.
- Dangerous actions require backend safety checks, not only frontend confirmation.
- AI decisions are auditable and cannot bypass deterministic risk controls.

## Testing Strategy

Tests should protect behavior before and during migration.

Backend tests:

- Existing core tests remain in place.
- Add route tests for each new API router.
- Add database migration tests using a temporary SQLite/PostgreSQL-compatible test database where practical.
- Add AI task tests with fake providers.
- Add task lifecycle tests for pending/running/succeeded/failed states.

Frontend tests:

- API client tests for response parsing.
- Component tests for key states: loading, success, empty, error.
- Browser smoke tests for dashboard, radar, positions, AI, settings.

Verification:

- Backend test suite must pass before claiming backend migration progress.
- Frontend build must pass before claiming frontend readiness.
- Browser checks must confirm that pages render without blank panels or blocking refresh behavior.

## Implementation Phases

### Phase 1: Foundation

- Add new backend `app/` structure.
- Add routers without deleting current routes.
- Add task registry abstraction.
- Add database model and migration foundation.
- Add frontend Vite app shell.
- Keep old pages working.

### Phase 2: Read APIs and Dashboard

- Add read-only dashboard APIs.
- Build new Overview page.
- Add cached summaries.
- Verify old and new dashboard data match.

### Phase 3: Radar Migration

- Add scan task API.
- Add radar read APIs.
- Build new Radar page.
- Remove direct slow scan waits from page refresh paths.
- Verify top candidates, top50, scan status, and active coins.

### Phase 4: Positions and Trading Controls

- Add position APIs.
- Add trading task APIs.
- Build Positions page.
- Build trading diagnostics and controls with safety status.
- Verify manual close and run-once behavior remain guarded.

### Phase 5: AI Task Layer

- Wrap existing AI clients with `AIService`.
- Add `ai_tasks` and `ai_decisions`.
- Build Strategy AI page on task polling.
- Store provider/model/status/errors.
- Keep deterministic validation after AI output.

### Phase 6: Database Migration

- Add structured tables.
- Migrate existing SQLite payloads.
- Validate row counts and sample records.
- Optionally add PostgreSQL via Docker Compose.
- Keep old SQLite backup.

### Phase 7: Cutover and Cleanup

- Make new frontend the default.
- Keep old pages under a legacy route for one release cycle.
- Remove obsolete template/static code only after verification.
- Document operational commands.

## Acceptance Criteria

The rebuild is acceptable when:

- Current project content remains available during migration.
- Backend and frontend are separated.
- New frontend has complete pages for overview, radar, positions, strategy AI, and settings.
- Slow scans and AI calls use task status instead of blocking page interactions.
- Database has migration files and structured core tables.
- Existing tests pass.
- New backend route tests pass.
- New frontend build passes.
- Browser smoke tests pass.
- No live-trading behavior changes occur without explicit approval.

## Open Decisions

The following choices can be finalized during implementation planning:

- Whether PostgreSQL is required immediately or introduced after SQLite-compatible SQLAlchemy models are in place.
- Whether background tasks should start with an in-process registry or immediately use Redis plus a worker.
- Whether the legacy Jinja pages should remain under their current URLs until all new pages are ready, or move under `/legacy`.

Default recommendation:

- Start with in-process tasks and SQLite-compatible SQLAlchemy models.
- Add PostgreSQL and Redis after the new API/frontend split is verified.
- Keep legacy pages on current URLs until the matching new page is verified.
