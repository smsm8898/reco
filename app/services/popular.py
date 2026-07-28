"""인기 상품 조회 — 인기 구좌, 개인화·연관의 인기 신호와 콜드스타트 재료.

위생 필터는 서빙 시점에 소비하는 쪽이 적용하므로, 여기서는 그대로 반환한다.
"""

from typing import Any, NamedTuple

from psycopg_pool import AsyncConnectionPool

from app.models.products import Interval, to_result
from app.ranking import apply_rrf, compute_popularity
from app.services import hygiene

# =========== CONSTANT ===========
SIGNALS = ["num_view", "num_cart", "num_order", "gmv"]
RRF_K = 60
SELLER_MIN_GAP = 5  # 같은 셀러가 다시 나오기까지 최소 슬롯 간격 (다양성)


class IntervalConfig(NamedTuple):
    gravity: float  # 감쇠 강도 — 오프라인 gate(scripts/experiments)로 고른 값
    bucket_hours: int  # 감쇠 해상도 — day는 시간 단위, week/month는 일 단위
    window_hours: int  # 집계 창 — universe(30일)에서 서빙 시점에 자른다


# dial은 전부 서빙 몫 — batch(popular_universe)는 interval-free 원본만 적재한다.
INTERVAL_CONFIG: dict[Interval, IntervalConfig] = {
    Interval.DAY: IntervalConfig(gravity=1.8, bucket_hours=1, window_hours=72),
    Interval.WEEK: IntervalConfig(gravity=1.0, bucket_hours=24, window_hours=240),
    Interval.MONTH: IntervalConfig(gravity=0.5, bucket_hours=24, window_hours=720),
}

# =========== SQL ===========
_GLOBAL_SQL = """
    SELECT product_seq, score
    FROM mart.product_popularity
    ORDER BY rank
    LIMIT %s
"""

_BY_CATEGORY_SQL = """
    SELECT product_seq, score, rank
    FROM mart.category_popularity
    WHERE category_seq = %s
    ORDER BY rank
    LIMIT %s
"""

_WEEKLY_BY_CATEGORY_SQL = """
    SELECT product_seq, score, last_event_at
    FROM mart.category_weekly_popularity
    WHERE category_seq = %s
"""

# 기준 시각은 universe 전역 max(bucket_ts) — now() 금지, 고정 데이터면 결과도 고정.
_UNIVERSE_SQL = """
    SELECT product_seq, seller_seq, bucket_ts, num_view, num_cart, num_order, gmv
    FROM mart.popular_universe
    WHERE category_seq = %s
      AND bucket_ts >= (SELECT max(bucket_ts) FROM mart.popular_universe)
                       - make_interval(hours => %s)
"""


# =========== Fetch ===========


async def fetch(pool: AsyncConnectionPool, fetch_limit: int) -> list[dict[str, Any]]:
    """전역 인기 — 인기 구좌, 콜드스타트."""
    async with pool.connection() as conn:
        cur = await conn.execute(_GLOBAL_SQL, (fetch_limit,))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "score": r[1]} for r in rows]


async def fetch_by_category(
    pool: AsyncConnectionPool, category_seq: int | None, fetch_limit: int
) -> list[dict[str, Any]]:
    """카테고리 인기 — 연관의 popular 신호, 개인화의 backfill. 카테고리 없으면 미제공."""
    if category_seq is None:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(_BY_CATEGORY_SQL, (category_seq, fetch_limit))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "score": r[1], "rank": r[2]} for r in rows]


async def fetch_weekly_by_category(
    pool: AsyncConnectionPool, category_seq: int
) -> list[dict[str, Any]]:
    """주간 카테고리 인기 후보 풀 (recall) — /popular 전용. 서빙이 HN으로 재랭킹한다."""
    async with pool.connection() as conn:
        cur = await conn.execute(_WEEKLY_BY_CATEGORY_SQL, (category_seq,))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "score": r[1], "last_event_at": r[2]} for r in rows]


