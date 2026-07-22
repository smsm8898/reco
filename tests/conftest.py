"""integration 테스트 공용 fixture.

실제 PG가 필요한 테스트(SQL·API)는 로컬에 PG가 없으면 skip되고, CI에서는 postgres
service 컨테이너로 항상 실행된다. 개발 데이터를 건드리지 않도록 별도 `reco_test` DB에
합성 데이터와 mart를 한 번 빌드해 세션 전체가 공유한다.

검증은 생성기에 심어둔 정답지로 한다 — 같은 seed로 재생성하면 정답을 알 수 있다.
"""

import os

# 라우터는 import 시점에 ENABLE_* 플래그로 마운트된다 — 공용 앱(app)은 전 지면을 테스트하므로
# app.main import 전에 세 플래그를 켠다. (플래그 자체의 on/off 동작은 test_feature_flags에서 검증)
os.environ.setdefault("ENABLE_RELATED", "true")
os.environ.setdefault("ENABLE_PERSONALIZED", "true")
os.environ.setdefault("ENABLE_POPULAR", "true")

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.settings import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.build_mart import build  # noqa: E402
from scripts.generate_data import Dataset, generate, load  # noqa: E402
from scripts.train_cf import train  # noqa: E402


def product_is_valid(product_row: tuple) -> bool:
    """서빙 위생 5-rule의 파이썬 재현 — 테스트 기대값 계산용 (hygiene._VALID_SQL과 동일 규칙)."""
    _, _, _, price, stock, expose, expired, deleted, _ = product_row
    return (
        expose == "Y"
        and expired == "N"
        and deleted == "N"
        and price > 0
        and (stock is None or stock > 0)
    )


TEST_DBNAME = "reco_test"
# 상품 수가 카테고리당 수십 개는 되어야 한다 — 너무 작으면 활동적인 유저가 선호
# 카테고리를 전부 소진해(시드 제외) 개인화 검증이 인공적으로 막힌다
DATASET_PARAMS = {"seed": 11, "n_users": 500, "n_products": 400, "session_scale": 2.0}


def db_url(dbname: str = TEST_DBNAME) -> str:
    return settings.pg_conninfo(dbname)


def postgres_available() -> bool:
    try:
        with psycopg.connect(db_url("postgres"), connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


@pytest.fixture(scope="session")
def dataset() -> Dataset:
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
