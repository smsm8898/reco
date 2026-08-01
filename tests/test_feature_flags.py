"""구좌별 라우터 조건부 마운트 — 같은 이미지를 지면별 Deployment로 쪼개는 이음매.

`app.main` 은 import 시점에 플래그를 읽으므로, 플래그를 바꾼 앱은 모듈을 다시 로드해 만든다.
검증은 라우트 introspection 이 아니라 **실제 응답**으로 한다 — FastAPI 가 include 한 라우터를
어떤 구조로 들고 있는지는 버전마다 다르고, 우리가 지키려는 계약은 "꺼진 지면은 404" 뿐이다.
"""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

SURFACES = {
    "ENABLE_POPULAR": ("/api/v1/products/popular", {"category_seq": 1}),
    "ENABLE_RELATED": ("/api/v1/products/1/related", {}),
    "ENABLE_PERSONALIZED": ("/api/v1/products/personalized", {}),
}


@contextmanager
def _app_with(monkeypatch, **flags) -> Iterator[TestClient]:
    import app.core.settings as settings_module
    import app.main

    for name, value in flags.items():
        monkeypatch.setattr(settings_module.settings, name, value)

    reloaded = importlib.reload(app.main)
    try:
        # lifespan 을 태우지 않는다 — DB 없이 마운트 여부만 본다(pool 은 요청 시 필요)
        yield TestClient(reloaded.app)
    finally:
        importlib.reload(app.main)  # 다른 테스트가 쓰는 전역 app 을 원래대로


def test_only_enabled_surfaces_are_mounted(monkeypatch) -> None:
    """마운트 여부는 OpenAPI 스키마로 본다 — 핸들러를 실행하지 않아 DB 없이 확인된다."""
    with _app_with(
        monkeypatch, ENABLE_POPULAR=True, ENABLE_RELATED=False, ENABLE_PERSONALIZED=False
    ) as client:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/products/popular" in paths
        assert "/api/v1/products/{product_seq}/related" not in paths
        assert "/api/v1/products/personalized" not in paths


def test_disabled_surface_returns_404(monkeypatch) -> None:
    """꺼진 지면은 라우팅 단계에서 404 — 핸들러·의존성까지 가지 않는다."""
    with _app_with(
        monkeypatch, ENABLE_POPULAR=False, ENABLE_RELATED=False, ENABLE_PERSONALIZED=False
    ) as client:
        for path, params in SURFACES.values():
            assert client.get(path, params=params).status_code == 404


def test_all_surfaces_off_still_serves_probes(monkeypatch) -> None:
    """전 지면을 꺼도 앱은 뜨고 probe 는 응답한다 — 배포 자체가 실패하면 안 된다."""
    with _app_with(
        monkeypatch, ENABLE_POPULAR=False, ENABLE_RELATED=False, ENABLE_PERSONALIZED=False
    ) as client:
        assert client.get("/health").status_code == 200
        for path, params in SURFACES.values():
            assert client.get(path, params=params).status_code == 404
