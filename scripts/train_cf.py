"""ALS 기반 CF 후보 빌드 — 모델 하나에서 mart 테이블 두 개가 나온다.

- mart.user_product_cf      model.recommend(user)       → 유저별 추천 후보 top-N
- mart.product_product_cf   model.similar_items(item)   → 상품별 유사 상품 top-N

학습 데이터: activity.logs + order_all 의 (user, product, confidence) 행렬.
implicit feedback 이므로 평점이 아니라 신뢰도(confidence) 가중치를 쓴다 —
행동이 진할수록(view < cart < order) "정말 선호한다"는 확신이 커진다.

재현성: random_state 고정 + 단일 스레드 학습 — 같은 입력이면 같은 모델.
본인이 이미 소비한 상품은 recommend가 제외한다 (filter_already_liked_items).

실행: uv run python -m scripts.train_cf
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # BLAS 스레딩 고정 — 재현성

import numpy as np
import psycopg
from implicit.als import AlternatingLeastSquares
from scipy.sparse import csr_matrix

from app.core.settings import settings

# =========== CONSTANT ===========
FACTORS = 50
REGULARIZATION = 0.01
ITERATIONS = 15
RANDOM_STATE = 42
TOP_N = 50
LOOKBACK_DAYS = 90
VIEW_CONFIDENCE = 1
CART_CONFIDENCE = 3
ORDER_CONFIDENCE = 5

# =========== SQL ===========
DDL = """
CREATE SCHEMA IF NOT EXISTS mart;
CREATE TABLE IF NOT EXISTS mart.user_product_cf (
    user_seq int NOT NULL,
    product_seq int NOT NULL,
    score double precision NOT NULL,
    PRIMARY KEY (user_seq, product_seq)
);
CREATE TABLE IF NOT EXISTS mart.product_product_cf (
    product_seq int NOT NULL,
    similar_product_seq int NOT NULL,
    score double precision NOT NULL,
    PRIMARY KEY (product_seq, similar_product_seq)
);
"""

_INTERACTIONS_SQL = f"""
WITH params AS (
    SELECT max(event_timestamp) - interval '{LOOKBACK_DAYS} days' AS since
    FROM activity.logs
),
weighted AS (
    SELECT
        user_seq,
        product_seq,
        CASE log_type WHEN 'ADD_CART' THEN {CART_CONFIDENCE} ELSE {VIEW_CONFIDENCE} END
            AS confidence
    FROM activity.logs, params
    WHERE event_timestamp >= params.since
    UNION ALL
    SELECT user_seq, product_seq, {ORDER_CONFIDENCE}
    FROM activity.order_all, params
    WHERE ordered_at >= params.since
)
SELECT user_seq, product_seq, sum(confidence)::double precision AS confidence
FROM weighted
GROUP BY 1, 2
"""


# =========== Train ===========


def _fit_model(rows: list[tuple]) -> tuple[AlternatingLeastSquares, csr_matrix]:
    users = np.array([r[0] for r in rows], dtype=np.int32)
    items = np.array([r[1] for r in rows], dtype=np.int32)
    confidence = np.array([r[2] for r in rows], dtype=np.float32)

    # user_seq/product_seq 가 조밀한 정수라 그대로 행렬 인덱스로 쓴다
    user_items = csr_matrix(
        (confidence, (users, items)),
        shape=(int(users.max()) + 1, int(items.max()) + 1),
    )
    model = AlternatingLeastSquares(
        factors=FACTORS,
        regularization=REGULARIZATION,
        iterations=ITERATIONS,
        random_state=RANDOM_STATE,
        num_threads=1,
    )
    model.fit(user_items, show_progress=False)
    return model, user_items


def train(conn: psycopg.Connection) -> None:
    rows = conn.execute(_INTERACTIONS_SQL).fetchall()
    model, user_items = _fit_model(rows)

    # 유저별 추천 — 본인이 이미 소비한 상품은 제외
    active_users = np.unique([r[0] for r in rows])
    rec_ids, rec_scores = model.recommend(
        active_users, user_items[active_users], N=TOP_N, filter_already_liked_items=True
    )
    user_cf_rows = [
        (int(user_seq), int(product_seq), float(score))
        for user_seq, row_ids, row_scores in zip(active_users, rec_ids, rec_scores, strict=True)
        for product_seq, score in zip(row_ids, row_scores, strict=True)
        if score > 0
    ]

    # 상품별 유사 상품 — 자기 자신은 제외 (similar_items 1위는 항상 자신)
    active_items = np.unique([r[1] for r in rows])
    sim_ids, sim_scores = model.similar_items(active_items, N=TOP_N + 1)
    item_cf_rows = [
        (int(product_seq), int(similar_seq), float(score))
        for product_seq, row_ids, row_scores in zip(active_items, sim_ids, sim_scores, strict=True)
        for similar_seq, score in zip(row_ids, row_scores, strict=True)
        if similar_seq != product_seq and score > 0
    ]

    with conn.transaction():
        conn.execute(DDL)
        conn.execute("DELETE FROM mart.user_product_cf")
        conn.execute("DELETE FROM mart.product_product_cf")
        with conn.cursor() as cur:
            with cur.copy(
                "COPY mart.user_product_cf (user_seq, product_seq, score) FROM STDIN"
            ) as copy:
                for row in user_cf_rows:
                    copy.write_row(row)
            with cur.copy(
                "COPY mart.product_product_cf (product_seq, similar_product_seq, score) FROM STDIN"
            ) as copy:
                for row in item_cf_rows:
                    copy.write_row(row)


def report(conn: psycopg.Connection) -> None:
    for table in ("user_product_cf", "product_product_cf"):
        count = conn.execute(f"SELECT count(*) FROM mart.{table}").fetchone()[0]  # noqa: S608
        print(f"mart.{table}: {count:,} rows")


def main() -> None:
    with psycopg.connect(settings.pg_conninfo()) as conn:
        train(conn)
        report(conn)


if __name__ == "__main__":
    main()
