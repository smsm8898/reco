"""합성 커머스 데이터를 생성해 PG에 적재한다.

실무형 웨어하우스 모델링을 따른다:
- `activity` 스키마: 사용자 활동
  - `events_all` — GA 스타일 클라이언트 이벤트: 피드 노출(view_item_list)과 클릭(select_item)
  - `logs` — 상품 행동 로그: 상세 조회(VIEW_PRODUCT)와 담기(ADD_CART). 추천 feature가 소비하는 원천
  - `order_all` — 주문. 돈이 오간 기록의 원천은 로그가 아니라 서비스 DB다
- `service_db` 스키마: 서비스 원장 — 회원/상품/카테고리 차원
- 행동 로그에는 세션이 없다 — 세션화는 소비하는 쪽(feature SQL)이 시간 gap 기준으로 수행
- 정답지(유저 선호, 상품 인기 가중치)는 DB에 저장하지 않는다 — 검증은 같은 seed로 재생성해서 수행

세션 인과 구조: 피드 노출(imp) → 클릭(clk) → 클릭한 상품부터 상세 탐색(logs) → 담기 → 주문.

심어둔 구조 (추천 품질 검증의 정답지):
- 상품 인기: 파워로 분포 (소수 상품에 트래픽 집중)
- 유저 선호: 유저마다 선호 카테고리 2~3개, 세션의 70%가 선호 카테고리에서 시작
- 세션 응집: 세션 내 다음 이벤트는 80% 확률로 같은 카테고리
- 퍼널: VIEW_PRODUCT → 12% ADD_CART → 35% 주문
- 위생 필터 재료: 상품의 ~10%에 위생 위반을 심는다 (비노출·만료·삭제·0원·품절) —
  서빙 위생 필터(5-rule)가 걸러야 한다

실행: uv run python -m scripts.generate_data
"""

import hashlib
import math
import random
from bisect import bisect
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from itertools import accumulate

import psycopg

from app.core.settings import settings

SEED = 42
N_USERS = 10_000
N_PRODUCTS = 2_000
N_SELLERS = 300
SESSION_SCALE = 2.5  # 유저당 세션 수 스케일 (전체 이벤트 규모 조절)
FEED_SIZE = 10  # 세션 시작 시 피드에 노출되는 상품 수

CATEGORY_NAMES = [
    "beauty",
    "fashion",
    "food",
    "electronics",
    "home",
    "sports",
    "books",
    "toys",
    "pets",
    "baby",
    "health",
    "outdoor",
]

# now()를 쓰면 실행할 때마다 데이터가 달라지므로 기준 시각을 고정한다
BASE_TIME = datetime(2026, 1, 1)
PERIOD_DAYS = 90

POPULARITY_EXPONENT = 0.9
PREFERRED_CATEGORY_P = 0.7
STAY_CATEGORY_P = 0.8
CART_P = 0.12
ORDER_P = 0.35

# 위생 필터 재료 — 서빙이 걸러야 할 상태를 일부 상품에 독립 확률로 심는다 (합쳐서 ~10%)
HIDDEN_P = 0.03  # expose = 'N'
EXPIRED_P = 0.03  # expired = 'Y' (판매 기간 종료 — 만료 판정은 배치가 플래그로 내려둔다)
DELETED_P = 0.02  # deleted = 'Y'
ZERO_PRICE_P = 0.01  # selling_price = 0
SOLD_OUT_P = 0.03  # stock_count = 0 (그 외 절반은 재고 수치, 절반은 NULL=무제한)


@dataclass
class Dataset:
    members: list[tuple[int, str, int, datetime]]  # user_seq, gender, birth_year, joined_at
    # seq, seller, name, price, stock, expose, expired, deleted, created_at
    products: list[tuple]
    categories: list[tuple[int, str]]  # category_seq, category_name
    product_categories: list[tuple[int, int]]  # product_seq, category_seq
    ga_events: list[tuple[int, str, int, datetime]]  # user_seq, event_name, product_seq, ts
    logs: list[tuple[int, int, str, datetime]]  # user_seq, product_seq, log_type, ts
    orders: list[tuple[int, int, int, datetime]]  # order_seq, user_seq, product_seq, ordered_at
    # 정답지 — DB에는 적재하지 않는다
    preferred: dict[int, list[int]]  # user_seq -> 선호 category_seq 목록
    popularity: dict[int, float]  # product_seq -> 심은 인기 가중치


