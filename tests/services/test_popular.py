"""인기 지면 서비스 — 감쇠 합산(순수)과 dial 사다리, 그리고 전역 파티션 조회."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.models.products import Interval
from app.services import popular
from app.services.popular import (
    GLOBAL_CATEGORY_SEQ,
    INTERVAL_CONFIG,
    SIGNALS,
    _compute_popularity,
)
from scripts.generate_data import Dataset
from tests.conftest import db_url, needs_db

NOW = datetime(2026, 4, 1)
MONTH = INTERVAL_CONFIG[Interval.MONTH]  # gravity 1.0·bucket 24h — 산수가 그대로 읽히는 dial


def _bucket(product_seq: int, seller_seq: int, hours_ago: int, **signals: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "product_seq": product_seq,
        "seller_seq": seller_seq,
        "bucket_ts": NOW - timedelta(hours=hours_ago),
    }
    for signal in SIGNALS:
        row[signal] = signals.get(signal, 0)
    return row


# =========== _compute_popularity (bucket → product 감쇠 합산) ===========


def test_sums_buckets_with_decay() -> None:
    """month dial: now bucket은 /2, 48h 전 bucket은 /4 — 상품 단위로 합산된다."""
    rows = [
        _bucket(1, 7, hours_ago=0, num_view=10),
        _bucket(1, 7, hours_ago=48, num_view=10),
        _bucket(2, 8, hours_ago=0, num_view=10),
    ]

    by_seq = {p["product_seq"]: p for p in _compute_popularity(rows, MONTH)}

    assert by_seq[1]["num_view"] == 10 / 2 + 10 / 4
    assert by_seq[2]["num_view"] == 10 / 2
    assert by_seq[1]["seller_seq"] == 7  # 다양성 정책용 셀러가 살아남는다


def test_recent_beats_old_at_equal_volume() -> None:
    """같은 총량이면 최신이 이긴다 — 이 지면이 '지금 뜨는'을 뜻하게 만드는 성질."""
    rows = [
        _bucket(1, 7, hours_ago=48, num_view=20),
        _bucket(2, 8, hours_ago=0, num_view=20),
    ]

    by_seq = {p["product_seq"]: p for p in _compute_popularity(rows, MONTH)}

    assert by_seq[2]["num_view"] > by_seq[1]["num_view"]


def test_reference_time_is_latest_bucket_not_wall_clock() -> None:
    """기준 시각은 풀 내 최신 bucket_ts — now() 금지 규칙이라 고정 데이터면 결과도 고정."""
    rows = [_bucket(1, 7, hours_ago=0, num_view=10)]

    first = _compute_popularity(rows, MONTH)
    second = _compute_popularity(rows, MONTH)

    assert first == second
    assert first[0]["num_view"] == 10 / 2  # age 0 — 실행 시각과 무관


def test_handles_empty_input() -> None:
    assert _compute_popularity([], MONTH) == []


# =========== INTERVAL_CONFIG (dial 사다리) ===========


def test_decay_ladder_is_monotonic() -> None:
    """day가 가장 세게 감쇠하고 창이 짧다 — flat에 가까운 dial은 일간 순위가 안 바뀐다."""
    day, week, month = (INTERVAL_CONFIG[i] for i in (Interval.DAY, Interval.WEEK, Interval.MONTH))

    assert (day.gravity, week.gravity, month.gravity) == (1.8, 1.4, 1.0)
    assert day.window_hours < week.window_hours < month.window_hours
    assert (day.bucket_hours, week.bucket_hours, month.bucket_hours) == (1, 24, 24)


# =========== fetch_popular_by_category (전역 = 한 파티션) ===========


def _fetch(category_seq: int | None = None, fetch_limit: int = 10) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            if category_seq is None:
                return await popular.fetch_popular_by_category(pool, fetch_limit=fetch_limit)
            return await popular.fetch_popular_by_category(
                pool, fetch_limit=fetch_limit, category_seq=category_seq
            )
        finally:
            await pool.close()

    return asyncio.run(run())


@needs_db
def test_default_category_is_the_global_partition(dataset: Dataset) -> None:
    """category_seq 를 안 주면 전역 인기 — 별도 테이블이 아니라 같은 테이블의 한 파티션이다."""
    default_rows = _fetch()
    explicit_rows = _fetch(category_seq=GLOBAL_CATEGORY_SEQ)

    assert default_rows
    assert default_rows == explicit_rows


@needs_db
def test_rows_come_back_in_rank_order(dataset: Dataset) -> None:
    rows = _fetch(fetch_limit=20)

    assert [r["rank"] for r in rows] == sorted(r["rank"] for r in rows)


@needs_db
def test_fetch_limit_caps_rows(dataset: Dataset) -> None:
    assert len(_fetch(fetch_limit=5)) == 5


@needs_db
def test_category_partition_holds_only_that_category(dataset: Dataset) -> None:
    """카테고리 파티션은 그 카테고리 상품만 — 전역과 섞이지 않는다."""
    category_seq = dataset.categories[0][0]
    in_category = {p for p, c in dataset.product_categories if c == category_seq}

    rows = _fetch(category_seq=category_seq, fetch_limit=50)

    assert rows
    assert {r["product_seq"] for r in rows} <= in_category
