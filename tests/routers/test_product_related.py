"""연관 상품 엔드포인트 — 응답 계약과 정답지 대조."""

from collections import Counter

import psycopg
from fastapi.testclient import TestClient

from app.services.related import SAME_SELLER_CAP
from scripts.generate_data import Dataset
from tests.conftest import (
    bad_seller_seqs,
    db_url,
    needs_db,
    seller_of,
    servable_seqs,
)

pytestmark = needs_db


def _anchor_with_candidates() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT product_seq FROM mart.product_related GROUP BY product_seq "
            "HAVING count(*) >= 20 LIMIT 1"
        ).fetchone()[0]


def _get(client: TestClient, product_seq: int, **params) -> dict:
    return client.get(f"/api/v1/products/{product_seq}/related", params=params).json()


def test_returns_envelope_with_unique_servable_products(
    client: TestClient, dataset: Dataset
) -> None:
    anchor = _anchor_with_candidates()

    body = _get(client, anchor)

    assert body["code"] == 200
    assert body["message"] == "success"
    result = body["result"]
    assert result
    assert anchor not in result  # self 제외
    assert len(result) == len(set(result))  # 중복 없음
    assert set(result) <= servable_seqs(dataset)  # 위생 9-rule + 셀러 정책 통과분만


def test_blocked_sellers_are_excluded(client: TestClient, dataset: Dataset) -> None:
    blocked = bad_seller_seqs(dataset)
    sellers = seller_of(dataset)

    result = _get(client, _anchor_with_candidates(), limit=50)["result"]

    assert result
    assert blocked
    assert all(sellers[seq] not in blocked for seq in result)


def test_same_seller_cap_is_enforced(client: TestClient, dataset: Dataset) -> None:
    sellers = seller_of(dataset)

    result = _get(client, _anchor_with_candidates(), limit=50)["result"]

    counts = Counter(sellers[seq] for seq in result)
    assert counts and max(counts.values()) <= SAME_SELLER_CAP


def test_stopped_product_is_still_a_valid_anchor(client: TestClient, dataset: Dataset) -> None:
    """판매중지 상품도 anchor로 유효하다 — 그 상품 페이지에서 지면이 뜨므로.

    위생은 추천 *결과*에만 걸린다.
    """
    servable = servable_seqs(dataset)
    invalid_seq = next(row[0] for row in dataset.products if row[0] not in servable)

    body = _get(client, invalid_seq)

    assert body["code"] == 200
    assert invalid_seq not in body["result"]


def test_unknown_product_returns_empty_result(client: TestClient) -> None:
    """없는 product_seq(stale·오타)는 에러가 아니라 빈 추천 — 미존재를 따로 구분하지 않는다."""
    response = client.get("/api/v1/products/9999999/related")

    assert response.status_code == 200
    assert response.json()["result"] == []


def test_limit_is_clamped(client: TestClient) -> None:
    anchor = _anchor_with_candidates()

    assert len(_get(client, anchor, limit=3)["result"]) == 3
    assert len(_get(client, anchor, limit=0)["result"]) == 1
    assert len(_get(client, anchor, limit=999)["result"]) <= 50
