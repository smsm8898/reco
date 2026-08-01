"""연관 상품 서빙 — batch(recall)가 모아둔 신호를 요청 시점에 융합(ranking)한다.

router(product_related.py)의 실제 호출 순서:
1. fetch_anchor_info(레벨별(lv2/3/4) 카테고리 pivot — 미존재도 빈 dict) /
   bad_seller.get_bad_sellers(전역 차단 셀러)를 asyncio.gather로 동시 취득 — 서로 독립
2. fetch_cf / fetch_popular_cascade(L4→L3→L2 dedup 누적)를 asyncio.gather로 동시 취득
3. build_related_result 가 위생·셀러 정책 → 신호 3개 ⊕ popular RRF 융합 → self 제외
   → same-seller cap → top-limit 으로 product_seq 리스트를 만든다 (응답 조립은 라우터 몫)
"""

from typing import Any

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.models.response import to_result
from app.ranking import RRF_K, apply_rrf, apply_same_seller_cap, derive_source_rankings
from app.services import hygiene, popular

logger = structlog.get_logger(__name__)

# =========== CONSTANT ===========
SAME_SELLER_CAP = 5  # 결과 내 same-seller 최대 (다양성 vs 같은 셀러 연관성)
POPULAR_TOP_K_RECALL = 100  # popular 후보 취득 상한(oversample)

# anchor pivot 이 싣는 카테고리 레벨 — SQL 컬럼 alias(lv{level}_category_seq)와 짝이다.
# cascade 는 이 역순(좁은 leaf 부터)으로 순회한다.
ANCHOR_LEVELS: tuple[int, ...] = (2, 3, 4)

# RRF source 컬럼 — 컬럼명을 그대로 source label 로 쓴다
_CF_SIGNAL_COLS: tuple[str, ...] = ("num_view", "num_cart", "num_order")


# =========== SQL ===========
# 존재 확인 + 레벨별 카테고리 취득을 한 쿼리로 — 상품당 3행인 매핑을 MAX(CASE ...) 로 접어
# 한 행 3컬럼(pivot)으로 만든다. LEFT JOIN이라 매핑 없는 상품도 1행 — 레벨 컬럼만 NULL이 된다.
# 컬럼 alias `lv{level}_category_seq` 는 fetch_anchor_info 가 이름으로 되접는 계약이다.
_ANCHOR_SQL = """
    SELECT p.product_seq,
           MAX(CASE WHEN c.level = 2 THEN c.category_seq END) AS lv2_category_seq,
           MAX(CASE WHEN c.level = 3 THEN c.category_seq END) AS lv3_category_seq,
           MAX(CASE WHEN c.level = 4 THEN c.category_seq END) AS lv4_category_seq
    FROM service_db.product_info p
    LEFT JOIN service_db.product_category pc USING (product_seq)
    LEFT JOIN service_db.category c USING (category_seq)
    WHERE p.product_seq = %s
    GROUP BY p.product_seq
"""

_CF_SQL = """
    SELECT related_product_seq AS product_seq, num_view, num_cart, num_order
    FROM mart.product_related
    WHERE product_seq = %s
"""

# =========== Fetch ===========


