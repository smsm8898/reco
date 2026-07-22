"""인기 상품 조회 — 인기 구좌, 개인화·연관의 인기 신호와 콜드스타트 재료.

위생 필터는 서빙 시점에 소비하는 쪽이 적용하므로, 여기서는 그대로 반환한다.
"""

from typing import Any

from psycopg_pool import AsyncConnectionPool

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
