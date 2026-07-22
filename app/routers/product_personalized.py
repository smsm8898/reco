import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import PersonalizedResponse, to_result
from app.ranking import apply_rrf
from app.services import hygiene, personalize, popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/personalized")
async def personalized_products(
    request: Request,
    pool: PoolDep,
    user_seq: Annotated[int | None, Query()] = None,
    limit: LimitParam = 20,
) -> PersonalizedResponse:
    limit = clamp_limit(limit)
    fetch_limit = limit * hygiene.OVERSAMPLE

    # 1) 유저 프로필 — 게스트(user_seq 없음/0 이하)·이력 없음이면 전역 인기 콜드스타트로 끝
    user_info = await personalize.fetch_user_info(pool, user_seq)
    if user_info is None:
        (rows,) = await hygiene.filter_valid(pool, await popular.fetch(pool, fetch_limit))
        return PersonalizedResponse(result=record_count(request, to_result(rows[:limit])))

    # 2) CF 후보 2원(user·item)과 뒤채움용 선호 카테고리 인기 — 서로 독립이라 동시 취득
    user_cf, item_cf, category_popular = await asyncio.gather(
        personalize.get_cf_by_user(pool, user_info["user_seq"], fetch_limit),
        personalize.get_cf_by_product(pool, user_info["preferred_product_seq"], fetch_limit),
        popular.fetch_by_category(pool, user_info["preferred_category_seq"], fetch_limit),
    )

    # 3) 위생 필터 — 세 목록을 한 쿼리로
    user_cf, item_cf, category_popular = await hygiene.filter_valid(
        pool, user_cf, item_cf, category_popular
    )

    # 4) CF 2원만 RRF 융합 → 시드(이미 본 상품) 제외 → 부족분은 카테고리 인기로 뒤채움
    fused = apply_rrf([("user_cf", user_cf), ("item_cf", item_cf)], id_key="product_seq")
    exclude = user_info["seed_product_seqs"]
    picked = [r for r in fused if r["product_seq"] not in exclude][:limit]
    if len(picked) < limit:
        seen = {r["product_seq"] for r in picked} | exclude
        picked += [r for r in category_popular if r["product_seq"] not in seen][
            : limit - len(picked)
        ]

    return PersonalizedResponse(result=record_count(request, to_result(picked)))
