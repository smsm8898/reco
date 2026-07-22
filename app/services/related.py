"""연관 상품 서빙 — batch(recall)가 모아둔 신호를 요청 시점에 융합(ranking)한다.

router(product_related.py)의 실제 호출 순서:
1. fetch_anchor — 존재 확인(미존재 404) + 카테고리 취득을 한 쿼리로
2. fetch_cf / popular.fetch_by_category(anchor 카테고리)를 asyncio.gather로 동시 취득 (서로 독립)
3. build_related_result 가 위생 필터 → 신호 3개 ⊕ popular RRF 융합 → self 제외
   → same-seller cap → top-limit 으로 product_seq 리스트를 만든다 (응답 조립은 라우터 몫)
"""

from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.models.products import to_result
from app.ranking import apply_rrf, apply_same_seller_cap
from app.services import hygiene

# =========== CONSTANT ===========
SAME_SELLER_CAP = 5  # 결과 내 same-seller 최대 (다양성 vs 같은 셀러 연관성)
RRF_K = 60  # Reciprocal Rank Fusion k
POPULAR_TOP_K_RECALL = 100  # popular 후보 취득 상한(oversample)


# =========== SQL ===========
_ANCHOR_SQL = """
    SELECT p.product_seq, pc.category_seq
    FROM service_db.product_info p
    LEFT JOIN service_db.product_category pc USING (product_seq)
    WHERE p.product_seq = %s
"""

_CF_SQL = """
    SELECT related_product_seq AS product_seq, num_view, num_cart, num_order
    FROM mart.product_related
    WHERE product_seq = %s
"""

# =========== Fetch ===========


async def fetch_anchor(pool: AsyncConnectionPool, product_seq: int) -> dict[str, Any] | None:
    """anchor 조회 — 미존재면 None (router가 not-found 처리)."""
    async with pool.connection() as conn:
        cur = await conn.execute(_ANCHOR_SQL, (product_seq,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {"product_seq": row[0], "category_seq": row[1]}


async def fetch_cf(pool: AsyncConnectionPool, product_seq: int) -> list[dict[str, Any]]:
    """anchor와 co-occurrence 신호가 있는 후보들 (랭킹 없음 — 카운트만)."""
    async with pool.connection() as conn:
        cur = await conn.execute(_CF_SQL, (product_seq,))
        rows = await cur.fetchall()
    return [
        {"product_seq": r[0], "num_view": r[1], "num_cart": r[2], "num_order": r[3]} for r in rows
    ]


# =========== Algorithm ===========


def _derive_source_rankings(
    cf_rows: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """CF 신호를 view/cart/order 3개의 ranked list로 분해한다 (RRF 입력, 컬럼명 = source label)."""
    rankings: list[tuple[str, list[dict[str, Any]]]] = []
    for col in ("num_view", "num_cart", "num_order"):
        ranked = sorted(
            (r for r in cf_rows if r[col] > 0),
            key=lambda r, c=col: (-r[c], r["product_seq"]),
        )
        rankings.append((col, ranked))
    return rankings


async def build_related_result(
    pool: AsyncConnectionPool,
    product_seq: int,
    limit: int,
    cf_rows: list[dict[str, Any]],
    popular_rows: list[dict[str, Any]],
) -> list[int]:
    """위생 필터 → RRF 융합 → self 제외 → same-seller cap → top-limit → product_seq 리스트."""
    cf_rows, popular_rows = await hygiene.filter_valid(pool, cf_rows, popular_rows)

    fused = apply_rrf(
        _derive_source_rankings(cf_rows) + [("popular", popular_rows)],
        k=RRF_K,
        id_key="product_seq",
    )

    fused = [r for r in fused if r["product_seq"] != product_seq]
    capped = apply_same_seller_cap(fused, cap=SAME_SELLER_CAP)[:limit]

    return to_result(capped)
