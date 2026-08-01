"""인기 구좌 엔드포인트 — 응답 계약과 정답지 대조."""

import psycopg
from fastapi.testclient import TestClient

from scripts.generate_data import Dataset
from tests.conftest import bad_seller_seqs, db_url, needs_db, seller_of, servable_seqs

pytestmark = needs_db


def _category_with_candidates() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT category_seq FROM mart.popular_universe "
            "GROUP BY category_seq HAVING count(DISTINCT product_seq) >= 20 LIMIT 1"
        ).fetchone()[0]


def _get(client: TestClient, **params) -> dict:
    return client.get("/api/v1/products/popular", params=params).json()


def test_category_seq_is_required(client: TestClient) -> None:
    assert client.get("/api/v1/products/popular").status_code == 422


def test_returns_envelope_with_unique_servable_products(
    client: TestClient, dataset: Dataset
) -> None:
    body = _get(client, category_seq=_category_with_candidates())

    assert body["code"] == 200
    assert body["message"] == "success"
    result = body["result"]
    assert result
    assert len(result) == len(set(result))  # 중복 없음
    assert set(result) <= servable_seqs(dataset)  # 위생 9-rule + 셀러 정책 통과분만


def test_blocked_sellers_are_excluded(client: TestClient, dataset: Dataset) -> None:
    blocked = bad_seller_seqs(dataset)
    sellers = seller_of(dataset)

    result = _get(client, category_seq=_category_with_candidates(), limit=50)["result"]

    assert result
    assert blocked  # 정답지에 차단 셀러가 있어야 의미가 있다
    assert all(sellers[seq] not in blocked for seq in result)


def test_results_belong_to_requested_category(client: TestClient, dataset: Dataset) -> None:
    category_seq = _category_with_candidates()
    in_category = {p for p, c in dataset.product_categories if c == category_seq}

    result = _get(client, category_seq=category_seq)["result"]

    assert set(result) <= in_category


def test_limit_is_clamped(client: TestClient) -> None:
    category_seq = _category_with_candidates()

    assert len(_get(client, category_seq=category_seq, limit=3)["result"]) == 3
    assert len(_get(client, category_seq=category_seq, limit=0)["result"]) == 1
    assert len(_get(client, category_seq=category_seq, limit=999)["result"]) <= 50


def test_all_intervals_are_accepted(client: TestClient) -> None:
    category_seq = _category_with_candidates()

    for interval in ("day", "week", "month"):
        response = client.get(
            "/api/v1/products/popular", params={"category_seq": category_seq, "interval": interval}
        )
        assert response.status_code == 200, interval
        assert response.json()["result"], interval


def test_invalid_interval_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/products/popular",
        params={"category_seq": _category_with_candidates(), "interval": "year"},
    )

    assert response.status_code == 422


def test_default_interval_is_week(client: TestClient) -> None:
    # 정확 동등(list ==) 은 동률 tie-break 를 안 두는 정렬(grip 정합)이라 PG 스캔 순서에
    # 의존한다 — 같은 세션·정적 테이블에서는 안정적이지만 VACUUM FULL·재적재가 개입하면
    # 흔들릴 수 있다(_UNIVERSE_SQL 에 ORDER BY 없음).
    category_seq = _category_with_candidates()

    default = _get(client, category_seq=category_seq)
    weekly = _get(client, category_seq=category_seq, interval="week")

    assert default["result"] == weekly["result"]


def test_unknown_category_returns_empty_result(client: TestClient) -> None:
    """없는 카테고리는 에러가 아니라 빈 추천 — 지면은 뜨고 내용만 빈다."""
    body = _get(client, category_seq=9_999_999)

    assert body["code"] == 200
    assert body["result"] == []
