"""mart.popular_universe 빌드 검증 — conftest 세션 fixture가 빌드한 test DB를 조회."""

from datetime import timedelta

import psycopg
import pytest

from scripts.generate_data import Dataset
from tests.conftest import db_url, postgres_available

pytestmark = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 없음 — integration 테스트 skip"
)


def test_popular_universe_is_populated(dataset: Dataset) -> None:
    with psycopg.connect(db_url()) as conn:
        count = conn.execute("SELECT count(*) FROM mart.popular_universe").fetchone()[0]
    assert count > 0


def test_popular_universe_window_is_30_days(dataset: Dataset) -> None:
    with psycopg.connect(db_url()) as conn:
        span = conn.execute(
            "SELECT max(bucket_ts) - min(bucket_ts) FROM mart.popular_universe"
        ).fetchone()[0]
    assert span <= timedelta(days=30)


def test_popular_universe_gmv_matches_orders(dataset: Dataset) -> None:
    """gmv = num_order × 현재 selling_price (주문 시점 가격이 없는 시뮬레이션 한계)."""
    with psycopg.connect(db_url()) as conn:
        bad = conn.execute(
            """
            SELECT count(*)
            FROM mart.popular_universe u
            JOIN service_db.product_info p USING (product_seq)
            WHERE u.gmv <> u.num_order::bigint * p.selling_price
            """
        ).fetchone()[0]
    assert bad == 0
