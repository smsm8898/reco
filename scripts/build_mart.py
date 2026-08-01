"""mart 스키마 빌드 — 원천(activity, service_db)을 읽어 서빙 재료를 만든다.

batch는 recall(후보와 신호 수집), ranking은 서빙 몫이다:
- mart.product_related      anchor별 co-occurrence 후보 top-100 — view/cart/order 신호 카운트만
                            저장하고 랭킹하지 않는다 (서빙이 RRF로 융합)
- mart.category_popularity  카테고리별 인기 순위 — 관련·개인화의 popular 신호 재료.
                            전역 인기(개인화 콜드스타트)는 별도 테이블이 아니라 이 테이블의
                            category_seq = GLOBAL_CATEGORY_SEQ 파티션이다 — 점수식이 같은데
                            테이블을 나누면 같은 계산이 두 벌이 되어 드리프트한다.
- mart.popular_universe     카테고리별 30일 hourly bucket 원본 — /popular 지면(감쇠·창은 서빙 dial)
- mart.user_recent_views    유저별 최근 본 상품 top-M — 프로필의 시드
- mart.user_info            유저 프로필: 인구통계(원장) + 행동 기반 선호 카테고리·상품

CF 테이블(user_product_cf, product_product_cf)은 ALS 모델 학습이 필요해
scripts/train_cf.py 가 별도로 소유한다.

위생 필터는 여기서 하지 않는다 — 상품 상태(판매중지 등)는 실시간으로 변하므로
batch 시점의 필터는 서빙 시점에는 이미 낡은 정보다. 서빙이 거른다.

핵심 기법:
- gap 세션화: 로그에는 세션이 없다. LAG로 직전 이벤트와의 간격을 재서
  SESSION_GAP_MINUTES 넘게 비면 새 세션으로 정의한다.
- 신호별 co-occurrence 단위: view는 세션, cart/order는 유저 (희소한 신호일수록 넓은 창)
- 무중단 멱등 재빌드: DELETE+INSERT를 한 트랜잭션으로 — PG MVCC 덕에
  서빙 읽기는 커밋 순간까지 기존 데이터를 보다가 원자적으로 전환된다.
- now() 금지: lookback 기준은 max(event_timestamp) — 데이터가 고정이면 결과도 고정.

실행: uv run python -m scripts.build_mart
"""

import psycopg

from app.core.settings import settings
from app.services.popular import GLOBAL_CATEGORY_SEQ

SESSION_GAP_MINUTES = 30
LOOKBACK_DAYS = 90
RELATED_RECALL_LIMIT = 100  # anchor별 후보 상한 (recall — 랭킹은 서빙 몫)
# 파티션당 상한 — 전역 파티션에도 같이 걸린다. 개인화 콜드스타트가 이 파티션을 통째로 읽으므로
# 서빙 최대 요청량(MAX_LIMIT × OVERSAMPLE)보다 작아지면 안 된다 — test_popular.py 가 고정한다.
CATEGORY_POPULAR_LIMIT = 100
RECENT_TOP_M = 20
ORDER_WEIGHT = 10  # 인기 점수에서 주문 1건 = view 몇 건 가치로 칠 것인가
UNIVERSE_DAYS = 30  # /popular universe 창 — interval-free 원본. 감쇠·창 선택은 서빙 dial 몫

