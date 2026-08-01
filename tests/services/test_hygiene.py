"""후보 필터 — 세 지면이 공유하는 단 하나의 관문.

핵심 테스트는 **정답지 등가**다: 위생 9-rule SQL + 셀러 정책이 파이썬 정답지와 전 상품에서
같은 판정을 내리는지. rule 을 하나 빼먹거나 예측어를 잘못 옮기면(coalesce 누락, char_length
경계 등) 여기서 깨진다.
"""

import asyncio

from psycopg_pool import AsyncConnectionPool

from app.services import hygiene
from scripts.generate_data import Dataset
from tests.conftest import bad_seller_seqs, db_url, needs_db, seller_of, servable_seqs


def test_empty_input_skips_the_query() -> None:
    """후보가 비면 DB를 아예 치지 않는다 — pool 자리에 None을 줘도 통과하는 것이 그 증거.

    빈 후보는 실제로 흔하다(CF 신호 없는 anchor, 카테고리 매칭 실패). 왕복 한 번을 아낀다.
    """
    assert asyncio.run(hygiene.filter_valid(None, [], blocklist=set())) == {}


@needs_db
def test_filter_valid_agrees_with_answer_key(dataset: Dataset) -> None:
    """위생 9-rule + 셀러 정책의 판정이 정답지와 전 상품에서 일치한다."""
    candidates = [{"product_seq": row[0]} for row in dataset.products]
    blocked = bad_seller_seqs(dataset)

    async def run() -> dict[int, int]:
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            return await hygiene.filter_valid(pool, candidates, blocklist=blocked)
        finally:
            await pool.close()

    valid = asyncio.run(run())
    expected = servable_seqs(dataset)

    assert set(valid) == expected
    assert 0 < len(expected) < len(dataset.products)  # 실제로 거르되 전멸시키지는 않는다
    assert blocked  # 정답지에 차단 셀러가 있어야 셀러 정책 부분이 검증된다


@needs_db
def test_filter_valid_returns_ledger_seller(dataset: Dataset) -> None:
    """값은 원장(product_info) seller_seq — 다양성 정책이 이 맵에서 셀러를 읽는다."""
    candidates = [{"product_seq": row[0]} for row in dataset.products]
    expected_seller = seller_of(dataset)

    async def run() -> dict[int, int]:
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            return await hygiene.filter_valid(pool, candidates, blocklist=set())
        finally:
            await pool.close()

    valid = asyncio.run(run())

    assert valid  # 비어 있으면 아래 단정이 공허하다
    assert all(valid[seq] == expected_seller[seq] for seq in valid)


@needs_db
def test_filter_valid_merges_lists_into_one_query(dataset: Dataset) -> None:
    """여러 벌을 합쳐 판정한다 — 목록이 나뉘어도 결과는 합집합 기준 한 벌이다."""
    seqs = [row[0] for row in dataset.products]
    first = [{"product_seq": s} for s in seqs[:50]]
    second = [{"product_seq": s} for s in seqs[50:100]]

    async def run() -> tuple[dict[int, int], dict[int, int]]:
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            together = await hygiene.filter_valid(pool, first, second, blocklist=set())
            apart_a = await hygiene.filter_valid(pool, first, blocklist=set())
            apart_b = await hygiene.filter_valid(pool, second, blocklist=set())
            return together, apart_a | apart_b
        finally:
            await pool.close()

    together, apart = asyncio.run(run())

    assert together == apart
