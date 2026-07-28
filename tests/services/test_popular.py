"""popular 서빙 순수 함수 단위 테스트 — DB 불필요."""

from datetime import datetime, timedelta
from typing import Any

from app.models.products import Interval
from app.services.popular import (
    SIGNALS,
    _compute_popularity,
    _derive_source_rankings,
    _spread_by_seller,
)

NOW = datetime(2026, 4, 1)


def _bucket(product_seq: int, seller_seq: int, hours_ago: int, **signals: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "product_seq": product_seq,
        "seller_seq": seller_seq,
        "bucket_ts": NOW - timedelta(hours=hours_ago),
    }
    for signal in SIGNALS:
        row[signal] = signals.get(signal, 0)
    return row


def test_compute_popularity_sums_buckets_with_decay() -> None:
    # week dial: gravity=1.0, bucket=24h → now bucket은 /2, 48h 전 bucket은 /4
    rows = [
        _bucket(1, 7, hours_ago=0, num_view=10),
        _bucket(1, 7, hours_ago=48, num_view=10),
        _bucket(2, 8, hours_ago=0, num_view=10),
    ]

    products = _compute_popularity(rows, Interval.WEEK)

    by_seq = {p["product_seq"]: p for p in products}
    assert by_seq[1]["num_view"] == 10 / 2 + 10 / 4  # 감쇠 합산
    assert by_seq[2]["num_view"] == 10 / 2
    assert by_seq[1]["seller_seq"] == 7


def test_recent_events_outweigh_old_at_equal_volume() -> None:
    rows = [
        _bucket(1, 7, hours_ago=48, num_view=20),  # 같은 총량, 오래됨
        _bucket(2, 8, hours_ago=0, num_view=20),  # 최신
    ]

    products = _compute_popularity(rows, Interval.WEEK)

    by_seq = {p["product_seq"]: p for p in products}
    assert by_seq[2]["num_view"] > by_seq[1]["num_view"]


def test_compute_popularity_handles_empty() -> None:
    assert _compute_popularity([], Interval.WEEK) == []


def test_derive_source_rankings_splits_and_drops_zero() -> None:
    products = [
        {"product_seq": 1, "num_view": 9.0, "gmv": 0.0},
        {"product_seq": 2, "num_view": 3.0, "gmv": 5.0},
    ]

    rankings = _derive_source_rankings(products, ["num_view", "gmv"])

    assert rankings[0][0] == "num_view"
    assert [r["product_seq"] for r in rankings[0][1]] == [1, 2]
    assert [r["product_seq"] for r in rankings[1][1]] == [2]  # gmv=0인 1은 제외


def test_derive_source_rankings_breaks_ties_by_product_seq() -> None:
    products = [
        {"product_seq": 2, "num_view": 5.0},
        {"product_seq": 1, "num_view": 5.0},
    ]

    rankings = _derive_source_rankings(products, ["num_view"])

    assert [r["product_seq"] for r in rankings[0][1]] == [1, 2]


def test_spread_by_seller_respects_min_gap() -> None:
    products = [
        {"product_seq": 1, "seller_seq": 7},
        {"product_seq": 2, "seller_seq": 7},
        {"product_seq": 3, "seller_seq": 8},
        {"product_seq": 4, "seller_seq": 9},
        {"product_seq": 5, "seller_seq": 10},
    ]

    spread = _spread_by_seller(products, min_gap=3)

    # 2(s7)는 직전 2슬롯에 s7이 없어질 때까지 뒤로 밀린다: 1,3,4,2,5
    assert [r["product_seq"] for r in spread] == [1, 3, 4, 2, 5]
    assert {r["product_seq"] for r in spread} == {1, 2, 3, 4, 5}  # 집합 불변


def test_spread_yields_to_score_order_when_infeasible() -> None:
    products = [{"product_seq": i, "seller_seq": 7} for i in (1, 2, 3)]

    spread = _spread_by_seller(products, min_gap=3)

    assert [r["product_seq"] for r in spread] == [1, 2, 3]  # 전부 같은 셀러 → 점수 순 양보
