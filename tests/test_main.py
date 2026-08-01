"""앱 진입점 — probe 계약. /health 는 DB 무관, /ready 는 DB 의존."""

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import needs_db


def test_health_does_not_touch_the_db() -> None:
    """liveness 는 프로세스가 살아있는지만 본다 — DB가 죽어도 200이어야 재시작 루프에 안 빠진다."""
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@needs_db
def test_ready_reports_postgres_ok(client: TestClient) -> None:
    """readiness 는 의존성까지 본다 — 트래픽을 받을 준비가 됐는지."""
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "postgres": "ok"}


def test_ready_returns_503_when_postgres_is_unreachable(monkeypatch) -> None:
    """DB가 안 되면 503 — 그래야 로드밸런서가 이 pod을 뺀다(500이 아니라 명시적 not_ready)."""
    from app.core import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "PG_HOST", "127.0.0.1")
    monkeypatch.setattr(settings_module.settings, "PG_PORT", 1)  # 닫힌 포트

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
