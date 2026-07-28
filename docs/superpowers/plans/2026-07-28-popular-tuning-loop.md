# Popular 튜닝 루프 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** grip-reco의 popular 지면 구조(interval-free hourly mart + 서빙 dial + 오프라인 gate/bakeoff 하네스)를 이 repo에 1:1 이식한다.

**Architecture:** batch(`build_mart.py`)는 30일 hourly bucket 원본(`mart.popular_universe`)만 적재하고, 감쇠(gravity/bucket/window) dial은 전부 서빙(`INTERVAL_CONFIG`)과 실험 스크립트에 둔다. 실험 스크립트는 서빙 함수를 import해서 sweep한다 — 감쇠 로직의 중복 구현이 없도록. 스펙: `docs/superpowers/specs/2026-07-28-popular-tuning-loop-design.md`.

**Tech Stack:** Python 3.13, FastAPI, psycopg(raw SQL, no ORM), pytest, uv.

## Global Constraints

- `__init__.py` 생성 금지 — namespace package + `pythonpath=["."]` 방식 유지.
- 커밋 메시지에 Claude 관련 문구·`Co-Authored-By` trailer 금지.
- `now()` 금지 — 시각 기준은 항상 데이터의 `max(timestamp)` (결정성).
- 신규 의존성 금지 — 실험 스크립트는 stdlib(`argparse`)만 사용.
- Docker 이미지는 `app/`만 복사 — `scripts/` 추가분은 이미지에 안 들어감(변경 불필요).
- ruff line-length 100. 각 태스크 끝에 `uv run ruff check .` 로 확인.
- integration 테스트는 로컬 PG 필요: `docker compose up -d` 먼저. PG 없으면 skip되므로 "전부 skip"을 "전부 통과"로 오독하지 말 것.
- 고정 dial 값(스펙 그대로): day=(gravity 1.8, bucket 1h, window 72h), week=(1.0, 24h, 240h), month=(0.5, 24h, 720h). `RRF_K=60`, `SELLER_MIN_GAP=5`. 실험: `GRAVITY_CANDIDATES=[0.0, 1.0, 1.5, 1.8, 2.0]`, `EVAL_DAYS=7`, `K=20`.
- 응답 envelope `{code, message, result}` 불변. `limit` clamp(1~50) 불변.

## File Structure

| 파일 | 책임 |
|---|---|
| `scripts/build_mart.py` (수정) | `mart.popular_universe` 적재 추가, `category_weekly_popularity` 제거 |
| `app/ranking.py` (수정) | `compute_popularity` 스칼라 순수 함수 (기존 `hacker_news_rank` 대체) |
| `app/models/products.py` (수정) | `Interval` enum 추가 |
| `app/services/popular.py` (수정) | `INTERVAL_CONFIG`, `fetch_popular`, `_compute_popularity`, `_derive_source_rankings`, `_spread_by_seller`, `build_popular_result` |
| `app/routers/product_popular.py` (수정) | `interval` query param + 새 파이프라인 |
| `scripts/experiments/popular_hn_gate.py` (신규) | gravity sweep gate |
| `scripts/experiments/popular_methodology_bakeoff.py` (신규) | 알고리즘 계열 비교 |
| `tests/test_build_mart.py` (신규) | universe 적재 검증 |
| `tests/services/test_popular.py` (신규) | 서빙 순수 함수 단위 테스트 |
| `tests/test_ranking.py`, `tests/routers/test_product_popular.py` (수정) | 교체·갱신 |
| `docs/notes/serving.md`, `README.md` (수정) | popular 섹션 재작성, 실험 실행법 |

---

### Task 1: `mart.popular_universe` 적재

**Files:**
- Modify: `scripts/build_mart.py`
- Test: `tests/test_build_mart.py` (신규)

**Interfaces:**
- Consumes: `activity.logs`, `activity.order_all`, `service_db.product_info`, `service_db.product_category` (기존 원천 테이블)
- Produces: `mart.popular_universe(category_seq int, product_seq int, seller_seq int, bucket_ts timestamp, num_view int, num_cart int, num_order int, gmv bigint)` — Task 3의 `fetch_popular`와 Task 6·7 실험 스크립트가 읽는다. 기존 `category_weekly_popularity`는 이 태스크에서 **삭제하지 않는다** (Task 5에서 제거 — 그때까지 기존 서빙이 살아 있어야 한다).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_build_mart.py` 신규 파일:

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_build_mart.py -v`
Expected: FAIL — `relation "mart.popular_universe" does not exist` (세션 fixture의 build는 성공하지만 테이블이 없어 조회가 죽는다)

- [ ] **Step 3: build_mart.py 구현**

`scripts/build_mart.py` 수정 — 상수 추가 (`WEEKLY_RECALL_LIMIT` 줄 아래):

```python
UNIVERSE_DAYS = 30  # /popular universe 창 — interval-free 원본. 감쇠·창 선택은 서빙 dial 몫
```

`DDL`의 `category_weekly_popularity` 블록 **아래에** 테이블 추가 (기존 블록은 아직 삭제하지 않는다):

```sql
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
```

`PRODUCT_POPULARITY_SQL` 정의 위에 INSERT SQL 추가:

```python
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
```

`REBUILDS` 리스트의 `category_weekly_popularity` 항목 **다음 줄에** 추가:

```python
    ("popular_universe", POPULAR_UNIVERSE_SQL),
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_build_mart.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 전체 테스트 + lint**

Run: `uv run pytest && uv run ruff check .`
Expected: 전부 PASS (기존 서빙은 그대로 — weekly 테이블도 아직 살아 있다)

- [ ] **Step 6: Commit**

```bash
git add scripts/build_mart.py tests/test_build_mart.py
git commit -m "feat(mart): popular_universe — 30일 hourly bucket 원본 (interval-free)"
```

---

### Task 2: `app/ranking.py::compute_popularity`

**Files:**
- Modify: `app/ranking.py`
- Test: `tests/test_ranking.py`

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `compute_popularity(signal: float, age: float, *, bucket: float, gravity: float = GRAVITY) -> float`, 모듈 상수 `GRAVITY = 1.8`. age 단위는 시간(h). Task 3 서빙과 Task 6·7 실험이 이 함수를 import한다. **기존 `hacker_news_rank`는 이 태스크에서 삭제하지 않는다** (라우터가 아직 쓴다 — Task 5에서 제거).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_ranking.py`에 추가 (기존 `hacker_news_rank` 테스트는 그대로 둔다):

