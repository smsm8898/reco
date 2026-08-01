"""개인화 지면 서비스 — 게스트 fast-path와 조립(융합·시드 제외·backfill) 규칙."""

import asyncio

import structlog

from app.services import personalize
from app.services.personalize import _is_guest


def test_guest_is_decided_without_touching_db() -> None:
    """비로그인 판정은 신호 계층의 무조회 fast-path — pool 자리에 None을 줘도 통과한다."""
    assert _is_guest(None) and _is_guest(0) and _is_guest(-1)
    assert not _is_guest(1)

    assert asyncio.run(personalize.fetch_user_info(None, None)) is None
    assert asyncio.run(personalize.fetch_user_info(None, 0)) is None


def _build(**kwargs):
    """후보가 전부 비면 위생 쿼리가 없어 pool 없이 조립을 검증할 수 있다."""
    return asyncio.run(
        personalize.build_personalized_result(
            None, limit=kwargs.pop("limit", 10), blocklist=set(), **kwargs
        )
    )


def test_empty_everything_logs_soft_signal() -> None:
    with structlog.testing.capture_logs() as logs:
        result = _build(category_popular=[])

    assert result == []
    assert [log["event"] for log in logs] == ["personalized empty"]


def test_cold_start_takes_the_same_path_as_warm() -> None:
    """CF 후보를 안 넘기면 융합이 빈손이라 전량 backfill — 콜드스타트 전용 분기가 없다."""
    result = _build(category_popular=[], user_cf=None, item_cf=None, seed=None)

    assert result == []  # 후보 자체가 없으니 빈 결과 (경로가 예외 없이 같다는 것만 확인)
