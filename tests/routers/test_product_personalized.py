"""개인화 라우터 테스트 — 공용 fixture(client, dataset)는 conftest.py 참고.

응답 계약: {code, message, result=[product_seq...]} — source 필드가 없으므로
콜드스타트는 "세 변형(비로그인·미지 유저·게스트 sentinel)이 서로 같은 전역 인기"로 검증한다.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from scripts.generate_data import Dataset
from tests.conftest import db_url, postgres_available, product_is_valid

pytestmark = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 없음 — integration 테스트 skip"
)


def _active_user() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT user_seq FROM mart.user_recent_views "
            "GROUP BY user_seq HAVING count(*) >= 5 LIMIT 1"
        ).fetchone()[0]


def test_personalized_excludes_seed_products(client: TestClient) -> None:
    user_seq = _active_user()
    with psycopg.connect(db_url()) as conn:
        seeds = {
            seq
            for (seq,) in conn.execute(
                "SELECT product_seq FROM mart.user_recent_views WHERE user_seq = %s", (user_seq,)
            ).fetchall()
        }

    response = client.get("/api/v1/products/personalized", params={"user_seq": user_seq})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["result"]
    assert not (set(body["result"]) & seeds)


def test_personalized_reflects_planted_preferences(client: TestClient, dataset: Dataset) -> None:
    category_of = dict(dataset.product_categories)
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
        response = client.get("/api/v1/products/personalized", params={"user_seq": user_seq})
        result = response.json()["result"]
        preferred = set(dataset.preferred[user_seq])
        shares.append(sum(1 for seq in result if category_of[seq] in preferred) / len(result))

    # 심은 선호(세션 70% 시작)가 개인화 결과에 드러나야 한다 — 무작위 기준선은
    # 선호 2~3개/카테고리 12개 ≈ 21%. 그 2배를 문턱으로 잡는다 (ALS 추천에는
    # 탐색 행동 30%와 인기 효과가 섞여 0.5 부근은 데이터 노이즈에 출렁인다)
    assert sum(shares) / len(shares) > 0.4


def _personalized(client: TestClient, **params) -> list[int]:
    return client.get("/api/v1/products/personalized", params=params).json()["result"]


def test_cold_start_variants_return_same_global_popular(
    client: TestClient, dataset: Dataset
) -> None:
    # 비로그인·미지 유저·게스트 sentinel(≤0) — 프로필 없음 → 셋 다 같은 전역 인기 콜드스타트
    no_user = _personalized(client)
    unknown = _personalized(client, user_seq=9999999)
    guest = _personalized(client, user_seq=-1)

    assert no_user and no_user == unknown == guest
    valid = {row[0] for row in dataset.products if product_is_valid(row)}
    assert set(no_user) <= valid


def test_fusion_fills_up_to_limit(client: TestClient) -> None:
    # user CF·선호 상품 CF가 융합되고, 부족분은 선호 카테고리 인기가 뒤채운다
    response = client.get(
        "/api/v1/products/personalized", params={"user_seq": _active_user(), "limit": 20}
    )

    assert len(response.json()["result"]) == 20


def test_personalized_result_passes_hygiene(client: TestClient, dataset: Dataset) -> None:
    valid = {row[0] for row in dataset.products if product_is_valid(row)}

    response = client.get("/api/v1/products/personalized", params={"user_seq": _active_user()})

    assert set(response.json()["result"]) <= valid