```python
from app.ranking import compute_popularity


def test_compute_popularity_decays_with_age() -> None:
    fresh = compute_popularity(100, 0, bucket=24, gravity=1.0)
    old = compute_popularity(100, 24 * 6, bucket=24, gravity=1.0)
    assert fresh > old


def test_compute_popularity_same_bucket_same_decay() -> None:
    # bucket 올림(ceil) — 같은 bucket 안(1h~24h)은 같은 감쇠, bucket 경계에서 계단
    assert compute_popularity(100, 1, bucket=24, gravity=1.8) == compute_popularity(
        100, 23, bucket=24, gravity=1.8
    )
    assert compute_popularity(100, 0, bucket=24, gravity=1.8) > compute_popularity(
        100, 1, bucket=24, gravity=1.8
    )


def test_compute_popularity_gravity_zero_means_no_decay() -> None:
    assert compute_popularity(100, 240, bucket=24, gravity=0.0) == 100


def test_compute_popularity_clamps_negative_age() -> None:
    assert compute_popularity(100, -5, bucket=24, gravity=1.8) == compute_popularity(
        100, 0, bucket=24, gravity=1.8
    )
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_ranking.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_popularity'`

- [ ] **Step 3: 구현**

`app/ranking.py`에 추가 (`hacker_news_rank`는 그대로 둔다). 파일 상단에 `import math` 추가:

```python
GRAVITY = 1.8  # Hacker News 기본값 — interval별 오버라이드는 서빙 INTERVAL_CONFIG 몫


def compute_popularity(
    signal: float, age: float, *, bucket: float, gravity: float = GRAVITY
) -> float:
    """HN 시간감쇠: signal / (ceil(age / bucket) + 2) ** gravity.

    age·bucket 단위는 시간(h). bucket 올림이라 같은 bucket 안의 이벤트는 같은 감쇠를
    받는다 — 감쇠 해상도(1h/24h)를 dial로 고를 수 있게 한 것 (grip-reco 시그니처).
    음수 age(미래 이벤트)는 0으로 clamp.
    """
    return signal / (math.ceil(max(age, 0.0) / bucket) + 2) ** gravity
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_ranking.py -v && uv run ruff check .`
Expected: PASS (기존 테스트 포함 전부)

- [ ] **Step 5: Commit**

```bash
git add app/ranking.py tests/test_ranking.py
git commit -m "feat(ranking): compute_popularity — bucket 올림 HN 감쇠 스칼라 함수"
```

---

### Task 3: 서빙 파이프라인 — `Interval` + `app/services/popular.py`

**Files:**
- Modify: `app/models/products.py`, `app/services/popular.py`
- Test: `tests/services/test_popular.py` (신규 — `__init__.py` 만들지 말 것)

**Interfaces:**
- Consumes: Task 1의 `mart.popular_universe`, Task 2의 `compute_popularity`, 기존 `app.ranking.apply_rrf(rankings, *, k, id_key)`, `app.services.hygiene.filter_valid(pool, *row_lists)`, `app.models.products.to_result(rows)`
- Produces (Task 4 라우터와 Task 6·7 실험이 사용):
  - `app.models.products.Interval(str, Enum)` — `DAY="day" | WEEK="week" | MONTH="month"`
  - `popular.INTERVAL_CONFIG: dict[Interval, IntervalConfig]`, `IntervalConfig(NamedTuple)` = `(gravity: float, bucket_hours: int, window_hours: int)`
  - `popular.SIGNALS = ["num_view", "num_cart", "num_order", "gmv"]`, `popular.RRF_K = 60`, `popular.SELLER_MIN_GAP = 5`
  - `async popular.fetch_popular(pool, category_seq: int, interval: Interval) -> list[dict]` — bucket row 리스트
  - `async popular.build_popular_result(pool, rows, *, interval: Interval, limit: int) -> list[int]`
  - 기존 `fetch`(콜드스타트)·`fetch_by_category`(related)는 시그니처 불변으로 유지

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_popular.py` 신규 파일:

```python
"""popular 서빙 순수 함수 단위 테스트 — DB 불필요."""

from datetime import datetime, timedelta
from typing import Any

from app.models.products import Interval
from app.services.popular import (
    SIGNALS,
    _compute_popularity,
    _derive_source_rankings,
    _spread_by_seller,
)

NOW = datetime(2026, 4, 1)


def _bucket(product_seq: int, seller_seq: int, hours_ago: int, **signals: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "product_seq": product_seq,
        "seller_seq": seller_seq,
        "bucket_ts": NOW - timedelta(hours=hours_ago),
    }
    for signal in SIGNALS:
        row[signal] = signals.get(signal, 0)
    return row


def test_compute_popularity_sums_buckets_with_decay() -> None:
    # week dial: gravity=1.0, bucket=24h → now bucket은 /2, 48h 전 bucket은 /4
    rows = [
        _bucket(1, 7, hours_ago=0, num_view=10),
        _bucket(1, 7, hours_ago=48, num_view=10),
        _bucket(2, 8, hours_ago=0, num_view=10),
    ]

    products = _compute_popularity(rows, Interval.WEEK)

    by_seq = {p["product_seq"]: p for p in products}
    assert by_seq[1]["num_view"] == 10 / 2 + 10 / 4  # 감쇠 합산
    assert by_seq[2]["num_view"] == 10 / 2
    assert by_seq[1]["seller_seq"] == 7


