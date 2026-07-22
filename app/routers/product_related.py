import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import RelatedResponse
from app.services import popular, related

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/{product_seq}/related")
async def related_products(
    request: Request,
    product_seq: int,
    pool: PoolDep,
    limit: LimitParam = 10,
) -> RelatedResponse:
    limit = clamp_limit(limit)

    # 1) anchor 확정 — 존재하지 않는 상품은 404 (없는 리소스의 하위 자원)
    anchor = await related.fetch_anchor(pool, product_seq)
    if anchor is None:
        raise HTTPException(status_code=404, detail="product not found")

    # 2) CF 신호와 카테고리 인기는 서로 독립 — 동시 취득
    cf_rows, popular_rows = await asyncio.gather(
        related.fetch_cf(pool, product_seq),
        popular.fetch_by_category(pool, anchor["category_seq"], related.POPULAR_TOP_K_RECALL),
    )

    result = await related.build_related_result(pool, product_seq, limit, cf_rows, popular_rows)
    return RelatedResponse(result=record_count(request, result))
