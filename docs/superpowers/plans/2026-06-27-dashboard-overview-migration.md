# Dashboard Overview Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the Overview/Dashboard first screen to the new v2 backend and independent React frontend while preserving the legacy pages.

**Architecture:** Add a read-only v2 dashboard API that summarizes existing `radar_engine` state without triggering scans. The React frontend consumes that endpoint through typed API helpers and renders the same operational concepts as the legacy dashboard: market state, summary metrics, direction distribution, latest candidates, and safety framing.

**Tech Stack:** FastAPI, pytest, Vite, React, TypeScript, Vitest.

## Global Constraints

- Do not modify legacy `backend/main.py`, Jinja templates, or old static assets.
- Do not trigger radar scans from the v2 dashboard read API.
- Do not change radar, trading, position, exchange, or AI decision behavior.
- Keep live trading visually and behaviorally guarded.
- Use typed frontend data helpers and avoid large `innerHTML` style updates.
- Verify with backend tests, frontend tests, frontend build, and browser rendering.

---

## File Structure

- Create `backend/app/services/dashboard.py`: pure summary builder for radar rows.
- Create `backend/app/api/dashboard.py`: `/api/v2/dashboard/overview`.
- Modify `backend/app/main.py`: register the dashboard router.
- Modify `tests/test_app_foundation.py`: backend route tests.
- Create `frontend/src/api/dashboard.ts`: typed dashboard API client.
- Create `frontend/src/api/dashboard.test.ts`: frontend summary API helper tests.
- Replace `frontend/src/App.tsx`: data-driven dashboard screen.
- Update `frontend/src/styles/app.css`: dashboard layout and responsive styling.

---

### Task 1: Backend Dashboard Summary

- [ ] Write a failing test in `tests/test_app_foundation.py` for `/api/v2/dashboard/overview` using a fake `radar_engine`.
- [ ] Run that test and confirm it fails because the route does not exist.
- [ ] Add `backend/app/services/dashboard.py` with `build_dashboard_overview(radar_engine: object) -> dict[str, object]`.
- [ ] Add `backend/app/api/dashboard.py` and register it in `backend/app/main.py`.
- [ ] Run the backend dashboard test and confirm it passes.

### Task 2: Frontend Dashboard Client

- [ ] Add a failing Vitest test for `getDashboardOverview()`.
- [ ] Implement `frontend/src/api/dashboard.ts`.
- [ ] Run Vitest and confirm it passes.

### Task 3: React Dashboard Screen

- [ ] Replace the foundation placeholder screen in `frontend/src/App.tsx` with a data-driven dashboard.
- [ ] Update `frontend/src/styles/app.css` for stable dashboard grids, candidate rows, direction bars, loading, and error states.
- [ ] Keep the app shell navigation visible but make Overview selected.
- [ ] Run `npm test -- --run` and `npm run build`.

### Task 4: Verification

- [ ] Run `python -m pytest tests/test_app_foundation.py tests/test_task_registry.py tests/test_db_foundation.py -v`.
- [ ] Run `python -m pytest -q`.
- [ ] Run `npm test -- --run` and `npm run build` in `frontend/`.
- [ ] Start or reuse `run_v2.py` and the frontend dev server.
- [ ] Open `http://127.0.0.1:5173` and capture a screenshot.
- [ ] Confirm the page renders Overview data without console errors or blank panels.