def test_recent_events_outweigh_old_at_equal_volume() -> None:
    rows = [
        _bucket(1, 7, hours_ago=48, num_view=20),  # 같은 총량, 오래됨
        _bucket(2, 8, hours_ago=0, num_view=20),  # 최신
    ]

    products = _compute_popularity(rows, Interval.WEEK)

    by_seq = {p["product_seq"]: p for p in products}
    assert by_seq[2]["num_view"] > by_seq[1]["num_view"]


def test_compute_popularity_handles_empty() -> None:
    assert _compute_popularity([], Interval.WEEK) == []


def test_derive_source_rankings_splits_and_drops_zero() -> None:
    products = [
        {"product_seq": 1, "num_view": 9.0, "gmv": 0.0},
        {"product_seq": 2, "num_view": 3.0, "gmv": 5.0},
    ]

    rankings = _derive_source_rankings(products, ["num_view", "gmv"])

    assert rankings[0][0] == "num_view"
    assert [r["product_seq"] for r in rankings[0][1]] == [1, 2]
    assert [r["product_seq"] for r in rankings[1][1]] == [2]  # gmv=0인 1은 제외


def test_spread_by_seller_respects_min_gap() -> None:
    products = [
        {"product_seq": 1, "seller_seq": 7},
        {"product_seq": 2, "seller_seq": 7},
        {"product_seq": 3, "seller_seq": 8},
        {"product_seq": 4, "seller_seq": 9},
        {"product_seq": 5, "seller_seq": 10},
    ]

    spread = _spread_by_seller(products, min_gap=3)

    # 2(s7)는 직전 2슬롯에 s7이 없어질 때까지 뒤로 밀린다: 1,3,4,2,5
    assert [r["product_seq"] for r in spread] == [1, 3, 4, 2, 5]
    assert {r["product_seq"] for r in spread} == {1, 2, 3, 4, 5}  # 집합 불변


def test_spread_yields_to_score_order_when_infeasible() -> None:
    products = [{"product_seq": i, "seller_seq": 7} for i in (1, 2, 3)]

    spread = _spread_by_seller(products, min_gap=3)

    assert [r["product_seq"] for r in spread] == [1, 2, 3]  # 전부 같은 셀러 → 점수 순 양보
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_popular.py -v`
Expected: FAIL — `ImportError` (Interval, SIGNALS 등 미정의)

- [ ] **Step 3: `Interval` enum 추가**

`app/models/products.py` — import에 `from enum import Enum` 추가, `RelatedResponse` 위에:

```python
class Interval(str, Enum):
    """popular 지면의 집계 관점 — 값은 query param 계약 (day/week/month)."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
```

- [ ] **Step 4: `app/services/popular.py` 구현**

기존 코드(`fetch`, `fetch_by_category`, `fetch_weekly_by_category`, 해당 SQL)는 그대로 두고 추가한다. 모듈 docstring 아래 import 정리:

```python
from typing import Any, NamedTuple

from psycopg_pool import AsyncConnectionPool

from app.models.products import Interval, to_result
from app.ranking import apply_rrf, compute_popularity
from app.services import hygiene
```

상수·설정 (SQL 섹션 위):

```python
# =========== CONSTANT ===========
SIGNALS = ["num_view", "num_cart", "num_order", "gmv"]
RRF_K = 60
SELLER_MIN_GAP = 5  # 같은 셀러가 다시 나오기까지 최소 슬롯 간격 (다양성)


class IntervalConfig(NamedTuple):
    gravity: float  # 감쇠 강도 — 오프라인 gate(scripts/experiments)로 고른 값
    bucket_hours: int  # 감쇠 해상도 — day는 시간 단위, week/month는 일 단위
    window_hours: int  # 집계 창 — universe(30일)에서 서빙 시점에 자른다


# dial은 전부 서빙 몫 — batch(popular_universe)는 interval-free 원본만 적재한다.
INTERVAL_CONFIG: dict[Interval, IntervalConfig] = {
    Interval.DAY: IntervalConfig(gravity=1.8, bucket_hours=1, window_hours=72),
    Interval.WEEK: IntervalConfig(gravity=1.0, bucket_hours=24, window_hours=240),
    Interval.MONTH: IntervalConfig(gravity=0.5, bucket_hours=24, window_hours=720),
}
```

SQL 섹션에 추가:

```python
# 기준 시각은 universe 전역 max(bucket_ts) — now() 금지, 고정 데이터면 결과도 고정.
_UNIVERSE_SQL = """
    SELECT product_seq, seller_seq, bucket_ts, num_view, num_cart, num_order, gmv
    FROM mart.popular_universe
    WHERE category_seq = %s
      AND bucket_ts >= (SELECT max(bucket_ts) FROM mart.popular_universe)
                       - make_interval(hours => %s)
