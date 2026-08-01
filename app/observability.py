"""관찰성 배선 — RED 메트릭(Prometheus)과 구조화 access 로그(Loki).

- RED: Instrumentator가 매 요청을 측정해 /metrics로 노출한다.
  - Rate/Errors: http_requests_total{handler,method,status} → rate(), status=~"5.."
  - Duration:    http_request_duration_seconds{handler,method} → histogram_quantile
- Loki: access_log 미들웨어가 요청당 canonical JSON 한 줄을 stdout에 emit한다.
  라우터가 request.state.reco_result_count를 세팅하면 필드로 승격 → 빈 추천율을 LogQL로 측정.
  쿼리스트링(url.query)은 차원이 쿼리에 있는 지면(popular의 category_seq·interval)을 Loki에서
  분해하기 위해 싣는다. 캐시 상태(reco.cache_status) 승격은 캐시 레이어와 함께 온다.
"""

import time
from uuid import uuid4

import structlog
from asgi_correlation_id import CorrelationIdMiddleware, correlation_id
from fastapi import FastAPI, Request, Response
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from starlette.middleware.base import RequestResponseEndpoint

from app.core.logging_config import get_access_logger

# 현 지연 대역을 촘촘히 나눈 히스토그램 버킷 — p99 보간 오차를 줄인다
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0)

_access_logger = get_access_logger()


async def _access_log(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """요청당 canonical JSON access 로그. 전 라우트 공통."""
    structlog.contextvars.clear_contextvars()  # 이전 요청 잔여 컨텍스트 청소(누수 방지)
    structlog.contextvars.bind_contextvars(request_id=correlation_id.get())
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # 실패 요청의 쿼리(product_seq/category_seq 등)까지 남긴다
        _access_logger.exception(
            "request failed",
            **{
                "http.request.method": request.method,
                "url.path": request.url.path,
                "url.query": request.url.query,
            },
        )
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    route = request.scope.get("route")
    fields = {
        "http.request.method": request.method,
        "http.route": getattr(route, "path", request.url.path),  # 템플릿 (저카디널리티)
        "url.path": request.url.path,
        # 차원(category_seq·interval)이 쿼리스트링에 있는 지면의 Loki 분해용 — 없으면 빈응답을
        # 카테고리로 귀속할 수 없다. 빈 쿼리스트링이면 키를 생략해 null 노이즈를 만들지 않는다.
        **({"url.query": request.url.query} if request.url.query else {}),
        "http.response.status_code": response.status_code,
        "duration_ms": duration_ms,
        "client.address": (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None)
        ),
        "http.request.body_size": request.headers.get("content-length"),
        "user_agent.original": request.headers.get("user-agent"),
    }
    # 라우터가 세팅한 결과 수를 필드로 승격 → 빈응답률(reco.result_count == 0)을 LogQL로 측정.
    # 미세팅 라우트(/health 등)는 키 자체를 생략한다.
    result_count = getattr(request.state, "reco_result_count", None)
    if result_count is not None:
        fields["reco.result_count"] = result_count
    _access_logger.info("request", **fields)
    return response


def setup_observability(app: FastAPI) -> None:
    """access 로그 미들웨어 + request_id + RED 메트릭(/metrics)을 앱에 배선한다."""
    app.middleware("http")(_access_log)
    # CorrelationId를 나중에 add → 가장 바깥 → request_id가 access_log보다 먼저 설정됨
    app.add_middleware(
        CorrelationIdMiddleware, header_name="X-Request-ID", generator=lambda: uuid4().hex
    )

    (
        Instrumentator(excluded_handlers=["/health", "/ready", "/metrics"])
        .add(metrics.requests())  # http_requests_total{handler,method,status}
        .add(metrics.latency(buckets=_LATENCY_BUCKETS, should_include_status=False))
        .instrument(app)
        .expose(app, endpoint="/metrics", include_in_schema=False)
    )
