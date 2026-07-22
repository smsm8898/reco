from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import PopularResponse, to_result
from app.ranking import hacker_news_rank
from app.services import hygiene, popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/popular")
async def popular_products(
    request: Request,
    pool: PoolDep,
    category_seq: int,
    limit: LimitParam = 20,
) -> PopularResponse:
    """카테고리 주간 인기 — batch 주간 후보를 HN 시간감쇠로 랭킹 후 위생 필터.

    후보 풀은 batch가 이미 카테고리당 상한으로 잘라놨으므로(recall), oversample 없이
    전체를 위생 필터하고 top-limit을 자른다 — 한 쿼리, limit이 작아도 빈 응답 없음.
    """
    limit = clamp_limit(limit)
    ranked = hacker_news_rank(await popular.fetch_weekly_by_category(pool, category_seq))
    (valid,) = await hygiene.filter_valid(pool, ranked)
    return PopularResponse(result=record_count(request, to_result(valid[:limit])))