"""
```

Fetch 섹션에 추가:

```python
async def fetch_popular(
    pool: AsyncConnectionPool, category_seq: int, interval: Interval
) -> list[dict[str, Any]]:
    """카테고리의 hourly bucket row — interval의 window로 자른다. 감쇠는 조립 몫."""
    window_hours = INTERVAL_CONFIG[interval].window_hours
    async with pool.connection() as conn:
        cur = await conn.execute(_UNIVERSE_SQL, (category_seq, window_hours))
        rows = await cur.fetchall()
    return [
        {
            "product_seq": r[0],
            "seller_seq": r[1],
            "bucket_ts": r[2],
            "num_view": r[3],
            "num_cart": r[4],
            "num_order": r[5],
            "gmv": r[6],
        }
        for r in rows
    ]
```

Algorithm 섹션 추가 (related.py의 `# =========== Algorithm ===========` 컨벤션):

```python
def _compute_popularity(rows: list[dict[str, Any]], interval: Interval) -> list[dict[str, Any]]:
    """bucket row를 product 단위 감쇠 합산으로 접는다 (순수 함수).

    신호 4종 각각에 compute_popularity를 적용해 합산 — 신호별 ranked list의 재료.
    now는 풀 내 최신 bucket_ts (now() 금지 규칙).
    """
    if not rows:
        return []
    cfg = INTERVAL_CONFIG[interval]
    now = max(row["bucket_ts"] for row in rows)
    products: dict[int, dict[str, Any]] = {}
    for row in rows:
        age_hours = (now - row["bucket_ts"]).total_seconds() / 3600
        acc = products.setdefault(
            row["product_seq"],
            {"product_seq": row["product_seq"], "seller_seq": row["seller_seq"]}
            | dict.fromkeys(SIGNALS, 0.0),
        )
        for signal in SIGNALS:
            acc[signal] += compute_popularity(
                row[signal], age_hours, bucket=cfg.bucket_hours, gravity=cfg.gravity
            )
    return list(products.values())


def _derive_source_rankings(
    products: list[dict[str, Any]], signals: list[str]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """감쇠 합산된 신호들을 각각의 ranked list로 분해한다 (RRF 입력, 컬럼명 = source label)."""
    rankings: list[tuple[str, list[dict[str, Any]]]] = []
    for col in signals:
        ranked = sorted(
            (r for r in products if r[col] > 0),
            key=lambda r, c=col: (-r[c], r["product_seq"]),
        )
        rankings.append((col, ranked))
    return rankings


def _spread_by_seller(products: list[dict[str, Any]], min_gap: int) -> list[dict[str, Any]]:
    """같은 셀러가 min_gap 슬롯 안에 다시 나오지 않게 greedy 재배치 (집합 불변).

    각 슬롯에서 '직전 min_gap-1개 셀러'에 없는 최고 점수 후보를 고르고, 불가능하면
    점수 순서에 양보한다. truncation 후에 실행해야 한다 — 먼저 펼치면 어떤 상품이
    잘리는지(선택) 자체가 바뀐다.
    """
    remaining = list(products)
    spread: list[dict[str, Any]] = []
    while remaining:
        recent = {r["seller_seq"] for r in spread[-(min_gap - 1) :]} if min_gap > 1 else set()
        pick = next((r for r in remaining if r["seller_seq"] not in recent), remaining[0])
        remaining.remove(pick)
        spread.append(pick)
    return spread


async def build_popular_result(
    pool: AsyncConnectionPool,
    rows: list[dict[str, Any]],
    *,
    interval: Interval,
    limit: int,
) -> list[int]:
    """감쇠 합산 → 위생 → 신호별 RRF → top-limit → seller spread → product_seq 리스트."""
    products = _compute_popularity(rows, interval)
    (valid,) = await hygiene.filter_valid(pool, products)
    fused = apply_rrf(_derive_source_rankings(valid, SIGNALS), k=RRF_K, id_key="product_seq")
    return to_result(_spread_by_seller(fused[:limit], SELLER_MIN_GAP))
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/services/test_popular.py tests/test_ranking.py -v && uv run ruff check .`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/models/products.py app/services/popular.py tests/services/test_popular.py
git commit -m "feat(popular): interval dial 서빙 파이프라인 — 신호별 HN 감쇠, RRF, seller spread"
```

---

### Task 4: 라우터 전환 — `interval` query param

**Files:**
- Modify: `app/routers/product_popular.py`, `tests/routers/test_product_popular.py`

**Interfaces:**
- Consumes: Task 3의 `Interval`, `popular.fetch_popular`, `popular.build_popular_result`; 기존 `deps.clamp_limit`, `deps.record_count`, `PopularResponse`
- Produces: `GET /api/v1/products/popular?category_seq=&interval=&limit=` — `interval` 기본값 `week`, 잘못된 값 422. 응답 계약 불변.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/routers/test_product_popular.py` 수정. `_category_with_candidates`를 universe 기준으로 교체:

```python
def _category_with_candidates() -> int:
    with psycopg.connect(db_url()) as conn:
        return conn.execute(
            "SELECT category_seq FROM mart.popular_universe "
            "GROUP BY category_seq HAVING count(DISTINCT product_seq) >= 20 LIMIT 1"
        ).fetchone()[0]
```

파일 끝에 interval 테스트 추가:

```python
def test_all_intervals_are_accepted(client: TestClient) -> None:
    category_seq = _category_with_candidates()
    for interval in ("day", "week", "month"):
        response = client.get(
            "/api/v1/products/popular",
            params={"category_seq": category_seq, "interval": interval},
        )
        assert response.status_code == 200, interval
        assert response.json()["result"]


def test_invalid_interval_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/products/popular",
        params={"category_seq": _category_with_candidates(), "interval": "year"},
    )
    assert response.status_code == 422


def test_default_interval_is_week(client: TestClient) -> None:
    category_seq = _category_with_candidates()

    default = client.get("/api/v1/products/popular", params={"category_seq": category_seq})
    weekly = client.get(
        "/api/v1/products/popular", params={"category_seq": category_seq, "interval": "week"}
    )

    assert default.json()["result"] == weekly.json()["result"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/routers/test_product_popular.py -v`
Expected: `test_all_intervals_are_accepted`에서 day/month가 week와 같은 결과(파라미터 무시)로 통과해버리면 안 되므로 — 현 시점엔 interval 파라미터가 아예 없어서 무시된다. `test_invalid_interval_is_rejected` FAIL (200이 나옴) 확인이 핵심.

- [ ] **Step 3: 라우터 구현**

`app/routers/product_popular.py` 전체 교체:

```python
from fastapi import APIRouter, Request

from app.deps import LimitParam, PoolDep, clamp_limit, record_count
from app.models.products import Interval, PopularResponse
from app.services import popular

router = APIRouter(prefix="/api/v1/products", tags=["products"])


@router.get("/popular")
async def popular_products(
    request: Request,
    pool: PoolDep,
    category_seq: int,
    interval: Interval = Interval.WEEK,
    limit: LimitParam = 20,
) -> PopularResponse:
    """카테고리 인기 — interval dial(day/week/month)로 감쇠·창을 고른 신호별 HN 랭킹.

    universe(30일 hourly bucket)를 interval의 window로 잘라 신호 4종을 각각 감쇠
    합산하고, 위생 필터 후 RRF로 융합한다. 마지막의 seller spread는 truncation 후 —
    순서만 바꾸고 선택은 안 바꾼다. 잘못된 interval은 enum 검증으로 422.
    """
    limit = clamp_limit(limit)
    rows = await popular.fetch_popular(pool, category_seq, interval)
    result = await popular.build_popular_result(pool, rows, interval=interval, limit=limit)
    return PopularResponse(result=record_count(request, result))
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/routers/test_product_popular.py -v`
Expected: PASS (기존 계약 테스트 포함 전부 — 위생·카테고리 소속·clamp)

- [ ] **Step 5: 전체 테스트**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS — 이 시점부터 weekly 경로(`fetch_weekly_by_category`, `hacker_news_rank`)는 라우터에서 미사용

- [ ] **Step 6: Commit**

```bash
git add app/routers/product_popular.py tests/routers/test_product_popular.py
git commit -m "feat(popular): interval query param — universe 기반 파이프라인으로 전환"
```

---

### Task 5: 구경로 decommission + 문서

**Files:**
- Modify: `app/ranking.py`, `app/services/popular.py`, `scripts/build_mart.py`, `tests/test_ranking.py`, `docs/notes/serving.md`, `README.md`

**Interfaces:**
- Consumes: Task 4 완료 상태 (weekly 경로 미사용 확인)
- Produces: `hacker_news_rank`·`HN_GRAVITY`·`HN_AGE_UNIT_SECONDS`, `fetch_weekly_by_category`·`_WEEKLY_BY_CATEGORY_SQL`, `mart.category_weekly_popularity`(DDL·SQL·상수·REBUILDS 항목) **삭제됨**. 이후 태스크는 이들을 참조하면 안 된다.

- [ ] **Step 1: 미사용 확인 (삭제 전 근거)**

Run: `grep -rn "hacker_news_rank\|fetch_weekly_by_category\|category_weekly_popularity\|WEEKLY_DAYS\|WEEKLY_RECALL_LIMIT" app/ tests/ scripts/ docs/notes/ README.md`
Expected: 정의부(app/ranking.py, app/services/popular.py, scripts/build_mart.py)와 자기 테스트(tests/test_ranking.py), 문서(README.md, docs/notes/serving.md)만 나오고 **다른 소비자가 없어야 한다**. 소비자가 나오면 삭제 중단하고 원인 파악.

- [ ] **Step 2: 코드 삭제**

- `app/ranking.py`: `hacker_news_rank` 함수, `HN_GRAVITY`, `HN_AGE_UNIT_SECONDS` 삭제. `datetime` import가 미사용이 되면 함께 삭제. 모듈 docstring의 "시간감쇠(Hacker News)"는 `compute_popularity`가 이어받으므로 유지.
- `app/services/popular.py`: `fetch_weekly_by_category`, `_WEEKLY_BY_CATEGORY_SQL` 삭제.
- `scripts/build_mart.py`: `WEEKLY_DAYS`, `WEEKLY_RECALL_LIMIT` 상수, DDL의 `category_weekly_popularity` 블록(주석 포함), `CATEGORY_WEEKLY_POPULARITY_SQL`, `REBUILDS`의 해당 항목 삭제. 모듈 docstring 7행을 다음으로 교체:

```
- mart.popular_universe     카테고리별 30일 hourly bucket 원본 — /popular 지면(감쇠·창은 서빙 dial)
```

- `tests/test_ranking.py`: `hacker_news_rank` 테스트 3개(`test_hacker_news_favours_recency_at_equal_score`, `test_hacker_news_favours_score_at_equal_recency`, `test_hacker_news_rank_handles_empty`)와 import 삭제. `datetime`/`timedelta` import가 미사용이 되면 삭제.

- [ ] **Step 3: 전체 테스트 (test DB 재빌드 포함 확인)**

Run: `uv run pytest && uv run ruff check .`
Expected: PASS — conftest가 test DB를 새로 만들므로 weekly 테이블 부재가 자동 검증된다

- [ ] **Step 4: 문서 갱신**

- `README.md` 엔드포인트 표의 popular 행을 교체:

```markdown
| `GET /api/v1/products/popular` | 카테고리 인기 — interval dial(day/week/month)별 HN 감쇠 + RRF (`category_seq` 필수) | ✅ |
```

- `docs/notes/serving.md`의 인기 구좌 섹션(`## 3. 인기` 이하)을 새 구조로 재작성. 반드시 담을 것:
  - universe(30일 hourly bucket)는 interval-free — dial(gravity/bucket/window)은 서빙 몫이라는 설계 판단과 이유(재적재 없이 dial 변경, 실험이 서빙 함수로 sweep 가능)
  - `INTERVAL_CONFIG` 표: day(1.8, 1h, 72h) / week(1.0, 24h, 240h) / month(0.5, 24h, 720h)
  - 신호 4종(view/cart/order/gmv) 각각 감쇠 합산 → 신호별 ranked list → RRF(k=60) 융합
  - seller spread(min_gap=5)는 truncation 후 — 순서만 바꾸고 선택은 안 바꾼다
  - dial 선정 근거는 `scripts/experiments/`의 오프라인 gate (다음 태스크에서 생성됨을 전제로 경로만 언급)
- 상단 구좌 표(3행)의 인기 행 한 줄도 갱신: "카테고리에서 지금 뜨는 상품 — interval dial로 day/week/month 관점 선택".

- [ ] **Step 5: Commit**

```bash
git add app/ranking.py app/services/popular.py scripts/build_mart.py tests/test_ranking.py docs/notes/serving.md README.md
git commit -m "refactor(popular): weekly 구경로 decommission — universe 파이프라인으로 완전 대체"
```

---

### Task 6: 실험 하네스 1 — `popular_hn_gate.py`

**Files:**
- Create: `scripts/experiments/popular_hn_gate.py`, `scripts/experiments/results/` (스크립트 실행 산출물)
- Modify: `README.md` (실험 실행법 섹션)

**Interfaces:**
- Consumes: Task 2 `compute_popularity`, Task 3 `INTERVAL_CONFIG`·`Interval`, 원천 `activity.*`·`service_db.*` (mart를 읽지 않는다 — point-in-time 규율)
- Produces: `uv run python -m scripts.experiments.popular_hn_gate [--out PATH]` — gravity별 capture@K/recall@K 콘솔 테이블 + results md. 테스트 없음(grip 동일 — 오프라인 일회성 도구).

- [ ] **Step 1: 스크립트 작성**

`scripts/experiments/popular_hn_gate.py` 신규 파일 (`scripts/experiments/`에 `__init__.py` 만들지 말 것):

```python
"""popular HN gate — gravity sweep을 point-in-time 시간 분할로 채점한다.

