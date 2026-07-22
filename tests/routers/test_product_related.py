"""연관 상품 라우터 테스트 — 공용 fixture(client, dataset)는 conftest.py 참고.

응답 계약: {code, message, result=[product_seq...]}.
"""

from collections import Counter

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.services.related import SAME_SELLER_CAP
from scripts.generate_data import Dataset
from tests.conftest import db_url, postgres_available, product_is_valid

pytestmark = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 없음 — integration 테스트 skip"
)


def _anchor_with_candidates() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT product_seq FROM mart.product_related GROUP BY product_seq "
            "HAVING count(*) >= 20 LIMIT 1"
        ).fetchone()[0]


def test_related_returns_unique_valid_result(client: TestClient, dataset: Dataset) -> None:
    anchor = _anchor_with_candidates()
    valid = {row[0] for row in dataset.products if product_is_valid(row)}

    response = client.get(f"/api/v1/products/{anchor}/related")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["message"] == "success"
    result = body["result"]
    assert result
    assert anchor not in result  # self 제외
    assert len(result) == len(set(result))  # 중복 없음
    assert set(result) <= valid  # 서빙 위생 필터 (5-rule)


def test_invalid_anchor_still_gets_recommendations(client: TestClient, dataset: Dataset) -> None:
    invalid_seq = next(row[0] for row in dataset.products if not product_is_valid(row))

    response = client.get(f"/api/v1/products/{invalid_seq}/related")

    assert response.status_code == 200
    result = response.json()["result"]
    assert result  # 존재하는 anchor — 카테고리 인기 신호가 채워준다
    assert invalid_seq not in result


def test_unknown_product_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/products/9999999/related")

    assert response.status_code == 404


def test_same_seller_cap_is_enforced(client: TestClient, dataset: Dataset) -> None:
    anchor = _anchor_with_candidates()
    seller_of = {row[0]: row[1] for row in dataset.products}

    response = client.get(f"/api/v1/products/{anchor}/related", params={"limit": 50})

    counts = Counter(seller_of[seq] for seq in response.json()["result"])
    assert counts and max(counts.values()) <= SAME_SELLER_CAP


def test_limit_is_clamped_not_rejected(client: TestClient) -> None:
    anchor = _anchor_with_candidates()

    response = client.get(f"/api/v1/products/{anchor}/related", params={"limit": 3})
    assert len(response.json()["result"]) == 3

    # 범위 밖 limit은 에러 대신 자동 clamp — 잘못된 값에도 지면은 뜬다
    response = client.get(f"/api/v1/products/{anchor}/related", params={"limit": 0})
    assert response.status_code == 200
    assert len(response.json()["result"]) == 1

    response = client.get(f"/api/v1/products/{anchor}/related", params={"limit": 999})
    assert response.status_code == 200
    assert len(response.json()["result"]) <= 50
