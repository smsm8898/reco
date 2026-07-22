"""관찰성 테스트 — RED 메트릭 노출과 access 로그 필드. DB 불필요(앱만 띄운다)."""

import structlog
from fastapi.testclient import TestClient

from app.main import app


def test_metrics_endpoint_exposes_red_after_request() -> None:
    with TestClient(app) as client:
        client.get("/health")  # 측정 대상 한 건 발생
        body = client.get("/metrics").text

    # Rate/Errors 카운터와 Duration 히스토그램이 노출된다 (RED)
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_metrics_excludes_probes() -> None:
    with TestClient(app) as client:
        client.get("/health")
        client.get("/ready")
        body = client.get("/metrics").text

    # probe/metrics는 instrument에서 제외 — 노이즈 카운트를 만들지 않는다
    assert 'handler="/health"' not in body
    assert 'handler="/ready"' not in body


def test_access_log_emits_canonical_line(capsys) -> None:
    # structlog을 콘솔 렌더러로 잡아 stdout 캡처 (json_logs=False)
    from app.core.logging_config import setup_logging

    setup_logging(service_name="reco", service_version="test", env="local", json_logs=False)
    try:
        with TestClient(app) as client:
            client.get("/health")
        out = capsys.readouterr().out
        assert "request" in out  # canonical access 라인 이벤트명
        assert "http.route" in out
    finally:
        # 다른 테스트에 영향 없도록 structlog 기본 상태로 되돌린다
        structlog.reset_defaults()
