"""ranking 유틸 단위 테스트 — DB 불필요한 순수 로직."""

from datetime import datetime, timedelta

from app.ranking import apply_rrf, apply_same_seller_cap, compute_popularity, hacker_news_rank


def test_rrf_prefers_items_ranked_high_in_multiple_lists() -> None:
    rankings = [
        ("view", [{"id": 1}, {"id": 2}, {"id": 3}]),
        ("cart", [{"id": 2}, {"id": 1}]),
        ("popular", [{"id": 2}, {"id": 4}]),
    ]

    fused = apply_rrf(rankings, k=60, id_key="id")

    # 2는 세 리스트 모두 상위 — 융합 1위여야 한다
    assert [r["id"] for r in fused][:2] == [2, 1]
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"]


def test_rrf_merges_row_fields() -> None:
    rankings = [
        ("view", [{"id": 1, "num_view": 9}]),
        ("popular", [{"id": 1, "rank": 3}]),
    ]

    fused = apply_rrf(rankings, k=60, id_key="id")

    assert fused[0]["num_view"] == 9
    assert fused[0]["rank"] == 3


def test_hacker_news_favours_recency_at_equal_score() -> None:
    now = datetime(2026, 4, 1)
    rows = [
        {"product_seq": 1, "score": 100, "last_event_at": now - timedelta(days=6)},
        {"product_seq": 2, "score": 100, "last_event_at": now},  # 같은 점수, 더 최근
    ]

    ranked = hacker_news_rank(rows)

    # age 기준(now)은 풀 최신값 = 상품 2의 시각 → 2가 앞선다
    assert [r["product_seq"] for r in ranked] == [2, 1]
    assert ranked[0]["hn_score"] > ranked[1]["hn_score"]


def test_hacker_news_favours_score_at_equal_recency() -> None:
    now = datetime(2026, 4, 1)
    rows = [
        {"product_seq": 1, "score": 10, "last_event_at": now},
        {"product_seq": 2, "score": 500, "last_event_at": now},  # 같은 최신성, 더 높은 점수
    ]

    ranked = hacker_news_rank(rows)

    assert [r["product_seq"] for r in ranked] == [2, 1]


def test_hacker_news_rank_handles_empty() -> None:
    assert hacker_news_rank([]) == []


def test_same_seller_cap_limits_per_seller_preserving_order() -> None:
    rows = [
        {"id": 1, "seller_seq": 7},
        {"id": 2, "seller_seq": 7},
        {"id": 3, "seller_seq": 8},
        {"id": 4, "seller_seq": 7},
        {"id": 5, "seller_seq": 8},
    ]

    capped = apply_same_seller_cap(rows, cap=2)

    assert [r["id"] for r in capped] == [1, 2, 3, 5]


def test_compute_popularity_decays_with_age() -> None:
    fresh = compute_popularity(100, 0, bucket=24, gravity=1.0)
    old = compute_popularity(100, 24 * 6, bucket=24, gravity=1.0)
    assert fresh > old


def test_compute_popularity_same_bucket_same_decay() -> None:
    # bucket 올림(ceil) — 같은 bucket 안(1h~24h)은 같은 감쇠, bucket 경계에서 계단
    assert compute_popularity(100, 1, bucket=24, gravity=1.8) == compute_popularity(
        100, 23, bucket=24, gravity=1.8
    )
    assert compute_popularity(100, 0, bucket=24, gravity=1.8) > compute_popularity(
        100, 1, bucket=24, gravity=1.8
    )


def test_compute_popularity_gravity_zero_means_no_decay() -> None:
    assert compute_popularity(100, 240, bucket=24, gravity=0.0) == 100


def test_compute_popularity_clamps_negative_age() -> None:
    assert compute_popularity(100, -5, bucket=24, gravity=1.8) == compute_popularity(
        100, 0, bucket=24, gravity=1.8
    )