grip-reco scripts/experiments/shopping/popular_hn_gate.py 미러링. 규율 세 가지:
- 서빙 함수(app.ranking.compute_popularity)를 import한다 — 감쇠 로직 재구현 금지.
  실험이 곧 서빙 코드 검증이 되게 하는 장치.
- point-in-time: cutoff T '이전' 이벤트만으로 스코어링 (미래 누수 방지). mart를 읽지
  않고 원천에서 직접 bucket을 집계하는 이유 — mart는 전체 기간으로 이미 빌드돼 있다.
- 채점은 (T, T+EVAL_DAYS] 평가창의 실제 view로: capture@K(실제 top-K 적중률),
  recall@K(실제 view된 상품 커버율). gravity=0이 무감쇠 incumbent 재현.

sweep 대상은 단일 신호(num_view)다 — gravity 비교가 목적이고, 신호 융합(RRF) 비교는
methodology bakeoff의 몫. bucket/window는 서빙 week dial 값으로 고정.

한계(합성 데이터): 인기도가 기간 내내 정적이라 gravity 간 차이가 거의 없다.
이 스크립트의 목적은 결과가 아니라 하네스의 형식이다.

실행: uv run python -m scripts.experiments.popular_hn_gate
"""

import argparse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import psycopg

from app.core.settings import settings
from app.models.products import Interval
from app.ranking import compute_popularity
from app.services.popular import INTERVAL_CONFIG

GRAVITY_CANDIDATES = [0.0, 1.0, 1.5, 1.8, 2.0]  # 0 = 무감쇠 (incumbent 재현)
EVAL_DAYS = 7
K = 20
DIAL = INTERVAL_CONFIG[Interval.WEEK]  # gravity 외 dial은 서빙 week 값으로 고정

CUTOFF_SQL = f"SELECT max(event_timestamp) - interval '{EVAL_DAYS} days' FROM activity.logs"

# cutoff 이전 window 창의 카테고리별 hourly view bucket — build_mart.POPULAR_UNIVERSE_SQL과
# 같은 모양이지만 cutoff 조건이 있다 (point-in-time).
TRAIN_BUCKETS_SQL = """
    SELECT pc.category_seq, l.product_seq,
           date_trunc('hour', l.event_timestamp) AS bucket_ts,
           count(*) AS num_view
    FROM activity.logs l
    JOIN service_db.product_category pc USING (product_seq)
    WHERE l.log_type = 'VIEW_PRODUCT'
      AND l.event_timestamp < %(cutoff)s
      AND l.event_timestamp >= %(cutoff)s - make_interval(hours => %(window_hours)s)
    GROUP BY 1, 2, 3
