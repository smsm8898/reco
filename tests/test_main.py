from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from app.core.db import get_pool
from app.main import app


class FakeConnection:
    async def execute(self, query: str) -> None:
        return None


class FakePool:
    """DB 없이 /ready의 두 분기를 결정적으로 테스트하기 위한 대역."""

    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    @asynccontextmanager
    async def connection(self, timeout: float | None = None):
        if not self.healthy:
            raise ConnectionError("postgres down")
        yield FakeConnection()


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_postgres_is_up() -> None:
    app.dependency_overrides[get_pool] = lambda: FakePool(healthy=True)
    try:
        with TestClient(app) as client:
            response = client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "postgres": "ok"}


def test_ready_when_postgres_is_down() -> None:
    app.dependency_overrides[get_pool] = lambda: FakePool(healthy=False)
    try:
        with TestClient(app) as client:
            response = client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "postgres": "unavailable"}
