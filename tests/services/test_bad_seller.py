"""전역 차단 셀러 신호 — SSOT 쿼리가 정답지와 같은 집합을 내는지."""

import asyncio

from psycopg_pool import AsyncConnectionPool

from app.services.bad_seller import get_bad_sellers
from scripts.generate_data import Dataset
from tests.conftest import bad_seller_seqs, db_url, needs_db


def _fetch() -> set[int]:
    async def run() -> set[int]:
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            return await get_bad_sellers(pool)
        finally:
            await pool.close()

    return asyncio.run(run())


@needs_db
def test_returns_only_lowest_grade_sellers(dataset: Dataset) -> None:
    """result=0 셀러만 — 10·20(정상)이 섞이면 정상 셀러가 통째로 배제된다."""
    expected = bad_seller_seqs(dataset)

    blocked = _fetch()

    assert blocked == expected
    assert expected  # 정답지가 비어 있으면 이 테스트가 공허하다
    assert len(expected) < len({row[1] for row in dataset.products})  # 전멸시키지 않는다