"""

EVAL_VIEWS_SQL = """
    SELECT pc.category_seq, l.product_seq, count(*) AS num_view
    FROM activity.logs l
    JOIN service_db.product_category pc USING (product_seq)
    WHERE l.log_type = 'VIEW_PRODUCT'
      AND l.event_timestamp > %(cutoff)s
      AND l.event_timestamp <= %(cutoff)s + make_interval(days => %(eval_days)s)
    GROUP BY 1, 2
"""


def rank_top_k(
    buckets: list[tuple[int, int, datetime, int]], gravity: float, cutoff: datetime
) -> dict[int, list[int]]:
    """카테고리별 top-K — 서빙 compute_popularity로 bucket 감쇠 합산 (age 기준 = cutoff)."""
    scores: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for category_seq, product_seq, bucket_ts, num_view in buckets:
        age_hours = (cutoff - bucket_ts).total_seconds() / 3600
        scores[category_seq][product_seq] += compute_popularity(
            num_view, age_hours, bucket=DIAL.bucket_hours, gravity=gravity
        )
    return {
        category_seq: [
            p for p, _ in sorted(per_cat.items(), key=lambda kv: (-kv[1], kv[0]))[:K]
        ]
        for category_seq, per_cat in scores.items()
    }


def score_dial(
    top_k: dict[int, list[int]], eval_views: list[tuple[int, int, int]]
) -> tuple[float, float]:
    """카테고리 매크로 평균 (capture@K, recall@K)."""
    actual_views: dict[int, dict[int, int]] = defaultdict(dict)
    for category_seq, product_seq, num_view in eval_views:
        actual_views[category_seq][product_seq] = num_view

    captures, recalls = [], []
    for category_seq, viewed in actual_views.items():
        picked = set(top_k.get(category_seq, []))
        if not picked:
            continue
        actual_top = {
            p for p, _ in sorted(viewed.items(), key=lambda kv: (-kv[1], kv[0]))[:K]
        }
        captures.append(len(picked & actual_top) / K)
        recalls.append(len(picked & set(viewed)) / len(viewed))
    return sum(captures) / len(captures), sum(recalls) / len(recalls)


def render_md(cutoff: datetime, results: list[tuple[float, float, float]]) -> str:
    lines = [
        "# popular HN gate — gravity sweep",
        "",
        f"- cutoff T: {cutoff} (스코어링은 T 이전, 채점은 T 이후 {EVAL_DAYS}일)",
        f"- dial: bucket={DIAL.bucket_hours}h, window={DIAL.window_hours}h (서빙 week 고정)",
        f"- K={K}, 지표는 카테고리 매크로 평균",
        "",
        "> 한계: 합성 데이터의 인기도는 정적이라 gravity 간 차이가 거의 없다.",
        "> 이 문서의 목적은 결과가 아니라 gate 하네스의 형식이다.",
        "",
        "| gravity | capture@20 | recall@20 |",
        "|---|---|---|",
    ]
    lines += [f"| {g} | {c:.4f} | {r:.4f} |" for g, c, r in results]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=f"scripts/experiments/results/{date.today().isoformat()}-popular-hn-gate.md",
    )
    args = parser.parse_args()

    with psycopg.connect(settings.pg_conninfo()) as conn:
        cutoff = conn.execute(CUTOFF_SQL).fetchone()[0]
        params = {"cutoff": cutoff, "window_hours": DIAL.window_hours, "eval_days": EVAL_DAYS}
        buckets = conn.execute(TRAIN_BUCKETS_SQL, params).fetchall()
        eval_views = conn.execute(EVAL_VIEWS_SQL, params).fetchall()

    results = []
    print(f"cutoff={cutoff}  buckets={len(buckets):,}  eval_rows={len(eval_views):,}")
    print(f"{'gravity':>8} {'capture@20':>11} {'recall@20':>10}")
    for gravity in GRAVITY_CANDIDATES:
        capture, recall = score_dial(rank_top_k(buckets, gravity, cutoff), eval_views)
        results.append((gravity, capture, recall))
        print(f"{gravity:>8} {capture:>11.4f} {recall:>10.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(cutoff, results), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 실행 검증**

로컬 dev DB가 준비돼 있어야 한다 (없으면: `docker compose up -d && uv run python -m scripts.generate_data && uv run python -m scripts.build_mart`).

Run: `uv run python -m scripts.experiments.popular_hn_gate`
Expected: gravity 5행 테이블 출력(값은 gravity 간 비슷 — 정적 인기도라 정상), `scripts/experiments/results/*-popular-hn-gate.md` 생성

- [ ] **Step 3: README 실험 섹션 추가**

`README.md` 실행 방법 부근에 추가:

```markdown
## 오프라인 실험 (scripts/experiments/)

dial(gravity 등)은 배포 전 오프라인 gate로 고른다 — 실험 스크립트는 서빙 함수를
import해서 sweep하므로(중복 구현 없음) 실험 결과가 곧 서빙 코드 검증이다.

- `uv run python -m scripts.experiments.popular_hn_gate` — gravity sweep (capture@K/recall@K)

결과는 `scripts/experiments/results/*.md`. 합성 데이터의 인기도는 정적이라 수치 차이는
거의 없다 — 목적은 하네스의 형식(point-in-time 분할, 서빙 함수 재사용)이다.
```

- [ ] **Step 4: lint + 전체 테스트**

Run: `uv run ruff check . && uv run pytest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/experiments/popular_hn_gate.py scripts/experiments/results/ README.md
git commit -m "feat(experiments): popular HN gate — 서빙 함수 import로 gravity sweep"
```

---

### Task 7: 실험 하네스 2 — `popular_methodology_bakeoff.py`

**Files:**
- Create: `scripts/experiments/popular_methodology_bakeoff.py`, results md (실행 산출물)
- Modify: `README.md` (실험 목록에 한 줄)

**Interfaces:**
- Consumes: Task 6과 동일한 SQL 모양·지표 함수 패턴, Task 2 `compute_popularity`
- Produces: `uv run python -m scripts.experiments.popular_methodology_bakeoff [--out PATH]` — 알고리즘 계열별 capture@K/recall@K. 테스트 없음.

- [ ] **Step 1: 스크립트 작성**

`scripts/experiments/popular_methodology_bakeoff.py` 신규 파일:

```python
"""popular methodology bakeoff — 알고리즘 계열 비교 (같은 point-in-time 분할·지표).