DDL = """
CREATE SCHEMA IF NOT EXISTS mart;
CREATE TABLE IF NOT EXISTS mart.product_related (
    product_seq int NOT NULL,
    related_product_seq int NOT NULL,
    num_view int NOT NULL,
    num_cart int NOT NULL,
    num_order int NOT NULL,
    PRIMARY KEY (product_seq, related_product_seq)
);
CREATE TABLE IF NOT EXISTS mart.category_popularity (
    category_seq int NOT NULL,
    product_seq int NOT NULL,
    score double precision NOT NULL,
    rank int NOT NULL,
    PRIMARY KEY (category_seq, product_seq)
);
-- /popular 전용 — 카테고리별 30일 hourly bucket 원본 (interval-free). gravity/bucket/window
-- 같은 dial은 전부 서빙·실험 몫이라 여기서는 시계열 원본만 적재한다. dial 변경에 재적재가
-- 필요 없고, 실험 스크립트가 서빙 함수로 sweep할 수 있는 구조 (grip-reco 미러링).
CREATE TABLE IF NOT EXISTS mart.popular_universe (
    category_seq int NOT NULL,
    product_seq int NOT NULL,
    seller_seq int NOT NULL,
    bucket_ts timestamp NOT NULL,
    num_view int NOT NULL,
    num_cart int NOT NULL,
    num_order int NOT NULL,
    gmv bigint NOT NULL,
    PRIMARY KEY (category_seq, product_seq, bucket_ts)
);
-- 전역 인기는 category_popularity 의 한 파티션으로 흡수됐다. 구 테이블이 남아 있으면
-- 낡은 데이터를 계속 들고 있게 되므로 재빌드 때 걷어낸다.
DROP TABLE IF EXISTS mart.product_popularity;
CREATE TABLE IF NOT EXISTS mart.user_recent_views (
    user_seq int NOT NULL,
    product_seq int NOT NULL,
    view_count int NOT NULL,
    last_viewed_at timestamp NOT NULL,
    PRIMARY KEY (user_seq, product_seq)
);
CREATE TABLE IF NOT EXISTS mart.user_info (
    user_seq int PRIMARY KEY,
    gender text NOT NULL,
    birth_year int NOT NULL,
    preferred_category_seq int NOT NULL,
    preferred_product_seq int NOT NULL
);
"""

PRODUCT_RELATED_SQL = f"""
INSERT INTO mart.product_related (product_seq, related_product_seq, num_view, num_cart, num_order)
WITH params AS (
    SELECT max(event_timestamp) - interval '{LOOKBACK_DAYS} days' AS since
    FROM activity.logs
),
views AS (
    SELECT
        user_seq,
        product_seq,
        event_timestamp,
        CASE
            WHEN lag(event_timestamp) OVER w IS NULL
              OR event_timestamp - lag(event_timestamp) OVER w
                 > interval '{SESSION_GAP_MINUTES} minutes'
            THEN 1 ELSE 0
        END AS is_new_session
    FROM activity.logs, params
    WHERE log_type = 'VIEW_PRODUCT'
      AND event_timestamp >= params.since
    WINDOW w AS (PARTITION BY user_seq ORDER BY event_timestamp)
),
sessioned AS (
    SELECT
        user_seq,
        product_seq,
        sum(is_new_session) OVER (PARTITION BY user_seq ORDER BY event_timestamp) AS session_seq
    FROM views
),
view_products AS (
    SELECT DISTINCT user_seq, session_seq, product_seq FROM sessioned
),
view_pairs AS (
    SELECT a.product_seq AS a, b.product_seq AS b, count(*) AS cnt
    FROM view_products a
    JOIN view_products b USING (user_seq, session_seq)
    WHERE a.product_seq <> b.product_seq
    GROUP BY 1, 2
),
cart_products AS (
    SELECT DISTINCT user_seq, product_seq
    FROM activity.logs, params
    WHERE log_type = 'ADD_CART' AND event_timestamp >= params.since
),
cart_pairs AS (
    SELECT a.product_seq AS a, b.product_seq AS b, count(*) AS cnt
    FROM cart_products a
    JOIN cart_products b USING (user_seq)
    WHERE a.product_seq <> b.product_seq
    GROUP BY 1, 2
),
order_products AS (
    SELECT DISTINCT user_seq, product_seq
    FROM activity.order_all, params
    WHERE ordered_at >= params.since
),
order_pairs AS (
    SELECT a.product_seq AS a, b.product_seq AS b, count(*) AS cnt
    FROM order_products a
    JOIN order_products b USING (user_seq)
    WHERE a.product_seq <> b.product_seq
    GROUP BY 1, 2
),
merged AS (
    SELECT
        a AS product_seq,
        b AS related_product_seq,
        coalesce(sum(cnt) FILTER (WHERE src = 'view'), 0)  AS num_view,
        coalesce(sum(cnt) FILTER (WHERE src = 'cart'), 0)  AS num_cart,
        coalesce(sum(cnt) FILTER (WHERE src = 'order'), 0) AS num_order
    FROM (
        SELECT a, b, 'view' AS src, cnt FROM view_pairs
        UNION ALL
        SELECT a, b, 'cart' AS src, cnt FROM cart_pairs
        UNION ALL
        SELECT a, b, 'order' AS src, cnt FROM order_pairs
    ) u
    GROUP BY 1, 2
),
recall AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY product_seq
            ORDER BY num_view + num_cart + num_order DESC, related_product_seq
        ) AS rn
    FROM merged
)
SELECT product_seq, related_product_seq, num_view, num_cart, num_order
FROM recall
WHERE rn <= {RELATED_RECALL_LIMIT}
"""

