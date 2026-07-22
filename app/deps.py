from typing import Annotated

from fastapi import Depends, Query, Request
from psycopg_pool import AsyncConnectionPool

from app.core.db import get_pool

PoolDep = Annotated[AsyncConnectionPool, Depends(get_pool)]
LimitParam = Annotated[int, Query()]

MAX_LIMIT = 50


def clamp_limit(limit: int) -> int:
    """limit이 범위(1~MAX_LIMIT) 밖이면 에러 대신 자동 clamp — 잘못된 값에도 지면은 뜬다."""
    return max(1, min(limit, MAX_LIMIT))


def record_count(request: Request, result: list[int]) -> list[int]:
    """추천 결과 수를 access 로그(reco.result_count)에 남기고 result를 그대로 돌려준다.

    세 구좌가 공유하는 cross-cutting 관심사 — 응답 조립 한 줄에 끼워 쓴다:
    `XResponse(result=record_count(request, rows))`.
    """
    request.state.reco_result_count = len(result)
    return result
