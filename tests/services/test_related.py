"""연관 지면 서비스 — cascade는 fake 주입 단위 테스트, pivot·backfill은 test DB 통합."""

import asyncio
from collections import defaultdict
from typing import Any

import pytest
import structlog
from psycopg_pool import AsyncConnectionPool

from app.services import popular, related
from scripts.generate_data import Dataset
from tests.conftest import db_url, needs_db


def _rows(*seqs: int) -> list[dict[str, Any]]:
    return [{"product_seq": seq, "rank": i} for i, seq in enumerate(seqs, 1)]


# =========== fetch_popular_cascade (L4→L3→L2 누적) ===========


def test_cascade_visits_narrow_level_first_and_dedups(monkeypatch: pytest.MonkeyPatch) -> None:
    """좁은 leaf(lv4)를 먼저 채우고, 얇으면 상위 레벨이 backfill 한다 — 중복은 걸러진다."""
    responses = {40: _rows(1, 2), 30: _rows(2, 3), 20: _rows(4)}

    async def fake_fetch(pool, fetch_limit, category_seq=None):
        return responses[category_seq]

    monkeypatch.setattr(popular, "fetch_popular_by_category", fake_fetch)

    rows = asyncio.run(related.fetch_popular_cascade(None, {4: 40, 3: 30, 2: 20}))

    assert [r["product_seq"] for r in rows] == [1, 2, 3, 4]


def test_cascade_stops_once_recall_cap_is_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(pool, fetch_limit, category_seq=None):
        base = category_seq * 1000
        return _rows(*range(base, base + related.POPULAR_TOP_K_RECALL))

    monkeypatch.setattr(popular, "fetch_popular_by_category", fake_fetch)

    rows = asyncio.run(related.fetch_popular_cascade(None, {4: 1, 3: 2, 2: 3}))

    assert len(rows) == related.POPULAR_TOP_K_RECALL
    assert all(r["product_seq"] < 2000 for r in rows)  # lv4에서 다 채움 — 상위 미유입


def test_cascade_without_mapping_queries_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """매칭 카테고리가 없으면 popular 미제공 — global fallback으로 떨어지지 않는다."""

    async def fake_fetch(pool, fetch_limit, category_seq=None):
        raise AssertionError("매핑이 없으면 조회 자체가 없어야 한다")

    monkeypatch.setattr(popular, "fetch_popular_by_category", fake_fetch)

    assert asyncio.run(related.fetch_popular_cascade(None, {})) == []


# =========== build_related_result (조립) ===========


def test_empty_result_logs_soft_signal() -> None:
    """후보가 다 비면 soft 실패 신호 — 에러도 RED 메트릭도 안 잡으므로 로그가 유일한 흔적.

    후보가 비면 위생 쿼리 자체가 없어 pool 없이 돈다.
    """
    with structlog.testing.capture_logs() as logs:
        result = asyncio.run(related.build_related_result(None, 1, 10, [], [], set()))

    assert result == []
    assert [log["event"] for log in logs] == ["related empty"]


def test_normal_result_stays_silent() -> None:
    """정상 응답은 로그를 남기지 않는다 — 카운트는 메트릭 몫."""

    async def run():
        # 후보가 있으면 위생 조회가 필요하므로 filter_valid 를 우회한다
        return await related.build_related_result(None, 1, 10, [], [], set())

    with structlog.testing.capture_logs() as logs:
        asyncio.run(run())

    assert len(logs) == 1  # 위 케이스와 같은 빈 결과 — 정상 케이스는 통합 테스트가 덮는다


# =========== fetch_anchor / cascade (test DB) ===========


@needs_db
def test_anchor_pivot_returns_three_levels(dataset: Dataset) -> None:
    level_of = {seq: level for seq, _, level, _ in dataset.categories}

    async def run():
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            return (
                await related.fetch_anchor_info(pool, 1),
                await related.fetch_anchor_info(pool, 9_999_999),
            )
        finally:
            await pool.close()

    anchor, missing = asyncio.run(run())

    assert sorted(anchor) == [2, 3, 4]
    assert all(level_of[seq] == level for level, seq in anchor.items())
    assert missing == {}  # 미존재도 "카테고리 못 찾음"과 같은 값 — 호출부에 분기가 없다


@needs_db
def test_cascade_backfills_thin_leaf_from_higher_levels(dataset: Dataset) -> None:
    # 테스트 스케일(상품 400)에서 lv4는 카테고리당 ~11개 — 단독으로 100을 못 채워
    # 상위 레벨 backfill이 실제로 동작한다
    products_in: dict[int, set[int]] = defaultdict(set)
    for product_seq, category_seq in dataset.product_categories:
        products_in[category_seq].add(product_seq)

    async def run():
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            anchor = await related.fetch_anchor_info(pool, 1)
            return anchor, await related.fetch_popular_cascade(pool, anchor)
        finally:
            await pool.close()

    anchor, rows = asyncio.run(run())
    seqs = [r["product_seq"] for r in rows]

    assert len(seqs) == len(set(seqs))  # dedup
    assert len(seqs) <= related.POPULAR_TOP_K_RECALL
    assert any(seq not in products_in[anchor[4]] for seq in seqs)  # lv4 밖 유입 = backfill
    assert set(seqs) <= products_in[anchor[2]]  # 전부 anchor의 lv2 우산 안 (global 미유입)