CATEGORY_POPULARITY_SQL = f"""
INSERT INTO mart.category_popularity (category_seq, product_seq, score, rank)
WITH params AS (
    SELECT max(event_timestamp) - interval '{LOOKBACK_DAYS} days' AS since
    FROM activity.logs
),
views AS (
    SELECT product_seq, count(*) AS view_count
    FROM activity.logs, params
    WHERE log_type = 'VIEW_PRODUCT' AND event_timestamp >= params.since
    GROUP BY 1
),
orders AS (
    SELECT product_seq, count(*) AS order_count
    FROM activity.order_all, params
    WHERE ordered_at >= params.since
    GROUP BY 1
),
-- 상품→카테고리 매핑에 전역 파티션을 한 벌 더 얹는다. 전역 인기가 카테고리 인기와 같은
-- 점수식·같은 순위 규칙을 쓴다는 사실이 UNION 한 줄로 드러나고, 별도 SQL·테이블이 필요 없다.
scoped AS (
    SELECT product_seq, category_seq FROM service_db.product_category
    UNION ALL
    SELECT product_seq, {GLOBAL_CATEGORY_SEQ} FROM service_db.product_info
),
ranked AS (
    SELECT
        s.category_seq,
        p.product_seq,
        coalesce(v.view_count, 0) + {ORDER_WEIGHT} * coalesce(o.order_count, 0) AS score,
        row_number() OVER (
            PARTITION BY s.category_seq
            ORDER BY coalesce(v.view_count, 0) + {ORDER_WEIGHT} * coalesce(o.order_count, 0) DESC,
                     p.product_seq
        ) AS rank
    FROM service_db.product_info p
    JOIN scoped s USING (product_seq)
    LEFT JOIN views v USING (product_seq)
    LEFT JOIN orders o USING (product_seq)
)
SELECT category_seq, product_seq, score, rank
FROM ranked
WHERE rank <= {CATEGORY_POPULAR_LIMIT}
"""

# gmv = num_order × 현재 selling_price — 주문 시점 가격이 원천에 없는 시뮬레이션 한계.
POPULAR_UNIVERSE_SQL = f"""
INSERT INTO mart.popular_universe
    (category_seq, product_seq, seller_seq, bucket_ts, num_view, num_cart, num_order, gmv)
WITH events AS (
    SELECT product_seq, date_trunc('hour', event_timestamp) AS bucket_ts,
           (log_type = 'VIEW_PRODUCT')::int AS is_view,
           (log_type = 'ADD_CART')::int AS is_cart,
           0 AS is_order
    FROM activity.logs
    UNION ALL
    SELECT product_seq, date_trunc('hour', ordered_at), 0, 0, 1
    FROM activity.order_all
),
params AS (
    SELECT max(bucket_ts) - interval '{UNIVERSE_DAYS} days' AS since FROM events
),
bucketed AS (
    SELECT product_seq, bucket_ts,
           sum(is_view) AS num_view, sum(is_cart) AS num_cart, sum(is_order) AS num_order
    FROM events, params
    WHERE bucket_ts >= params.since
    GROUP BY 1, 2
)
SELECT pc.category_seq, b.product_seq, p.seller_seq, b.bucket_ts,
       b.num_view, b.num_cart, b.num_order,
       b.num_order::bigint * p.selling_price AS gmv
FROM bucketed b
JOIN service_db.product_info p USING (product_seq)
JOIN service_db.product_category pc USING (product_seq)
"""

