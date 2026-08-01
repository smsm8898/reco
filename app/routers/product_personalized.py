import asyncio

from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.response import ApiResponse
from app.services import bad_seller, personalize, popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])

OVERSAMPLE = 2

@router.get("/personalized", description="개인화 상품 추천")
async def personalized_products(
    request: Request,
    pool: PoolDep,
    user_seq: int = None,
    limit: LimitParam = 20,
) -> ApiResponse:
    """이 유저의 취향에 맞춘 상품 — CF 2원 융합, 부족분은 인기로 뒤채운다.

    계약:
    - `user_seq` 없거나 0 이하면 게스트 — DB 조회 없이(fast-path) 전역 인기 콜드스타트로 응답한다.
      로그인했지만 이력이 없어도 같은 경로다. 콜드스타트 여부는 응답에 드러나지 않는다.
    - `limit` 은 범위 밖이면 clamp(1~50). 후보는 `limit × OVERSAMPLE` 만큼 넉넉히 취득해
      위생·셀러 탈락분을 흡수한다.

    파이프라인 (라우터는 취득만, 조립은 서비스 몫):
    1. fetch_user_info / get_bad_sellers / 전역 인기 동시 취득 — 셋 다 독립. 프로필이 없으면
       그 자리에서 콜드스타트로 끝난다(이미 받아둔 전역 인기를 그대로 쓴다).
    2. user CF / item CF / 선호 카테고리 인기 동시 취득 — 셋 다 프로필에만 의존해 서로 독립.
    3. build_personalized_result 가 위생·셀러 정책 → CF 2원 RRF 융합 → 시드(이미 본 상품) 제외
       → 부족분 카테고리 인기 backfill → seller spread.

    인기를 융합이 아니라 **backfill 로만** 쓰는 것이 이 지면의 선택 — RRF 에 넣으면 범용 인기가
    CF 와 순위를 겨뤄 개인화가 희석된다(연관 지면은 반대로 융합에 넣는다).
    """
    limit = clamp_limit(limit)
    fetch_limit = limit * OVERSAMPLE

    user_info, blocklist, category_popular = await asyncio.gather(
        personalize.fetch_user_info(pool, user_seq),
        bad_seller.get_bad_sellers(pool),
        popular.fetch_popular_by_category(pool, fetch_limit=fetch_limit),
    )
    if user_info is None:  # cold-start — CF 후보가 없으니 전량 인기 backfill 로 채워진다
        result = await personalize.build_personalized_result(
            pool, limit=limit, blocklist=blocklist, category_popular=category_popular
        )
        return ApiResponse(result=record_count(request, result))

    user_cf, item_cf, category_popular = await asyncio.gather(
        personalize.get_cf_by_user(pool, user_info["user_seq"], fetch_limit),
        personalize.get_cf_by_product(pool, user_info["preferred_product_seq"], fetch_limit),
        popular.fetch_popular_by_category(
            pool, fetch_limit=fetch_limit, category_seq=user_info["preferred_category_seq"]
        ),
    )


    result = await personalize.build_personalized_result(
        pool,
        limit=limit,
        blocklist=blocklist,
        category_popular=category_popular,
        user_cf=user_cf,
        item_cf=item_cf,
        seed=user_info["seed_product_seqs"],
    )
    return ApiResponse(result=record_count(request, result))
