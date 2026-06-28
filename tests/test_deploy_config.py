from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_compose_defines_frontend_backend_and_migrations():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "backend:" in compose
    assert "frontend:" in compose
    assert "alembic upgrade head" in compose
    assert "8001:8001" in compose
    assert "8080:80" in compose


def test_backend_dockerfile_uses_production_web_server():
    dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")

    assert "gunicorn" in dockerfile
    assert "uvicorn.workers.UvicornWorker" in dockerfile
