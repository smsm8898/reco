from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import Interval, PopularResponse
from app.services import popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/popular")
async def popular_products(
    request: Request,
    pool: PoolDep,
    category_seq: int,
    interval: Interval = Interval.WEEK,
    limit: LimitParam = 20,
) -> PopularResponse:
    """카테고리 인기 — interval dial(day/week/month)로 감쇠·창을 고른 신호별 HN 랭킹.

    universe(30일 hourly bucket)를 interval의 window로 잘라 신호 4종을 각각 감쇠
    합산하고, 위생 필터 후 RRF로 융합한다. 마지막의 seller spread는 truncation 후 —
    순서만 바꾸고 선택은 안 바꾼다. 잘못된 interval은 enum 검증으로 422.
    """
    limit = clamp_limit(limit)
    rows = await popular.fetch_popular(pool, category_seq, interval)
    result = await popular.build_popular_result(pool, rows, interval=interval, limit=limit)
    return PopularResponse(result=record_count(request, result))
