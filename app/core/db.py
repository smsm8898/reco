from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from app.core.settings import settings


def create_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.pg_conninfo(),
        min_size=settings.PG_POOL_MIN_SIZE,
        max_size=settings.PG_POOL_MAX_SIZE,
        kwargs=settings.pg_serving_kwargs(),  # 커넥션마다 statement_timeout 적용
        open=False,
    )


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool
