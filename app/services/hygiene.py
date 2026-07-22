"""서빙 시점 위생 필터.

상품 상태(판매중지 등)는 실시간으로 변하므로 batch가 아니라 서빙에서 거른다.
필터를 통과한 row에는 seller_seq가 병합된다 — same-seller cap 판단용 (응답엔 미포함).
"""

from typing import Any

from psycopg_pool import AsyncConnectionPool

OVERSAMPLE = 2  # 소비자가 후보를 취득할 때 위생 탈락분을 흡수할 여유 배수

# 원장 기준 위생 5-rule: 노출 · 미만료 · 미삭제 · 가격 > 0 · 재고(NULL = 무제한)
_VALID_SQL = """
    SELECT product_seq, seller_seq
    FROM service_db.product_info
    WHERE product_seq = ANY(%s)
      AND expose = 'Y'
      AND expired = 'N'
      AND deleted = 'N'
      AND selling_price > 0
      AND (stock_count IS NULL OR stock_count > 0)
"""


async def filter_valid(
    pool: AsyncConnectionPool,
    *row_lists: list[dict[str, Any]],
    id_key: str = "product_seq",
) -> tuple[list[dict[str, Any]], ...]:
    """여러 후보 목록을 합쳐 한 번의 쿼리로 위생 검사한다."""
    seqs = list({row[id_key] for rows in row_lists for row in rows})
    valid: dict[int, int] = {}
    if seqs:
        async with pool.connection() as conn:
            cur = await conn.execute(_VALID_SQL, (seqs,))
            valid = {product_seq: seller_seq for product_seq, seller_seq in await cur.fetchall()}

    def keep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**row, "seller_seq": valid[row[id_key]]} for row in rows if row[id_key] in valid]

    return tuple(keep(rows) for rows in row_lists)
