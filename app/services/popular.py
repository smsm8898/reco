"""인기 상품 조회 — 인기 구좌, 개인화·연관의 인기 신호와 콜드스타트 재료.

위생 필터는 서빙 시점에 소비하는 쪽이 적용하므로, 여기서는 그대로 반환한다.
"""

from typing import Any, NamedTuple

import structlog
from psycopg_pool import AsyncConnectionPool

from app.models.products import Interval
from app.models.response import to_result
from app.ranking import (
    RRF_K,
    apply_rrf,
    apply_seller_spread,
    compute_popularity,
    derive_source_rankings,
)
from app.services import hygiene

logger = structlog.get_logger(__name__)

# =========== CONSTANT ===========
SIGNALS = ["num_view", "num_cart", "num_order", "gmv"]
SELLER_MIN_GAP = 5  # 같은 셀러가 다시 나오기까지 최소 슬롯 간격 (다양성)
GLOBAL_CATEGORY_SEQ = -1  # 전역 인기 = 카테고리 인기의 한 파티션 (batch가 같은 점수식으로 적재)


class IntervalConfig(NamedTuple):
    gravity: float  # 감쇠 강도 — 오프라인 gate(scripts/experiments)로 고른 값
    bucket_hours: int  # 감쇠 해상도 — day는 시간 단위, week/month는 일 단위
    window_hours: int  # 집계 창 — universe(30일)에서 서빙 시점에 자른다


# dial은 전부 서빙 몫 — batch(popular_universe)는 interval-free 원본만 적재한다.
INTERVAL_CONFIG: dict[Interval, IntervalConfig] = {
    Interval.DAY: IntervalConfig(gravity=1.8, bucket_hours=1, window_hours=72),
    Interval.WEEK: IntervalConfig(gravity=1.4, bucket_hours=24, window_hours=168),
    Interval.MONTH: IntervalConfig(gravity=1.0, bucket_hours=24, window_hours=240),
}

# =========== SQL ===========
# 카테고리 인기 — category_seq = GLOBAL_CATEGORY_SEQ 면 전역 인기(같은 테이블의 한 파티션).
_BY_CATEGORY_SQL = """
    SELECT product_seq, rank
    FROM mart.category_popularity
    WHERE category_seq = %s
    ORDER BY rank
    LIMIT %s
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

async def fetch_popular_by_category(
    pool: AsyncConnectionPool, 
    fetch_limit: int, 
    category_seq: int = GLOBAL_CATEGORY_SEQ
) -> list[dict[str, Any]]:
    """카테고리 인기 
    
    — Related: popular 신호
    - Personalized: backfill·cold-start
    """
    async with pool.connection() as conn:
        cur = await conn.execute(_BY_CATEGORY_SQL, (category_seq, fetch_limit))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "rank": r[1]} for r in rows]


async def fetch_popular(
    pool: AsyncConnectionPool, category_seq: int, interval: Interval
) -> list[dict[str, Any]]:
    """universe 를 interval window 로 잘라 read → HN 감쇠 점수화까지"""
    cfg = INTERVAL_CONFIG[interval]
    async with pool.connection() as conn:
        cur = await conn.execute(_UNIVERSE_SQL, (category_seq, cfg.window_hours))
        rows = await cur.fetchall()
    buckets = [
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
    return _compute_popularity(buckets, cfg)


# =========== Algorithm ===========


def _compute_popularity(rows: list[dict[str, Any]], cfg: IntervalConfig) -> list[dict[str, Any]]:
    """bucket row를 product 단위 감쇠 합산으로 접는다 (순수 함수).

    신호 4종 각각에 compute_popularity를 적용해 합산 — 신호별 ranked list의 재료.
    now는 풀 내 최신 bucket_ts (now() 금지 규칙).
    """
    if not rows:
        return []
    
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


async def build_popular_result(
    pool: AsyncConnectionPool,
    products: list[dict[str, Any]],
    *,
    limit: int,
    blocklist: set[int],
) -> list[int]:
    """위생·셀러 정책 → 신호별 RRF → top-limit → seller spread → product_seq 리스트"""

    valid = await hygiene.filter_valid(pool, products, blocklist=blocklist)
    candidates = len(products)
    products = [r for r in products if r["product_seq"] in valid]
    fused = apply_rrf(
        derive_source_rankings(products, SIGNALS),
        k=RRF_K,
        id_key="product_seq"
    )
    result = to_result(apply_seller_spread(fused[:limit], SELLER_MIN_GAP, seller_of=valid))
    if not result:
        # universe 에 후보가 있었는데 결과가 0 — 위생·셀러 정책이 전부 걷어냈거나 애초에 후보가
        # 없었다. soft 실패 신호(에러도 RED 메트릭도 안 잡는다). 어느 카테고리·interval 인지는
        # 같은 request_id 의 access 라인(url.query)에서 읽는다.
        logger.warning("popular empty", limit=limit, candidates=candidates, valid=len(products))
    return result
