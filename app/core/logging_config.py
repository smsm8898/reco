"""로그 설정 — Loki가 수집할 구조화 로그를 stdout에 낸다.

- 운영(env != local): JSON 한 줄 → Loki `| json`으로 필드 파싱
- 로컬: 사람이 읽는 컬러 콘솔
- request_id 등 요청 컨텍스트는 contextvars로 모든 라인에 자동 부착
- 앱 로그와 uvicorn 로그를 한 렌더러로 통일

structlog + 표준 logging을 잇는 뼈대(ProcessorFormatter)는 structlog 공식 문서의
권장 방식을 따른다. 공통 필드(service_*)와 필드명은 이 서비스에 맞춰 정한 것.
"""

import logging
import sys

import structlog

# 로그 한 줄마다 붙는 서비스 식별 필드 (Loki 라벨/필터용)
_CONTEXT = "app.access"


def _base_processors(service_name: str, service_version: str, env: str) -> list:
    """앱 로그와 uvicorn 로그가 공유하는 전처리 체인."""

    def tag_service(_, __, event: dict) -> dict:
        event["service"] = service_name
        event["version"] = service_version
        event["env"] = env
        return event

    return [
        structlog.contextvars.merge_contextvars,  # request_id 등
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),  # logging의 extra= 흡수
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        tag_service,
    ]


def setup_logging(
    *,
    service_name: str,
    service_version: str,
    env: str,
    json_logs: bool = True,
    level: str = "INFO",
) -> None:
    """루트 로깅을 structlog로 구성한다. 다른 로거를 쓰기 전에 1회 호출."""
    shared = _base_processors(service_name, service_version, env)
    render = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 표준 logging(uvicorn 등)도 같은 체인을 거쳐 같은 모양으로 나가게 한다
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            render,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    # uvicorn의 자체 access 로그는 우리 미들웨어가 대신 발행하므로 끈다
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []


def get_access_logger() -> structlog.stdlib.BoundLogger:
    """요청 access 로그 전용 로거 (Loki에서 logger로 분리 조회)."""
    return structlog.get_logger(_CONTEXT)
