from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from app.core.db import create_pool, get_pool
from app.core.logging_config import setup_logging
from app.core.settings import settings
from app.observability import setup_observability
from app.routers.product_personalized import router as personalized_router
from app.routers.product_popular import router as popular_router
from app.routers.product_related import router as related_router

# 다른 로거 사용 전에 1회 설정 (structlog cache_logger_on_first_use)
setup_logging(
    service_name=settings.SERVICE_NAME,
    service_version=settings.SERVICE_VERSION,
    env=settings.ENV,
    json_logs=settings.ENV != "local",
    level=settings.LOG_LEVEL,
)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 부팅 설정 스냅샷 — 장애 조사 시 "그때 그 pod이 어떤 지면을 켜고 떴나" 재구성용
    logger.info(
        "startup",
        log_level=settings.LOG_LEVEL,
        enable_related=settings.ENABLE_RELATED,
        enable_personalized=settings.ENABLE_PERSONALIZED,
        enable_popular=settings.ENABLE_POPULAR,
    )
    pool = create_pool()
    app.state.pool = pool
    await pool.open()
    yield
    logger.info("shutdown")
    await pool.close()


app = FastAPI(title="reco", lifespan=lifespan)
setup_observability(app)

# 구좌별 라우터를 플래그로 조건부 마운트 — 같은 이미지를 지면별 Deployment로 나눠
# 독립 스케일링하기 위한 이음매 (gitops에서 지면마다 하나만 켠다). 전부 on이면 모놀리스.
if settings.ENABLE_PERSONALIZED:
    app.include_router(personalized_router)
if settings.ENABLE_POPULAR:
    app.include_router(popular_router)
if settings.ENABLE_RELATED:
    app.include_router(related_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(pool: Annotated[AsyncConnectionPool, Depends(get_pool)]):
    try:
        async with pool.connection(timeout=2.0) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        logger.exception("readiness check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "postgres": "unavailable"},
        )
    return {"status": "ready", "postgres": "ok"}
