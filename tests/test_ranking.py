"""랭킹 프리미티브 단위 테스트 — 전부 순수 함수라 DB 불필요.

세 지면이 공유하는 계층이라 여기서 깨지면 전 지면이 깨진다. 각 함수의 **계약**을 고정한다:
융합은 순위만 쓴다 / 다양성 정책은 셀러를 맵에서 읽는다 / 감쇠는 bucket 단위로 계단이다.
"""

from app.ranking import (
    RRF_K,
    apply_rrf,
    apply_same_seller_cap,
    apply_seller_spread,
    compute_popularity,
    derive_source_rankings,
)

# =========== compute_popularity (HN 시간감쇠) ===========


def test_decay_shrinks_with_age() -> None:
    fresh = compute_popularity(100, 0, bucket=24, gravity=1.0)
    old = compute_popularity(100, 24 * 6, bucket=24, gravity=1.0)

    assert fresh > old


def test_same_bucket_gets_same_decay() -> None:
    """bucket 올림이라 같은 bucket 안은 동일 감쇠 — 경계에서만 계단이 생긴다."""
    assert compute_popularity(100, 1, bucket=24, gravity=1.8) == compute_popularity(
        100, 23, bucket=24, gravity=1.8
    )
    assert compute_popularity(100, 0, bucket=24, gravity=1.8) > compute_popularity(
        100, 1, bucket=24, gravity=1.8
    )


def test_gravity_zero_means_no_decay() -> None:
    assert compute_popularity(100, 240, bucket=24, gravity=0.0) == 100


def test_negative_age_is_clamped() -> None:
    """미래 이벤트(클록 스큐)는 최신 취급 — 음수 지수로 크래시하지 않는다."""
    assert compute_popularity(100, -5, bucket=24, gravity=1.8) == compute_popularity(
        100, 0, bucket=24, gravity=1.8
    )


# =========== derive_source_rankings (wide → long) ===========


def test_derive_splits_columns_into_ranked_lists() -> None:
    rows = [
        {"product_seq": 1, "num_view": 9.0, "gmv": 0.0},
        {"product_seq": 2, "num_view": 3.0, "gmv": 5.0},
    ]

    rankings = derive_source_rankings(rows, ["num_view", "gmv"])

    assert [label for label, _ in rankings] == ["num_view", "gmv"]  # 컬럼명 = source 라벨
    assert [r["product_seq"] for r in rankings[0][1]] == [1, 2]


def test_derive_drops_zero_signal_rows() -> None:
    """"신호 없음"과 "신호 최하위"는 다르다 — 0이면 그 순위에서 빠져야 1/(k+rank)를 안 받는다."""
    rows = [{"product_seq": 1, "gmv": 0.0}, {"product_seq": 2, "gmv": 5.0}]

    (_, gmv_ranked), = derive_source_rankings(rows, ["gmv"])

    assert [r["product_seq"] for r in gmv_ranked] == [2]


def test_derive_preserves_input_order_on_ties() -> None:
    """동률은 입력 순서 보존 — grip 처럼 tie-break 키를 두지 않는다."""
    rows = [{"product_seq": 2, "num_view": 5.0}, {"product_seq": 1, "num_view": 5.0}]

    (_, ranked), = derive_source_rankings(rows, ["num_view"])

    assert [r["product_seq"] for r in ranked] == [2, 1]


# =========== apply_rrf (융합) ===========


def test_rrf_prefers_items_ranked_high_in_multiple_sources() -> None:
    rankings = [
        ("view", [{"product_seq": 1}, {"product_seq": 2}, {"product_seq": 3}]),
        ("cart", [{"product_seq": 2}, {"product_seq": 1}]),
        ("popular", [{"product_seq": 2}, {"product_seq": 4}]),
    ]

    fused = apply_rrf(rankings)

    # 2는 세 source 모두 상위 — 융합 1위
    assert [r["product_seq"] for r in fused][:2] == [2, 1]
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"]


def test_rrf_uses_position_not_stored_rank() -> None:
    """순위는 리스트 내 위치다 — mart가 매겨둔 rank 컬럼이 아니라.

    위생·셀러 정책이 후보를 걷어내면 저장된 rank에는 구멍이 생기는데, 위치로 다시 매겨야
    많이 걸러진 source가 부당하게 손해를 보지 않는다.
    """
    fused = apply_rrf([("popular", [{"product_seq": 7, "rank": 99}])])

    assert fused[0]["rrf_score"] == 1.0 / (RRF_K + 1)


def test_rrf_merges_fields_across_sources() -> None:
    rankings = [
        ("view", [{"product_seq": 1, "num_view": 9}]),
        ("popular", [{"product_seq": 1, "rank": 3}]),
    ]

    fused = apply_rrf(rankings)

    assert fused[0]["num_view"] == 9
    assert fused[0]["rank"] == 3


def test_rrf_scales_incomparable_signals_without_normalizing() -> None:
    """카운트 수백(view) vs 한 자릿수(order)를 정규화 없이 섞을 수 있는 것이 RRF의 요점."""
    fused = apply_rrf(
        [
            ("view", [{"product_seq": 1}, {"product_seq": 2}]),
            ("order", [{"product_seq": 2}, {"product_seq": 1}]),
        ]
    )

    # 두 source에서 정확히 대칭이라 점수가 같다 — 값 크기는 개입하지 않는다
    assert fused[0]["rrf_score"] == fused[1]["rrf_score"]


# =========== apply_same_seller_cap (다양성 — 개수 제한) ===========


def test_cap_limits_per_seller_preserving_order() -> None:
    rows = [{"product_seq": i} for i in (1, 2, 3, 4, 5)]
    seller_of = {1: 7, 2: 7, 3: 8, 4: 7, 5: 8}

    capped = apply_same_seller_cap(rows, cap=2, seller_of=seller_of)

    assert [r["product_seq"] for r in capped] == [1, 2, 3, 5]  # 4(s7 세 번째)만 탈락


def test_cap_keeps_rows_whose_seller_is_unknown() -> None:
    """맵에 없는 상품은 셀러 판정 대상이 아니다 — 조용히 버리지 않는다."""
    rows = [{"product_seq": 1}, {"product_seq": 2}]

    capped = apply_same_seller_cap(rows, cap=1, seller_of={1: 7})

    assert [r["product_seq"] for r in capped] == [1, 2]


# =========== apply_seller_spread (다양성 — 간격 벌리기) ===========


def test_spread_pushes_repeat_seller_past_the_gap() -> None:
    rows = [{"product_seq": i} for i in (1, 2, 3, 4, 5)]
    seller_of = {1: 7, 2: 7, 3: 8, 4: 9, 5: 10}

    spread = apply_seller_spread(rows, min_gap=3, seller_of=seller_of)

    # 2(s7)는 직전 2슬롯에 s7이 없어질 때까지 밀린다: 1,3,4,2,5
    assert [r["product_seq"] for r in spread] == [1, 3, 4, 2, 5]


def test_spread_is_set_preserving() -> None:
    """cap과 달리 버리지 않는다 — 순서만 바꾼다."""
    rows = [{"product_seq": i} for i in range(1, 6)]
    seller_of = dict.fromkeys(range(1, 6), 7) | {3: 8}

    spread = apply_seller_spread(rows, min_gap=3, seller_of=seller_of)

    assert {r["product_seq"] for r in spread} == {1, 2, 3, 4, 5}
    assert len(spread) == 5


def test_spread_yields_to_score_order_when_infeasible() -> None:
    """전부 같은 셀러면 제약을 만족할 수 없다 — 다양성보다 지면을 채우는 쪽이 우선."""
    rows = [{"product_seq": i} for i in (1, 2, 3)]

    spread = apply_seller_spread(rows, min_gap=3, seller_of=dict.fromkeys((1, 2, 3), 7))

    assert [r["product_seq"] for r in spread] == [1, 2, 3]


def test_spread_is_noop_when_gap_le_one() -> None:
    rows = [{"product_seq": 1}, {"product_seq": 2}]

    assert apply_seller_spread(rows, min_gap=1, seller_of={1: 7, 2: 7}) == rows
