"""개인화 추천 서빙 — batch(user_info·user_product_cf)가 준비한 프로필과 후보를 융합한다.

router(product_personalized.py)의 실제 호출 순서:
1. fetch_user_info — 프로필(성별·출생연도·선호 카테고리·선호 상품) + 시드 목록을 한 쿼리로
   (게스트·무이력이면 None → 콜드스타트, 게스트 판정은 이 신호 계층의 fast-path)
2. get_cf_by_user / get_cf_by_product(선호 상품) / popular.fetch_by_category(선호 카테고리)를
   asyncio.gather로 동시 취득 (서로 독립)
3. router가 위생 필터 → RRF 융합 → 시드(이미 본 상품) 제외 → top-limit
"""

from typing import Any

from psycopg_pool import AsyncConnectionPool

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