def generate(
    seed: int = SEED,
    n_users: int = N_USERS,
    n_products: int = N_PRODUCTS,
    session_scale: float = SESSION_SCALE,
) -> Dataset:
    rng = random.Random(seed)

    categories = [(seq, name) for seq, name in enumerate(CATEGORY_NAMES, start=1)]
    category_seqs = [seq for seq, _ in categories]

    ranks = list(range(1, n_products + 1))
    rng.shuffle(ranks)
    products = []
    product_categories = []
    popularity: dict[int, float] = {}
    for product_seq, rank in enumerate(ranks, start=1):
        category_seq = rng.choice(category_seqs)
        product_name = f"{CATEGORY_NAMES[category_seq - 1]} item {product_seq}"
        selling_price = (
            0
            if rng.random() < ZERO_PRICE_P
            else int(math.exp(rng.uniform(math.log(5_000), math.log(200_000)))) // 100 * 100
        )
        stock_roll = rng.random()
        if stock_roll < SOLD_OUT_P:
            stock_count = 0
        elif stock_roll < 0.5:
            stock_count = rng.randint(1, 500)
        else:
            stock_count = None  # NULL = 무제한
        expose = "N" if rng.random() < HIDDEN_P else "Y"
        expired = "Y" if rng.random() < EXPIRED_P else "N"
        deleted = "Y" if rng.random() < DELETED_P else "N"
        created_at = BASE_TIME - timedelta(seconds=rng.uniform(0, 365 * 86400))
        products.append(
            (
                product_seq,
                rng.randint(1, N_SELLERS),
                product_name,
                selling_price,
                stock_count,
                expose,
                expired,
                deleted,
                created_at,
            )
        )
        product_categories.append((product_seq, category_seq))
        popularity[product_seq] = rank**-POPULARITY_EXPONENT

    members = [
        (
            user_seq,
            rng.choice(["F", "M"]),
            rng.randint(1966, 2006),
            BASE_TIME - timedelta(seconds=rng.uniform(0, 730 * 86400)),
        )
        for user_seq in range(1, n_users + 1)
    ]
    preferred = {
        user_seq: rng.sample(category_seqs, k=rng.choice([2, 3])) for user_seq, *_ in members
    }

    # 카테고리별 (상품 seq, 누적 가중치) — 인기 가중 샘플링을 O(log n)으로
    by_category: dict[int, tuple[list[int], list[float]]] = {}
    for category_seq in category_seqs:
        seqs = [p for p, c in product_categories if c == category_seq]
        weights = [popularity[p] for p in seqs]
        by_category[category_seq] = (seqs, list(accumulate(weights)))

    def pick_category(user_preferred: list[int]) -> int:
        if rng.random() < PREFERRED_CATEGORY_P:
            return rng.choice(user_preferred)
        return rng.choice(category_seqs)

    def pick_product(category_seq: int) -> int:
        seqs, cum = by_category[category_seq]
        return seqs[bisect(cum, rng.random() * cum[-1])]

    def pick_feed(category_seq: int) -> list[int]:
        """피드 노출 배치 — 중복 없는 인기 가중 샘플. 첫 상품이 클릭 대상."""
        candidates = by_category[category_seq][0]
        size = min(FEED_SIZE, len(candidates))
        batch: list[int] = []
        attempts = 0
        while len(batch) < size and attempts < 200:
            attempts += 1
            candidate = pick_product(category_seq)
            if candidate not in batch:
                batch.append(candidate)
        for leftover in candidates:
            if len(batch) >= size:
                break
            if leftover not in batch:
                batch.append(leftover)
        return batch

    ga_events = []
    logs = []
    orders = []
    for user_seq, *_ in members:
        n_sessions = min(60, max(1, round(session_scale * rng.paretovariate(1.3))))
        for _ in range(n_sessions):
            t = BASE_TIME + timedelta(seconds=rng.uniform(0, PERIOD_DAYS * 86400))
            category_seq = pick_category(preferred[user_seq])

            # 피드 노출 → 클릭 → 상세 진입
            feed = pick_feed(category_seq)
            clicked = feed[0]
            for shown in feed:
                ga_events.append((user_seq, "view_item_list", shown, t))
            t += timedelta(seconds=rng.uniform(1, 10))
            ga_events.append((user_seq, "select_item", clicked, t))
            t += timedelta(seconds=rng.uniform(1, 5))

            length = min(30, 1 + int(rng.expovariate(1 / 7)))
            product_seq = clicked
            for i in range(length):
                if i > 0:
                    if rng.random() > STAY_CATEGORY_P:
                        category_seq = pick_category(preferred[user_seq])
                    product_seq = pick_product(category_seq)
                logs.append((user_seq, product_seq, "VIEW_PRODUCT", t))
                t += timedelta(seconds=rng.uniform(5, 120))
                if rng.random() < CART_P:
                    logs.append((user_seq, product_seq, "ADD_CART", t))
                    t += timedelta(seconds=rng.uniform(5, 120))
                    if rng.random() < ORDER_P:
                        orders.append((0, user_seq, product_seq, t))
                        t += timedelta(seconds=rng.uniform(5, 120))

    ga_events.sort(key=lambda e: e[3])
    logs.sort(key=lambda e: e[3])
    orders.sort(key=lambda o: o[3])
    orders = [(i, u, p, ts) for i, (_, u, p, ts) in enumerate(orders, start=1)]

    return Dataset(
        members=members,
        products=products,
        categories=categories,
        product_categories=product_categories,
        ga_events=ga_events,
        logs=logs,
        orders=orders,
        preferred=preferred,
        popularity=popularity,
    )