async def fetch_popular(
    pool: AsyncConnectionPool, category_seq: int, interval: Interval
) -> list[dict[str, Any]]:
    """카테고리의 hourly bucket row — interval의 window로 자른다. 감쇠는 조립 몫."""
    window_hours = INTERVAL_CONFIG[interval].window_hours
    async with pool.connection() as conn:
        cur = await conn.execute(_UNIVERSE_SQL, (category_seq, window_hours))
        rows = await cur.fetchall()
    return [
        {
            "product_seq": r[0],
            "seller_seq": r[1],
            "bucket_ts": r[2],
            "num_view": r[3],
            "num_cart": r[4],
            "num_order": r[5],
            "gmv": r[6],
        }
        for r in rows
    ]


# =========== Algorithm ===========


def _compute_popularity(rows: list[dict[str, Any]], interval: Interval) -> list[dict[str, Any]]:
    """bucket row를 product 단위 감쇠 합산으로 접는다 (순수 함수).

    신호 4종 각각에 compute_popularity를 적용해 합산 — 신호별 ranked list의 재료.
    now는 풀 내 최신 bucket_ts (now() 금지 규칙).
    """
    if not rows:
        return []
    cfg = INTERVAL_CONFIG[interval]
    now = max(row["bucket_ts"] for row in rows)
    products: dict[int, dict[str, Any]] = {}
    for row in rows:
        age_hours = (now - row["bucket_ts"]).total_seconds() / 3600
        acc = products.setdefault(
            row["product_seq"],
            {"product_seq": row["product_seq"], "seller_seq": row["seller_seq"]}
            | dict.fromkeys(SIGNALS, 0.0),
        )
        for signal in SIGNALS:
            acc[signal] += compute_popularity(
                row[signal], age_hours, bucket=cfg.bucket_hours, gravity=cfg.gravity
            )
    return list(products.values())


def _derive_source_rankings(
    products: list[dict[str, Any]], signals: list[str]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """감쇠 합산된 신호들을 각각의 ranked list로 분해한다 (RRF 입력, 컬럼명 = source label)."""
    rankings: list[tuple[str, list[dict[str, Any]]]] = []
    for col in signals:
        ranked = sorted(
            (r for r in products if r[col] > 0),
            key=lambda r, c=col: (-r[c], r["product_seq"]),
        )
        rankings.append((col, ranked))
    return rankings


def _spread_by_seller(products: list[dict[str, Any]], min_gap: int) -> list[dict[str, Any]]:
    """같은 셀러가 min_gap 슬롯 안에 다시 나오지 않게 greedy 재배치 (집합 불변).

    각 슬롯에서 '직전 min_gap-1개 셀러'에 없는 최고 점수 후보를 고르고, 불가능하면
    점수 순서에 양보한다. truncation 후에 실행해야 한다 — 먼저 펼치면 어떤 상품이
    잘리는지(선택) 자체가 바뀐다.
    """
    remaining = list(products)
    spread: list[dict[str, Any]] = []
    while remaining:
        recent = {r["seller_seq"] for r in spread[-(min_gap - 1) :]} if min_gap > 1 else set()
        pick = next((r for r in remaining if r["seller_seq"] not in recent), remaining[0])
        remaining.remove(pick)
        spread.append(pick)
    return spread


async def build_popular_result(
    pool: AsyncConnectionPool,
    rows: list[dict[str, Any]],
    *,
    interval: Interval,
    limit: int,
) -> list[int]:
    """감쇠 합산 → 위생 → 신호별 RRF → top-limit → seller spread → product_seq 리스트."""
    products = _compute_popularity(rows, interval)
    (valid,) = await hygiene.filter_valid(pool, products)
    fused = apply_rrf(_derive_source_rankings(valid, SIGNALS), k=RRF_K, id_key="product_seq")
    return to_result(_spread_by_seller(fused[:limit], SELLER_MIN_GAP))
