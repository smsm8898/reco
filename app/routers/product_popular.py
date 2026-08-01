import asyncio

from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import Interval
from app.models.response import ApiResponse
from app.services import bad_seller, popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/popular", description="인기 상품 추천")
async def popular_products(
    request: Request,
    pool: PoolDep,
    category_seq: int,
    interval: Interval = Interval.WEEK,
    limit: LimitParam = 20,
) -> ApiResponse:
    """카테고리에서 지금 뜨는 상품 — interval dial 로 "지금"을 보는 관점을 고른다.

    계약:
    - `category_seq` 필수. `interval` 은 day/week/month (그 외 값은 enum 검증으로 422).
    - `limit` 은 범위 밖이면 에러 대신 clamp(1~50) — 잘못된 값에도 지면은 뜬다.

    파이프라인 (라우터는 취득만, 조립은 서비스 몫):
    1. fetch_popular / get_bad_sellers 동시 취득 — 서로 독립. fetch_popular 가 universe(30일
       hourly bucket)를 interval window 로 잘라 읽고 신호 4종 HN 감쇠 점수화까지 끝낸다.
    2. build_popular_result 가 위생·셀러 정책 → 신호별 RRF 융합 → top-limit → seller spread.
       spread 가 truncation 뒤인 것이 요점 — 순서만 바꾸고 어떤 상품이 뽑혔는지는 안 바꾼다.
    """
    limit = clamp_limit(limit)
    # fetch_popular 가 window 자르기·감쇠 점수화까지 끝낸 product-grain 후보를 준다
    products, blocklist = await asyncio.gather(
        popular.fetch_popular(pool, category_seq, interval),
        bad_seller.get_bad_sellers(pool),
    )
    result = await popular.build_popular_result(
        pool, products, limit=limit, blocklist=blocklist
    )
    return ApiResponse(result=record_count(request, result))
