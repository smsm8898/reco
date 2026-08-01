"""개인화 엔드포인트 — 응답 계약, 심어둔 선호 반영, 콜드스타트.

응답에 source 필드가 없으므로 콜드스타트는 "세 변형(비로그인·미지 유저·게스트 sentinel)이
서로 같은 전역 인기"라는 성질로 검증한다.
"""

import psycopg
from fastapi.testclient import TestClient

from scripts.generate_data import Dataset
from tests.conftest import bad_seller_seqs, db_url, needs_db, seller_of, servable_seqs

pytestmark = needs_db


def _active_user() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT user_seq FROM mart.user_recent_views "
            "GROUP BY user_seq HAVING count(*) >= 5 LIMIT 1"
        ).fetchone()[0]


def _result(client: TestClient, **params) -> list[int]:
    return client.get("/api/v1/products/personalized", params=params).json()["result"]


def test_returns_envelope_with_servable_products(client: TestClient, dataset: Dataset) -> None:
    body = client.get(
        "/api/v1/products/personalized", params={"user_seq": _active_user()}
    ).json()

    assert body["code"] == 200
    result = body["result"]
    assert result
    assert len(result) == len(set(result))
    assert set(result) <= servable_seqs(dataset)


def test_seed_products_are_excluded(client: TestClient) -> None:
    """이미 본 상품(시드)은 추천하지 않는다."""
    user_seq = _active_user()
    with psycopg.connect(db_url()) as conn:
        seeds = {
            seq
            for (seq,) in conn.execute(
                "SELECT product_seq FROM mart.user_recent_views WHERE user_seq = %s", (user_seq,)
            ).fetchall()
        }

    result = _result(client, user_seq=user_seq)

    assert result
    assert not (set(result) & seeds)


def test_blocked_sellers_are_excluded(client: TestClient, dataset: Dataset) -> None:
    blocked = bad_seller_seqs(dataset)
    sellers = seller_of(dataset)

    personalized = _result(client, user_seq=_active_user(), limit=20)
    cold_start = _result(client)  # 콜드스타트 경로도 같은 정책을 탄다

    assert personalized and cold_start
    assert blocked
    assert all(sellers[seq] not in blocked for seq in personalized + cold_start)


def test_reflects_planted_preferences(client: TestClient, dataset: Dataset) -> None:
    """생성기가 심은 선호(세션 70%가 선호 카테고리에서 시작)가 결과에 드러나야 한다."""
    # dataset.preferred는 lv3 seq다 — product_categories엔 상품당 3행(lv4·lv3·lv2)이 있으니
    # lv3 행만 골라야 category_of가 선호 카테고리와 같은 레벨을 가리킨다
    level_of = {seq: level for seq, _, level, _ in dataset.categories}
    category_of = {
        product_seq: category_seq
        for product_seq, category_seq in dataset.product_categories
        if level_of[category_seq] == 3
    }
    with psycopg.connect(db_url()) as conn:
        users = [
            seq
            for (seq,) in conn.execute(
                "SELECT user_seq FROM mart.user_recent_views "
                "GROUP BY user_seq HAVING count(*) >= 5 LIMIT 10"
            ).fetchall()
        ]

    shares = []
    for user_seq in users:
        result = _result(client, user_seq=user_seq)
        preferred = set(dataset.preferred[user_seq])
        shares.append(sum(1 for seq in result if category_of[seq] in preferred) / len(result))

    # 무작위 기준선은 선호 2~3개/카테고리 12개 ≈ 21%. 그 2배를 문턱으로 잡는다 (ALS 추천에는
    # 탐색 행동 30%와 인기 효과가 섞여 0.5 부근은 데이터 노이즈에 출렁인다)
    assert sum(shares) / len(shares) > 0.4


def test_cold_start_variants_agree(client: TestClient, dataset: Dataset) -> None:
    """비로그인·미지 유저·게스트 sentinel(≤0) — 프로필이 없으니 셋 다 같은 전역 인기."""
    no_user = _result(client)
    unknown = _result(client, user_seq=9_999_999)
    guest = _result(client, user_seq=-1)

    assert no_user
    assert no_user == unknown == guest
    assert set(no_user) <= servable_seqs(dataset)


def test_fusion_fills_up_to_limit(client: TestClient) -> None:
    """CF 융합이 모자라면 카테고리 인기가 뒤채운다 — OVERSAMPLE 마진이 위생 탈락분을 흡수한다."""
    assert len(_result(client, user_seq=_active_user(), limit=20)) == 20


def test_limit_is_clamped(client: TestClient) -> None:
    user_seq = _active_user()

    assert len(_result(client, user_seq=user_seq, limit=3)) == 3
    assert len(_result(client, user_seq=user_seq, limit=0)) == 1
    assert len(_result(client, user_seq=user_seq, limit=999)) <= 50