grip-reco scripts/experiments/shopping/popular_methodology_bakeoff.py 미러링.
hn 계열은 서빙 함수(compute_popularity)를 import하고, 서빙에 없는 비교군
(count/exp_decay/funnel)만 여기서 정의한다.

계열:
- count      단순 view 카운트 (무감쇠 baseline)
- hn_decay   서빙 HN 감쇠 (week dial: gravity=1.0, bucket=24h)
- exp_decay  반감기 지수감쇠 (half-life 72h)
- funnel     깔때기 가중 카운트 (view + 3*cart + 10*order)
- funnel_hn  깔때기 가중 + HN 감쇠

실행: uv run python -m scripts.experiments.popular_methodology_bakeoff
"""

import argparse
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import psycopg

from app.core.settings import settings
from app.models.products import Interval
from app.ranking import compute_popularity
from app.services.popular import INTERVAL_CONFIG

EVAL_DAYS = 7
K = 20
DIAL = INTERVAL_CONFIG[Interval.WEEK]
HALF_LIFE_HOURS = 72
CART_WEIGHT = 3
ORDER_WEIGHT = 10

CUTOFF_SQL = f"SELECT max(event_timestamp) - interval '{EVAL_DAYS} days' FROM activity.logs"

# hn_gate와 같은 모양이지만 cart/order 신호까지 집계한다 (funnel 계열용).
TRAIN_BUCKETS_SQL = """
    WITH events AS (
        SELECT product_seq, date_trunc('hour', event_timestamp) AS bucket_ts,
               (log_type = 'VIEW_PRODUCT')::int AS is_view,
               (log_type = 'ADD_CART')::int AS is_cart,
               0 AS is_order
        FROM activity.logs
        UNION ALL
        SELECT product_seq, date_trunc('hour', ordered_at), 0, 0, 1
        FROM activity.order_all
    )
    SELECT pc.category_seq, e.product_seq, e.bucket_ts,
           sum(e.is_view) AS num_view, sum(e.is_cart) AS num_cart, sum(e.is_order) AS num_order
    FROM events e
    JOIN service_db.product_category pc USING (product_seq)
    WHERE e.bucket_ts < %(cutoff)s
      AND e.bucket_ts >= %(cutoff)s - make_interval(hours => %(window_hours)s)
    GROUP BY 1, 2, 3
