"""구좌별 라우터 플래그 테스트 — DB 불필요 (라우트 등록 여부만 확인).

플래그로 특정 지면만 켠 이미지를 지면별 Deployment로 나눠 배포하는 이음매를 검증한다.
라우터 마운트는 app.main import 시점에 굳으므로, 각 플래그 조합을 **깨끗한 서브프로세스**에서
띄워 확인한다 (같은 프로세스 내 reload는 이미 로드된 모듈이 옛 settings를 물고 있어 불안정).
등록된 경로는 OpenAPI paths로 읽는다 — 최신 FastAPI는 include_router를 app.routes에
평탄화하지 않으므로 openapi()가 신뢰할 수 있는 소스다.
"""

import json
import os
import subprocess
import sys

_SNIPPET = """
import json
from app.main import app
print("ROUTES=" + json.dumps(sorted(app.openapi()["paths"])))
"""


def _routes_with_flags(**flags: str) -> set[str]:
    # 전체 env를 복사해 세 플래그만 명시적으로 통제 (PATH/venv 등은 유지)
    env = {
        **os.environ,
        "ENABLE_RELATED": "false",
        "ENABLE_PERSONALIZED": "false",
        "ENABLE_POPULAR": "false",
        **flags,
    }
    out = subprocess.run(
        [sys.executable, "-c", _SNIPPET], env=env, capture_output=True, text=True, check=True
    ).stdout
    line = next(ln for ln in out.splitlines() if ln.startswith("ROUTES="))
    return set(json.loads(line[len("ROUTES=") :]))


def test_all_surfaces_when_all_enabled() -> None:
    routes = _routes_with_flags(
        ENABLE_RELATED="true", ENABLE_PERSONALIZED="true", ENABLE_POPULAR="true"
    )
    assert "/api/v1/products/{product_seq}/related" in routes
    assert "/api/v1/products/personalized" in routes
    assert "/api/v1/products/popular" in routes


def test_single_surface_deployment() -> None:
    # related만 켠 Deployment — 다른 지면 라우트는 등록되지 않는다
    routes = _routes_with_flags(ENABLE_RELATED="true")
    assert "/api/v1/products/{product_seq}/related" in routes
    assert "/api/v1/products/personalized" not in routes
    assert "/api/v1/products/popular" not in routes
    # 헬스는 플래그와 무관하게 항상 있다 (/metrics는 include_in_schema=False라 스키마엔 없음)
    assert "/health" in routes


def test_all_surfaces_off_by_default() -> None:
    # 기본값(전부 false) — 어떤 지면도 켜지 않으면 상품 라우트가 없다 (명시적 opt-in)
    routes = _routes_with_flags()
    assert not any(r.startswith("/api/v1/products") for r in routes)
    assert "/health" in routes
