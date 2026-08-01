"""개인화 추천 서빙 — batch(user_info·user_product_cf)가 준비한 프로필과 후보를 융합한다.

router(product_personalized.py)의 실제 호출 순서:
1. fetch_user_info(프로필(성별·출생연도·선호 카테고리·선호 상품) + 시드 목록을 한 쿼리로) /
   bad_seller.get_bad_sellers(전역 차단 셀러)를 asyncio.gather로 동시 취득 (서로 독립)
   — user_info가 None이면(게스트·무이력) 전역 인기 콜드스타트로 끝(게스트 판정은 fast-path)
2. get_cf_by_user / get_cf_by_product(선호 상품) / popular.fetch_popular_by_category(선호
   카테고리)를 asyncio.gather로 동시 취득 (서로 독립)
3. router가 위생·셀러 정책 → RRF 융합 → 시드(이미 본 상품) 제외 → top-limit
"""

from typing import Any

import structlog
from psycopg_pool import AsyncConnectionPool

from app.models.response import to_result
from app.ranking import apply_rrf, apply_seller_spread
from app.services import hygiene

logger = structlog.get_logger(__name__)

# =========== CONSTANT ===========
SELLER_MIN_GAP = 5  # 같은 셀러가 다시 나오기까지 최소 슬롯 간격 (다양성)

# =========== SQL ===========
# 프로필(성별·출생연도·선호)과 시드 목록(RRF 후 제외용)을 한 쿼리로
_USER_INFO_SQL = """
    SELECT
        ui.gender,
        ui.birth_year,
        ui.preferred_category_seq,
        ui.preferred_product_seq,
        array_agg(urv.product_seq) AS seed_product_seqs
    FROM mart.user_info ui
    JOIN mart.user_recent_views urv USING (user_seq)
    WHERE ui.user_seq = %s
    GROUP BY 1, 2, 3, 4
"""

# ALS model.recommend — batch(train_cf)가 미리 계산해둔 유저별 CF 후보
_USER_CF_SQL = """
    SELECT product_seq, score
    FROM mart.user_product_cf
    WHERE user_seq = %s
    ORDER BY score DESC, product_seq
    LIMIT %s
"""

# ALS model.similar_items — 선호 상품의 임베딩 유사 상품 (같은 모델의 다른 출력)
_ITEM_CF_SQL = """
    SELECT similar_product_seq AS product_seq, score
    FROM mart.product_product_cf
    WHERE product_seq = %s
    ORDER BY score DESC, similar_product_seq
    LIMIT %s
"""


# =========== Fetch ===========


def _is_guest(user_seq: int | None) -> bool:
    """비로그인 = user_seq 미제공 또는 0 이하 — 신호 계층이 DB 미조회 fast-path로 처리한다."""
    return user_seq is None or user_seq <= 0


async def fetch_user_info(pool: AsyncConnectionPool, user_seq: int | None) -> dict[str, Any] | None:
    """유저 프로필 조회 — 게스트·이력 없음은 None (콜드스타트)."""
    if _is_guest(user_seq):
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(_USER_INFO_SQL, (user_seq,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "user_seq": user_seq,
        "gender": row[0],
        "birth_year": row[1],
        "preferred_category_seq": row[2],
        "preferred_product_seq": row[3],
        "seed_product_seqs": set(row[4]),
    }


async def get_cf_by_user(
    pool: AsyncConnectionPool, user_seq: int, fetch_limit: int
) -> list[dict[str, Any]]:
    """유저 기준 CF 후보 — ALS recommend를 batch가 계산해두고 서빙은 읽기만 한다."""
    async with pool.connection() as conn:
        cur = await conn.execute(_USER_CF_SQL, (user_seq, fetch_limit))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "score": r[1]} for r in rows]


async def get_cf_by_product(
    pool: AsyncConnectionPool, product_seq: int, fetch_limit: int
) -> list[dict[str, Any]]:
    """선호 상품 기준 CF 후보 — 같은 ALS 모델의 similar_items 출력."""
    async with pool.connection() as conn:
        cur = await conn.execute(_ITEM_CF_SQL, (product_seq, fetch_limit))
        rows = await cur.fetchall()
    return [{"product_seq": r[0], "score": r[1]} for r in rows]


# =========== Algorithm ===========


async def build_personalized_result(
    pool: AsyncConnectionPool,
    *,
    limit: int,
    blocklist: set[int],
    category_popular: list[dict[str, Any]],
    user_cf: list[dict[str, Any]] | None = None,
    item_cf: list[dict[str, Any]] | None = None,
    seed: set[int] | None = None,
) -> list[int]:
    """위생·셀러 정책 → CF 2원 RRF 융합 → 시드 제외 → 인기 backfill → seller spread → seq 리스트"""

    user_cf, item_cf = user_cf or [], item_cf or []
    seen = set(seed or ())

    # valid: 통과 상품 → 셀러. 거르는 데 쓰고(in), 마지막 seller spread 가 셀러 조회에 그대로 쓴다
    valid = await hygiene.filter_valid(
        pool, user_cf, item_cf, category_popular, blocklist=blocklist
    )
    user_cf = [r for r in user_cf if r["product_seq"] in valid]
    item_cf = [r for r in item_cf if r["product_seq"] in valid]
    category_popular = [r for r in category_popular if r["product_seq"] in valid]

    fused = apply_rrf([("user_cf", user_cf), ("item_cf", item_cf)], id_key="product_seq")
    picked = [r for r in fused if r["product_seq"] not in seen][:limit]
    if len(picked) < limit:
        seen |= {r["product_seq"] for r in picked}
        picked += [r for r in category_popular if r["product_seq"] not in seen][
            : limit - len(picked)
        ]
    result = to_result(apply_seller_spread(picked, SELLER_MIN_GAP, seller_of=valid))
    if not result:
        # CF 도 인기 backfill 도 빈손 — 콜드스타트 경로였다면 전역 인기 자체가 비었다는 뜻이라
        # mart 이상 신호에 가깝다. soft 실패 신호(에러도 RED 메트릭도 안 잡는다).
        logger.warning(
            "personalized empty",
            limit=limit,
            valid_user_cf=len(user_cf),
            valid_item_cf=len(item_cf),
            valid_popular=len(category_popular),
            seeds=len(seen),
        )
    return result