async def fetch_anchor_info(pool: AsyncConnectionPool, product_seq: int) -> dict[int, int]:
    """anchor의 레벨별 카테고리 `{level: category_seq}` — 없으면 빈 dict.

    "카테고리를 못 찾았다"는 상황을 한 가지로 만든다: 상품이 없든(stale·오타 seq) 매핑이 없든
    결과는 `{}` 이고, cascade 가 popular 를 미제공하면 CF 만으로 응답한다. 미존재를 별도 값으로
    구분하지 않으므로 호출부에 분기가 없다.

    저장 모양(pivot 된 wide row)을 도메인 모양(레벨 → 카테고리)으로 여기서 되접는다 — 그래야
    cascade 가 SQL 컬럼 구조를 모르고 레벨 우선순위(L4→L3→L2)만 알면 된다. 컬럼은 인덱스가
    아니라 이름으로 읽는다: SELECT 에 컬럼이 끼어들어도 조용히 어긋나지 않는다.
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ANCHOR_SQL, (product_seq,))
        row = await cur.fetchone()
    if row is None:
        return {}
    return {
        level: int(row[f"lv{level}_category_seq"])
        for level in ANCHOR_LEVELS
        if row[f"lv{level}_category_seq"] is not None
    }


async def fetch_cf(pool: AsyncConnectionPool, product_seq: int) -> list[dict[str, Any]]:
    """anchor와 co-occurrence 신호가 있는 후보들 (랭킹 없음 — 카운트만)."""
    async with pool.connection() as conn:
        cur = await conn.execute(_CF_SQL, (product_seq,))
        rows = await cur.fetchall()
    return [
        {"product_seq": r[0], "num_view": r[1], "num_cart": r[2], "num_order": r[3]} for r in rows
    ]


async def fetch_popular_cascade(
    pool: AsyncConnectionPool, anchor_cats: dict[int, int]
) -> list[dict[str, Any]]:
    """popular cascade — L4→L3→L2 dedup 누적, POPULAR_TOP_K_RECALL 채우면 조기 중단.

    좁은 leaf(lv4)의 인기를 우선하되 얇으면 상위 레벨이 backfill한다. 매치되는
    카테고리가 없으면 popular 미제공 — global fallback은 두지 않는다(무관 상품이
    '연관' 지면에 섞이는 것보다 빈 신호가 낫다). hygiene 미적용 — build_related_result 몫.
    """
    popular_rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for level in reversed(ANCHOR_LEVELS):  # 좁은 leaf(lv4)부터 — 얇으면 상위가 backfill
        if len(popular_rows) >= POPULAR_TOP_K_RECALL:
            break
        category_seq = anchor_cats.get(level)
        if category_seq is None:
            continue
        level_rows = await popular.fetch_popular_by_category(
            pool, fetch_limit=POPULAR_TOP_K_RECALL, category_seq=category_seq
        )
        for row in level_rows:
            if row["product_seq"] in seen:
                continue
            seen.add(row["product_seq"])
            popular_rows.append(row)
            if len(popular_rows) >= POPULAR_TOP_K_RECALL:
                break
    return popular_rows


# =========== Algorithm ===========


async def build_related_result(
    pool: AsyncConnectionPool,
    product_seq: int,
    limit: int,
    cf_rows: list[dict[str, Any]],
    popular_rows: list[dict[str, Any]],
    blocklist: set[int],
) -> list[int]:
    """위생·셀러 정책 → RRF 융합 → self 제외 → same-seller cap → top-limit → product_seq 리스트."""
    # valid: 통과 상품 → 셀러. 거르는 데 쓰고(in), same-seller cap 이 셀러 조회에 그대로 쓴다
    valid = await hygiene.filter_valid(pool, cf_rows, popular_rows, blocklist=blocklist)
    cf_rows = [r for r in cf_rows if r["product_seq"] in valid]
    popular_rows = [r for r in popular_rows if r["product_seq"] in valid]

    fused = apply_rrf(
        derive_source_rankings(cf_rows, _CF_SIGNAL_COLS) + [("popular", popular_rows)],
        k=RRF_K,
        id_key="product_seq",
    )

    fused = [r for r in fused if r["product_seq"] != product_seq]
    capped = apply_same_seller_cap(fused, cap=SAME_SELLER_CAP, seller_of=valid)[:limit]

    result = to_result(capped)
    if not result:
        # 유효 anchor 인데 결과 0 — 신호·popular 가 모두 비어 파이프라인이 빈손. soft 실패
        # 신호(에러도 RED 메트릭도 안 잡는다) — 카운트는 메트릭 몫.
        logger.warning(
            "related empty",
            product_seq=product_seq,
            limit=limit,
            valid_cf=len(cf_rows),
            valid_popular=len(popular_rows),
        )
    return result