USER_RECENT_VIEWS_SQL = f"""
INSERT INTO mart.user_recent_views (user_seq, product_seq, view_count, last_viewed_at)
WITH params AS (
    SELECT max(event_timestamp) - interval '{LOOKBACK_DAYS} days' AS since
    FROM activity.logs
),
recent AS (
    SELECT
        user_seq,
        product_seq,
        count(*) AS view_count,
        max(event_timestamp) AS last_viewed_at,
        row_number() OVER (
            PARTITION BY user_seq
            ORDER BY max(event_timestamp) DESC, product_seq
        ) AS rn
    FROM activity.logs, params
    WHERE log_type = 'VIEW_PRODUCT' AND event_timestamp >= params.since
    GROUP BY user_seq, product_seq
)
SELECT user_seq, product_seq, view_count, last_viewed_at
FROM recent
WHERE rn <= {RECENT_TOP_M}
"""

# 유저 프로필 — 인구통계(원장) + 행동 기반 선호. 행동(user_recent_views)이 있는 유저만.
USER_INFO_SQL = """
INSERT INTO mart.user_info
    (user_seq, gender, birth_year, preferred_category_seq, preferred_product_seq)
WITH viewed AS (
    SELECT
        urv.user_seq,
        urv.product_seq,
        pc.category_seq,
        urv.view_count,
        row_number() OVER (
            PARTITION BY urv.user_seq
            ORDER BY urv.view_count DESC, urv.product_seq
        ) AS product_rn
    FROM mart.user_recent_views urv
    JOIN service_db.product_category pc USING (product_seq)
    -- 선호 카테고리는 lv3 기준 — product_category가 레벨별 3행이라 필터 없이는
    -- 집계 범위가 넓은 lv2가 항상 이겨 선호가 lv2로 뭉개진다
    JOIN service_db.category c USING (category_seq)
    WHERE c.level = 3
),
top_category AS (
    SELECT
        user_seq,
        category_seq,
        row_number() OVER (
            PARTITION BY user_seq
            ORDER BY sum(view_count) DESC, category_seq
        ) AS category_rn
    FROM viewed
    GROUP BY user_seq, category_seq
)
SELECT m.user_seq, m.gender, m.birth_year, tc.category_seq, tp.product_seq
FROM service_db.member m
JOIN (SELECT user_seq, product_seq FROM viewed WHERE product_rn = 1) tp USING (user_seq)
JOIN (SELECT user_seq, category_seq FROM top_category WHERE category_rn = 1) tc USING (user_seq)
"""

# 순서 주의: user_info 는 user_recent_views 를 읽는다
REBUILDS = [
    ("product_related", PRODUCT_RELATED_SQL),
    ("category_popularity", CATEGORY_POPULARITY_SQL),
    ("popular_universe", POPULAR_UNIVERSE_SQL),
    ("user_recent_views", USER_RECENT_VIEWS_SQL),
    ("user_info", USER_INFO_SQL),
]


def build(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(DDL)
        for table, insert_sql in REBUILDS:
            conn.execute(f"DELETE FROM mart.{table}")  # noqa: S608 (고정 테이블명)
            conn.execute(insert_sql)


def report(conn: psycopg.Connection) -> None:
    for table, _ in REBUILDS:
        count = conn.execute(f"SELECT count(*) FROM mart.{table}").fetchone()[0]  # noqa: S608
        print(f"mart.{table}: {count:,} rows")
    covered = conn.execute("SELECT count(DISTINCT product_seq) FROM mart.product_related")
    print(f"  관련 후보 보유 anchor 수: {covered.fetchone()[0]:,}")


def main() -> None:
    with psycopg.connect(settings.pg_conninfo()) as conn:
        build(conn)
        report(conn)


if __name__ == "__main__":
    main()
