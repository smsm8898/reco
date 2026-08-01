"""관찰성 — RED 메트릭 노출과 access 로그 필드. DB 불필요(앱만 띄운다).

access 라인은 Loki 에서 지면 상태를 읽는 유일한 창구라, 어떤 필드가 실리는지가 계약이다.
"""

import structlog
from fastapi.testclient import TestClient

from app.core.logging_config import setup_logging
from app.main import app


def _capture(capsys, path: str, **params) -> str:
    """콘솔 렌더러로 access 라인을 stdout 에 뽑는다(json_logs=False)."""
    setup_logging(service_name="reco", service_version="test", env="local", json_logs=False)
    try:
        with TestClient(app) as client:
            client.get(path, params=params)
        return capsys.readouterr().out
    finally:
        structlog.reset_defaults()  # 다른 테스트에 영향 없도록 기본 상태로 되돌린다


# =========== RED 메트릭 ===========


def test_metrics_endpoint_exposes_red() -> None:
    with TestClient(app) as client:
        client.get("/health")  # 측정 대상 한 건 발생
        body = client.get("/metrics").text

    assert "http_requests_total" in body  # Rate/Errors
    assert "http_request_duration_seconds" in body  # Duration


def test_metrics_excludes_probes() -> None:
    """probe/metrics 는 instrument 에서 제외 — 노이즈 카운트를 만들지 않는다."""
    with TestClient(app) as client:
        client.get("/health")
        client.get("/ready")
        body = client.get("/metrics").text

    assert 'handler="/health"' not in body
    assert 'handler="/ready"' not in body


# =========== access 로그 ===========


def test_emits_canonical_line_per_request(capsys) -> None:
    out = _capture(capsys, "/health")

    assert "request" in out  # canonical access 라인 이벤트명
    assert "http.route" in out  # 템플릿 경로(저카디널리티) — 대시보드 그룹핑 키


def test_includes_query_string_when_present(capsys) -> None:
    """차원이 쿼리스트링에 있는 지면(popular)의 Loki 분해용 — 없으면 빈응답을 귀속 못 한다."""
    out = _capture(capsys, "/health", category_seq=7)

    assert "url.query" in out
    assert "category_seq=7" in out


def test_omits_query_string_when_empty(capsys) -> None:
    """빈 쿼리스트링이면 키 자체를 생략 — null 노이즈를 만들지 않는다."""
    out = _capture(capsys, "/health")

    assert "url.query" not in out


def test_omits_result_count_on_routes_that_do_not_set_it(capsys) -> None:
    """추천 라우트만 reco.result_count 를 싣는다 — probe 는 키 자체가 없다."""
    out = _capture(capsys, "/health")

    assert "reco.result_count" not in out
