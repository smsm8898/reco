"""서빙 시점 후보 필터 — 세 지면(related·popular·personalized)이 공유하는 단 하나의 관문.

거르는 판단은 둘이고 순서가 고정돼 있다:
1. **상품 상태**(위생 9-rule) — 품절·미노출·판매기간처럼 분 단위로 변하는 값이라 batch 가 아니라
   서빙이 본다.
2. **셀러 정책** — 전역 차단 셀러(`bad_seller.get_bad_sellers`) 제외. 셀러 판정은 1번이 붙여주는
   `seller_seq` 가 있어야 가능해서 뒤집을 수 없다.
"""

from typing import Any

from psycopg_pool import AsyncConnectionPool

OVERSAMPLE = 2

_VALID_SQL = """
    SELECT product_seq, seller_seq
    FROM service_db.product_info
    WHERE product_seq = ANY(%s)
      AND expose = 'Y'
      AND punished = 'N'
      AND deleted = 'N'
      AND coalesce(excluded, 'N') <> 'Y'
      AND selling_price > 0
      AND char_length(product_name) >= 2
      AND (start_at IS NULL OR start_at <= now())
      AND (end_at IS NULL OR end_at >= now())
      AND (stock_count IS NULL OR stock_count > 0)
"""


async def filter_valid(
    pool: AsyncConnectionPool, *row_lists: list[dict[str, Any]], blocklist: set[int]
) -> dict[int, int]:
    """후보 목록 여러 벌 → 통과분 `{product_seq: seller_seq}`. 상품 위생 → 셀러 정책 순.

    - 쿼리: 목록을 전부 합쳐 **1 쿼리**. 빈 입력은 무쿼리.
    - 키 부재 = 탈락(위생 미통과이거나 차단 셀러). 소비자는 `in` 으로 거르고, seller_seq 가
      필요하면 `with_seller` 로 붙인다.
    """
    seqs = list({row["product_seq"] for rows in row_lists for row in rows})
    if not seqs:
        return {}
    async with pool.connection() as conn:
        cur = await conn.execute(_VALID_SQL, (seqs,))
        rows = await cur.fetchall()
    return {seq: seller_seq for seq, seller_seq in rows if seller_seq not in blocklist}
