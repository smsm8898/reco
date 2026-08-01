import asyncio

from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.response import ApiResponse
from app.services import bad_seller, related

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/{product_seq}/related", description="연관 상품 추천")
async def related_products(
    request: Request,
    product_seq: int,
    pool: PoolDep,
    limit: LimitParam = 10,
) -> ApiResponse:
    """지금 보고 있는 상품(anchor)과 함께 소비되는 상품.

    계약:
    - 없는 `product_seq`(stale·오타)도 에러가 아니라 **빈 결과 200** — anchor 미존재를 따로
      구분하지 않는다(카테고리를 못 찾은 것과 같은 취급).
    - `limit` 은 범위 밖이면 clamp(1~50). anchor 자신은 결과에서 제외된다.
    - 판매중지 상품도 유효한 anchor — 그 상품 페이지에서 지면이 뜨므로. 위생은 추천 *결과*에만 건다.

    파이프라인 (라우터는 취득만, 조립은 서비스 몫):
    1. fetch_anchor_info / get_bad_sellers 동시 취득 — 서로 독립. anchor 조회가 레벨별(lv2/3/4)
       카테고리 pivot 을 겸한다(popular cascade 의 입력).
    2. fetch_cf / fetch_popular_cascade 동시 취득 — cascade 는 anchor 카테고리에 의존해
       1단계 뒤로 온다. L4→L3→L2 로 dedup 누적하며 좁은 leaf 부터 채운다.
    3. build_related_result 가 위생·셀러 정책 → CF 신호 3개 ⊕ popular RRF 융합 → self 제외
       → same-seller cap → top-limit.
    """
    limit = clamp_limit(limit)

    anchor_cats, blocklist = await asyncio.gather(
        related.fetch_anchor_info(pool, product_seq),
        bad_seller.get_bad_sellers(pool),
    )

    cf_rows, popular_rows = await asyncio.gather(
        related.fetch_cf(pool, product_seq),
        related.fetch_popular_cascade(pool, anchor_cats),
    )

    result = await related.build_related_result(
        pool, product_seq, limit, cf_rows, popular_rows, blocklist
    )
    return ApiResponse(result=record_count(request, result))
