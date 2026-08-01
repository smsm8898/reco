"""integration 테스트 공용 fixture + 정답지 헬퍼.

실제 PG가 필요한 테스트(SQL·API)는 로컬에 PG가 없으면 skip되고, CI에서는 postgres service
컨테이너로 항상 실행된다. 개발 데이터를 건드리지 않도록 별도 `reco_test` DB에 합성 데이터와
mart를 한 번 빌드해 세션 전체가 공유한다.

검증은 생성기에 심어둔 **정답지**로 한다 — 같은 seed로 재생성하면 무엇이 정답인지 알 수 있으므로,
서빙 결과를 "그럴듯한가"가 아니라 "정답과 같은가"로 볼 수 있다. `product_is_valid`·
`bad_seller_seqs`가 그 정답지이고, 서빙 SQL과 규칙이 어긋나면 그 등가 테스트가 먼저 깨진다.
"""

import os

# 라우터는 import 시점에 ENABLE_* 플래그로 마운트된다 — 공용 앱(app)은 전 지면을 테스트하므로
# app.main import 전에 세 플래그를 켠다. (플래그 자체의 on/off 동작은 test_feature_flags에서 검증)
os.environ.setdefault("ENABLE_RELATED", "true")
os.environ.setdefault("ENABLE_PERSONALIZED", "true")
os.environ.setdefault("ENABLE_POPULAR", "true")

from datetime import datetime  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.settings import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.build_mart import build  # noqa: E402
from scripts.generate_data import Dataset, generate, load  # noqa: E402
from scripts.train_cf import train  # noqa: E402

TEST_DBNAME = "reco_test"
# 상품 수가 카테고리당 수십 개는 되어야 한다 — 너무 작으면 활동적인 유저가 선호 카테고리를
# 전부 소진해(시드 제외) 개인화 검증이 인공적으로 막힌다
DATASET_PARAMS = {"seed": 11, "n_users": 500, "n_products": 400, "session_scale": 2.0}


def db_url(dbname: str = TEST_DBNAME) -> str:
    return settings.pg_conninfo(dbname)


def postgres_available() -> bool:
    try:
        with psycopg.connect(db_url("postgres"), connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


needs_db = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 없음 — integration 테스트 skip"
)


# =========== 정답지 (서빙 SQL과 같은 규칙의 파이썬 재현) ===========


def product_is_valid(product_row: tuple) -> bool:
    """서빙 위생 9-rule의 파이썬 재현 — `hygiene._VALID_SQL`과 규칙이 1:1이어야 한다.

    판매기간은 SQL이 `now()`로 보므로 여기도 실시간 시계를 쓴다. 생성기가 만료/예약 시각을
    고정값으로 심어 두어 실행 시점이 달라도 판정은 흔들리지 않는다.
    """
    (
        _,
        _,
        product_name,
        price,
        stock,
        expose,
        punished,
        excluded,
        deleted,
        start_at,
        end_at,
        _,
    ) = product_row
    now = datetime.now()
    return (
        expose == "Y"
        and punished == "N"
        and deleted == "N"
        and (excluded or "N") != "Y"
        and price > 0
        and len(product_name) >= 2
        and (start_at is None or start_at <= now)
        and (end_at is None or end_at >= now)
        and (stock is None or stock > 0)
    )


def bad_seller_seqs(dataset: Dataset) -> set[int]:
    """차단 셀러(result=0) — `bad_seller.get_bad_sellers`와 같은 규칙의 정답지."""
    return {seller for seller, result in dataset.producer_service_quality if result == 0}


def servable_seqs(dataset: Dataset) -> set[int]:
    """서빙 가능한 상품 = 위생 통과 ∩ 차단 셀러 아님 — `hygiene.filter_valid`의 정답지.

    세 지면의 응답은 예외 없이 이 집합의 부분집합이어야 한다.
    """
    blocked = bad_seller_seqs(dataset)
    seller_of = {row[0]: row[1] for row in dataset.products}
    return {
        row[0]
        for row in dataset.products
        if product_is_valid(row) and seller_of[row[0]] not in blocked
    }


def seller_of(dataset: Dataset) -> dict[int, int]:
    """product_seq → seller_seq (원장 기준)."""
    return {row[0]: row[1] for row in dataset.products}


# =========== fixture ===========


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    """합성 데이터 + mart + CF를 test DB에 한 번 빌드하고 정답지(Dataset)를 넘긴다."""
    with psycopg.connect(db_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DBNAME} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DBNAME}")

    generated = generate(**DATASET_PARAMS)
    with psycopg.connect(db_url()) as conn:
        load(conn, generated)
        build(conn)
        train(conn)

    yield generated

    with psycopg.connect(db_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DBNAME} WITH (FORCE)")


@pytest.fixture
def client(dataset: Dataset, monkeypatch: pytest.MonkeyPatch):
    """test DB를 바라보는 TestClient — 앱 lifespan이 settings.PG_DB로 pool을 만든다."""
    monkeypatch.setattr(settings, "PG_DB", TEST_DBNAME)
    with TestClient(app) as test_client:
        yield test_client
