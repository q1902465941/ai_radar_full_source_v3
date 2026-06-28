# Deployment

This project now has a separated production-style deployment path for the v2 rebuild.

## Local development

- Backend v2 API: `python run_v2.py`
- Frontend dev server: `cd frontend && npm run dev`
- Frontend talks to `/api/v2/*` through the Vite proxy.

## Docker Compose

Start the separated stack:

```bash
docker compose up --build
```

Services:

- Frontend: `http://127.0.0.1:8080`
- Backend API: `http://127.0.0.1:8001`
- API docs: `http://127.0.0.1:8001/api/v2/docs`

The backend command runs `python -m alembic upgrade head` before starting Gunicorn with the Uvicorn worker.

## Persistent data

Compose mounts:

- `./data:/app/data`
- `./logs:/app/logs`

The current default database is SQLite at the configured `settings.db_path`. The SQLAlchemy layer is ready for PostgreSQL-compatible URLs in later deployment phases.