def digest(dataset: Dataset) -> str:
    h = hashlib.md5()
    for field in fields(Dataset):
        value = getattr(dataset, field.name)
        rows = sorted(value.items()) if isinstance(value, dict) else value
        for row in rows:
            h.update(repr(row).encode())
    return h.hexdigest()


DDL = """
CREATE SCHEMA IF NOT EXISTS activity;
CREATE SCHEMA IF NOT EXISTS service_db;
DROP TABLE IF EXISTS
    activity.events_all, activity.logs, activity.order_all,
    service_db.member, service_db.product_info,
    service_db.category, service_db.product_category;
CREATE TABLE service_db.member (
    user_seq int PRIMARY KEY,
    gender text NOT NULL,
    birth_year int NOT NULL,
    joined_at timestamp NOT NULL
);
CREATE TABLE service_db.product_info (
    product_seq int PRIMARY KEY,
    seller_seq int NOT NULL,
    product_name text NOT NULL,
    selling_price int NOT NULL,
    stock_count int,
    expose text NOT NULL,
    expired text NOT NULL,
    deleted text NOT NULL,
    created_at timestamp NOT NULL
);
CREATE TABLE service_db.category (
    category_seq int PRIMARY KEY,
    category_name text NOT NULL
);
CREATE TABLE service_db.product_category (
    product_seq int NOT NULL,
    category_seq int NOT NULL,
    PRIMARY KEY (product_seq, category_seq)
);
CREATE TABLE activity.events_all (
    user_seq int NOT NULL,
    event_name text NOT NULL,
    product_seq int NOT NULL,
    event_timestamp timestamp NOT NULL
);
CREATE TABLE activity.logs (
    user_seq int NOT NULL,
    product_seq int NOT NULL,
    log_type text NOT NULL,
    event_timestamp timestamp NOT NULL
);
CREATE TABLE activity.order_all (
    order_seq bigint PRIMARY KEY,
    user_seq int NOT NULL,
    product_seq int NOT NULL,
    ordered_at timestamp NOT NULL
);
"""

COPY_TARGETS = [
    ("members", "service_db.member (user_seq, gender, birth_year, joined_at)"),
    (
        "products",
        "service_db.product_info (product_seq, seller_seq, product_name, selling_price, "
        "stock_count, expose, expired, deleted, created_at)",
    ),
    ("categories", "service_db.category (category_seq, category_name)"),
    ("product_categories", "service_db.product_category (product_seq, category_seq)"),
    ("ga_events", "activity.events_all (user_seq, event_name, product_seq, event_timestamp)"),
    ("logs", "activity.logs (user_seq, product_seq, log_type, event_timestamp)"),
    ("orders", "activity.order_all (order_seq, user_seq, product_seq, ordered_at)"),
]


def load(conn: psycopg.Connection, dataset: Dataset) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
        for field_name, target in COPY_TARGETS:
            with cur.copy(f"COPY {target} FROM STDIN") as copy:
                for row in getattr(dataset, field_name):
                    copy.write_row(row)
    conn.commit()


def report(conn: psycopg.Connection) -> None:
    tables = [target.split(" ")[0] for _, target in COPY_TARGETS]
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 (고정 테이블명)
            print(f"{table}: {cur.fetchone()[0]:,} rows")
        cur.execute(
            "SELECT event_name, count(*) FROM activity.events_all GROUP BY 1 ORDER BY 2 DESC"
        )
        for event_name, count in cur.fetchall():
            print(f"  events_all/{event_name}: {count:,}")
        cur.execute("SELECT log_type, count(*) FROM activity.logs GROUP BY 1 ORDER BY 2 DESC")
        for log_type, count in cur.fetchall():
            print(f"  logs/{log_type}: {count:,}")


def main() -> None:
    dataset = generate()
    print(f"digest: {digest(dataset)}")
    with psycopg.connect(settings.pg_conninfo()) as conn:
        load(conn, dataset)
        report(conn)


if __name__ == "__main__":
    main()