"""

EVAL_VIEWS_SQL = """
    SELECT pc.category_seq, l.product_seq, count(*) AS num_view
    FROM activity.logs l
    JOIN service_db.product_category pc USING (product_seq)
    WHERE l.log_type = 'VIEW_PRODUCT'
      AND l.event_timestamp > %(cutoff)s
      AND l.event_timestamp <= %(cutoff)s + make_interval(days => %(eval_days)s)
    GROUP BY 1, 2
"""

Bucket = tuple[int, int, datetime, int, int, int]  # cat, product, ts, view, cart, order


def _funnel(num_view: int, num_cart: int, num_order: int) -> float:
    return num_view + CART_WEIGHT * num_cart + ORDER_WEIGHT * num_order


METHODOLOGIES: dict[str, Callable[[Bucket, float], float]] = {
    "count": lambda b, age: b[3],
    "hn_decay": lambda b, age: compute_popularity(
        b[3], age, bucket=DIAL.bucket_hours, gravity=DIAL.gravity
    ),
    "exp_decay": lambda b, age: b[3] * 0.5 ** (age / HALF_LIFE_HOURS),
    "funnel": lambda b, age: _funnel(b[3], b[4], b[5]),
    "funnel_hn": lambda b, age: compute_popularity(
        _funnel(b[3], b[4], b[5]), age, bucket=DIAL.bucket_hours, gravity=DIAL.gravity
    ),
}


def rank_top_k(
    buckets: list[Bucket], scorer: Callable[[Bucket, float], float], cutoff: datetime
) -> dict[int, list[int]]:
    scores: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for bucket in buckets:
        age_hours = (cutoff - bucket[2]).total_seconds() / 3600
        scores[bucket[0]][bucket[1]] += scorer(bucket, age_hours)
    return {
        category_seq: [
            p for p, _ in sorted(per_cat.items(), key=lambda kv: (-kv[1], kv[0]))[:K]
        ]
        for category_seq, per_cat in scores.items()
    }


def score_dial(
    top_k: dict[int, list[int]], eval_views: list[tuple[int, int, int]]
) -> tuple[float, float]:
    """카테고리 매크로 평균 (capture@K, recall@K) — hn_gate와 동일 정의."""
    actual_views: dict[int, dict[int, int]] = defaultdict(dict)
    for category_seq, product_seq, num_view in eval_views:
        actual_views[category_seq][product_seq] = num_view

    captures, recalls = [], []
    for category_seq, viewed in actual_views.items():
        picked = set(top_k.get(category_seq, []))
        if not picked:
            continue
        actual_top = {
            p for p, _ in sorted(viewed.items(), key=lambda kv: (-kv[1], kv[0]))[:K]
        }
        captures.append(len(picked & actual_top) / K)
        recalls.append(len(picked & set(viewed)) / len(viewed))
    return sum(captures) / len(captures), sum(recalls) / len(recalls)


def render_md(cutoff: datetime, results: list[tuple[str, float, float]]) -> str:
    lines = [
        "# popular methodology bakeoff",
        "",
        f"- cutoff T: {cutoff}, 평가창 {EVAL_DAYS}일, K={K}, 카테고리 매크로 평균",
        f"- hn 계열 dial: gravity={DIAL.gravity}, bucket={DIAL.bucket_hours}h (서빙 week)",
        "",
        "> 한계: 합성 인기도가 정적이라 감쇠 계열의 우위가 안 드러난다 — 형식이 목적.",
        "",
        "| methodology | capture@20 | recall@20 |",
        "|---|---|---|",
    ]
    lines += [f"| {name} | {c:.4f} | {r:.4f} |" for name, c, r in results]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=(
            f"scripts/experiments/results/{date.today().isoformat()}"
            "-popular-methodology-bakeoff.md"
        ),
    )
    args = parser.parse_args()

    with psycopg.connect(settings.pg_conninfo()) as conn:
        cutoff = conn.execute(CUTOFF_SQL).fetchone()[0]
        params = {"cutoff": cutoff, "window_hours": DIAL.window_hours, "eval_days": EVAL_DAYS}
        buckets = conn.execute(TRAIN_BUCKETS_SQL, params).fetchall()
        eval_views = conn.execute(EVAL_VIEWS_SQL, params).fetchall()

    results = []
    print(f"cutoff={cutoff}  buckets={len(buckets):,}  eval_rows={len(eval_views):,}")
    print(f"{'methodology':>12} {'capture@20':>11} {'recall@20':>10}")
    for name, scorer in METHODOLOGIES.items():
        capture, recall = score_dial(rank_top_k(buckets, scorer, cutoff), eval_views)
        results.append((name, capture, recall))
        print(f"{name:>12} {capture:>11.4f} {recall:>10.4f}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_md(cutoff, results), encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 실행 검증**

Run: `uv run python -m scripts.experiments.popular_methodology_bakeoff`
Expected: 5개 계열 테이블 출력, results md 생성. funnel 계열이 count와 다른 값을 내는지 확인(신호가 실제로 섞였다는 증거).

- [ ] **Step 3: README 실험 목록에 한 줄 추가**

```markdown
- `uv run python -m scripts.experiments.popular_methodology_bakeoff` — 알고리즘 계열 비교 (count/hn/exp/funnel)
```

- [ ] **Step 4: lint + 전체 테스트**

Run: `uv run ruff check . && uv run pytest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/experiments/popular_methodology_bakeoff.py scripts/experiments/results/ README.md
git commit -m "feat(experiments): popular methodology bakeoff — 계열 비교 하네스"
```
