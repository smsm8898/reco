"""주간 카테고리 인기 라우터 테스트 — 공용 fixture(client, dataset)는 conftest.py 참고.

(HN 시간감쇠 랭킹 로직 자체는 순수 함수라 tests/test_ranking.py 에서 단위 테스트한다.)
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from scripts.generate_data import Dataset
from tests.conftest import db_url, postgres_available, product_is_valid

pytestmark = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 없음 — integration 테스트 skip"
)


def _category_with_candidates() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT category_seq FROM mart.category_weekly_popularity "
            "GROUP BY category_seq HAVING count(*) >= 20 LIMIT 1"
        ).fetchone()[0]


def test_category_seq_is_required(client: TestClient) -> None:
    assert client.get("/api/v1/products/popular").status_code == 422


def test_popular_returns_unique_valid_result(client: TestClient, dataset: Dataset) -> None:
    category_seq = _category_with_candidates()
    valid = {row[0] for row in dataset.products if product_is_valid(row)}

    response = client.get("/api/v1/products/popular", params={"category_seq": category_seq})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    result = body["result"]
    assert result
    assert len(result) == len(set(result))  # 중복 없음
    assert set(result) <= valid  # 서빙 위생 필터


def test_results_belong_to_requested_category(client: TestClient, dataset: Dataset) -> None:
    category_seq = _category_with_candidates()
    in_category = {p for p, c in dataset.product_categories if c == category_seq}

    response = client.get("/api/v1/products/popular", params={"category_seq": category_seq})

    assert set(response.json()["result"]) <= in_category


def test_limit_is_clamped(client: TestClient) -> None:
    category_seq = _category_with_candidates()

    response = client.get(
        "/api/v1/products/popular", params={"category_seq": category_seq, "limit": 3}
    )
    assert len(response.json()["result"]) == 3

    response = client.get(
        "/api/v1/products/popular", params={"category_seq": category_seq, "limit": 0}
    )
    assert response.status_code == 200
    assert len(response.json()["result"]) == 1
