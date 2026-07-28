# Popular 튜닝 루프 — grip-reco 1:1 미러링 설계

날짜: 2026-07-28
상태: 승인 대기

## 배경과 목표

grip-reco의 popular 지면은 "batch는 interval-free 원본 시계열만 저장하고,
gravity/bucket/window dial은 서빙과 실험 쪽에 둔다"는 구조 위에서
`scripts/experiments/`의 오프라인 gate/bakeoff가 **서빙 함수를 직접 import**해
dial을 고르고, 통과한 값이 `INTERVAL_CONFIG`로 서빙에 들어가는
"오프라인 실험 → 서빙 반영" 루프를 갖는다.

이 repo에 그 루프를 동일한 형태로 이식한다. **목표는 결과의 유의미함이 아니라
코드 패턴 자체다** — 이런 식으로 쿼리를 짜고, 이런 규율로 하네스를 심는다는 것.
합성 데이터의 인기도는 기간 내내 정적이므로 gravity sweep 결과는 밋밋하게
나오는 것이 정상이며, 이 한계는 실험 결과 문서에 명시한다.

## 비목표

- 데이터 생성기(`scripts/generate_data.py`) 수정 없음 — 시간 역학(트렌딩/신상품) 미도입.
- 배포 전후 KPI 측정(`src/kpi/` 대응물) 없음 — 합성 이벤트는 추천 노출의 영향을
  받지 않으므로 전후 차이가 원리적으로 없음.
- Redis 캐시 레이어 없음 — 별도 스펙 사이클로 진행.
- 온라인 A/B 프레임워크 없음 — grip-reco에도 없음(flag flip + 전후 비교가 규율).
- related / personalized surface 변경 없음.

## grip-reco 대응표

| grip-reco | 이 repo |
|---|---|
| `feature_store.shopping_popular_universe` (30일 hourly bucket) | `mart.popular_universe` 신규 |
| `src/api/ranking.py::compute_popularity` | `app/ranking.py::compute_popularity` (기존 `hacker_news_rank` 대체) |
| `src/api/services/shopping/popular.py` `INTERVAL_CONFIG`·`_compute_popularity`·`_derive_source_rankings`·`_spread_by_seller` | `app/services/popular.py` 재작성 |
| `src/api/models/product.py::Interval` | `app/models/products.py::Interval` |
| `scripts/experiments/shopping/popular_hn_gate.py` | `scripts/experiments/popular_hn_gate.py` |
| `scripts/experiments/shopping/popular_methodology_bakeoff.py` | `scripts/experiments/popular_methodology_bakeoff.py` |
| `scripts/experiments/results/*.md` | `scripts/experiments/results/*.md` |

조립 위치는 grip(서비스의 `build_popular_response`)이 아니라 이 repo의
기존 house style(라우터가 오케스트레이션)을 따른다.

## 1. Batch — `mart.popular_universe`

`scripts/build_mart.py`에 추가:

```sql
CREATE TABLE IF NOT EXISTS mart.popular_universe (
    category_seq int NOT NULL,
    product_seq  int NOT NULL,
    seller_seq   int NOT NULL,
    bucket_ts    timestamp NOT NULL,   -- hourly bucket (date_trunc('hour', ...))
    num_view     int NOT NULL,
    num_cart     int NOT NULL,
    num_order    int NOT NULL,
    gmv          bigint NOT NULL,
    PRIMARY KEY (category_seq, product_seq, bucket_ts)
);
```

- 집계 창: 데이터 최신 시각(`max(event_timestamp)`) 기준 **최근 30일**. `now()` 미사용.
- 소스: `activity.logs`(`VIEW_PRODUCT`→num_view, `ADD_CART`→num_cart) +
  `activity.order_all`(num_order) + `service_db.product_info.selling_price`
  (gmv = num_order × selling_price — 주문 시점 가격이 없는 시뮬레이션 한계를 주석으로 명시).
- 카테고리 조인: `service_db.product_category`. seller는 `product_info.seller_seq`.
- interval 개념 없음 — 감쇠·창은 전부 서빙/실험 몫 (grip의 핵심 설계 판단).
- 기존 `REBUILDS` 방식(단일 트랜잭션 DELETE+INSERT) 그대로 편입.

**삭제(완전 대체 — grip의 decommission 방식):**
`mart.category_weekly_popularity` 테이블 DDL·`CATEGORY_WEEKLY_POPULARITY_SQL`·
`WEEKLY_DAYS`·`WEEKLY_RECALL_LIMIT` 상수, `app/services/popular.py::fetch_weekly_by_category`.
`mart.category_popularity`(related용)와 `mart.product_popularity`(콜드스타트용)는 유지.

## 2. 서빙

### `app/models/products.py`

```python
class Interval(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
```

### `app/ranking.py`

`hacker_news_rank`(row 리스트 단위, `HN_GRAVITY`·`HN_AGE_UNIT_SECONDS` 포함) 삭제,
grip 시그니처의 스칼라 순수 함수로 교체:

```python
GRAVITY = 1.8

def compute_popularity(signal: float, age: float, *, bucket: float, gravity: float = GRAVITY) -> float:
    """HN 시간감쇠: signal / (ceil(age / bucket) + 2) ** gravity. age는 시간(h), 음수는 0으로 clamp."""
```

`apply_rrf`, `apply_same_seller_cap`은 그대로.

### `app/services/popular.py`

```python
SIGNALS = ["num_view", "num_cart", "num_order", "gmv"]
RRF_K = 60
SELLER_MIN_GAP = 5

# IntervalConfig = NamedTuple(gravity: float, bucket_hours: int, window_hours: int)
INTERVAL_CONFIG: dict[Interval, IntervalConfig] = {
    Interval.DAY:   IntervalConfig(gravity=1.8, bucket_hours=1,  window_hours=72),
    Interval.WEEK:  IntervalConfig(gravity=1.0, bucket_hours=24, window_hours=240),
    Interval.MONTH: IntervalConfig(gravity=0.5, bucket_hours=24, window_hours=720),
}

async def fetch_popular(pool, category_seq: int, interval: Interval) -> list[dict[str, Any]]
    # popular_universe에서 window_hours로 자른 bucket row 조회.
    # 기준 시각(데이터의 '현재')은 카테고리 무관 전역 max(bucket_ts) 서브쿼리 — 결정적.

def _compute_popularity(rows: list[dict[str, Any]], interval: Interval) -> list[dict[str, Any]]
    # 순수 함수. bucket별로 ranking.compute_popularity를 신호 4종 각각에 적용해
    # product 단위로 합산. now = max(bucket_ts). 반환 row: product_seq, seller_seq, 신호 4종 감쇠합.

def _derive_source_rankings(products, signals: list[str]) -> list[tuple[str, list[dict]]]
    # 신호별 내림차순 ranked list. tie-break는 product_seq 오름차순.

def _spread_by_seller(products, min_gap: int) -> list[dict[str, Any]]
    # greedy 다양성 rerank: 직전 min_gap-1 슬롯에 없는 셀러 중 최고 점수를 선택,
    # 불가능하면 점수 순서에 양보. 집합은 불변, 순서만 변경.
```

기존 `fetch`(전역 인기 — personalized 콜드스타트), `fetch_by_category`(related·backfill)는 유지.

### `app/routers/product_popular.py`

```python
async def popular_products(request, pool, category_seq: int,
                           interval: Interval = Interval.WEEK, limit: LimitParam = 20)
```

파이프라인: `fetch_popular` → `_compute_popularity` → `hygiene.filter_valid`(한 쿼리)
→ `_derive_source_rankings` + `apply_rrf(k=60)` → `[:limit]` → `_spread_by_seller(min_gap=5)`
→ `to_result` + `record_count`.

- spread는 truncation **후** 실행 — 먼저 자르지 않으면 spread가 선택 자체를 바꾼다(grip 주석의 이유까지 이관).
- 기본 interval=week: 기존 주간 지면과 호환. 잘못된 interval은 enum 검증으로 422.
- oversample 없음: universe 전체를 hygiene → 랭킹 (기존 popular 정책 유지).

## 3. 실험 하네스 — `scripts/experiments/`

공통 규율: **서빙 함수(`app.ranking.compute_popularity` 등)를 import해서 쓴다.**
실험 코드가 곧 서빙 코드 검증이 되게 하는 것이 grip 규율의 핵심. dial 후보를
스크립트에 재구현하지 않는다. stdlib argparse만 사용(신규 의존성 없음),
Docker 이미지 제외(`scripts/` 기존 정책).

### `popular_hn_gate.py`

- point-in-time 분할: `T = max(event_timestamp) − 7일`, 평가창 = `(T, T+7일]`.
- 스코어링 입력은 mart를 읽지 않고 **activity 원본에서 cutoff T로 직접 hourly bucket을
  집계**한다 (build_mart와 같은 쿼리 모양 — 미래 누수 방지의 point-in-time 규율).
- gravity sweep: `{0, 1.0, 1.5, 1.8, 2.0}` — 0은 무감쇠(기존 방식 재현 = incumbent).
- 채점: 카테고리별로
  - `capture@K` = |dial의 top-K ∩ 평가창 실제 top-K| / K (실제 순위 = 평가창 num_view 내림차순)
  - `recall@K` = 평가창에서 view된 **고유 상품 집합** 중 dial top-K에 포함된 비율
  - K = 20, 카테고리 매크로 평균으로 요약.
- 출력: 콘솔 테이블 + `scripts/experiments/results/YYYY-MM-DD-popular-hn-gate.md`
  (합성 데이터의 정적 인기도 한계를 md 서두에 명시).

### `popular_methodology_bakeoff.py`

- 같은 분할·지표로 알고리즘 계열 비교: `count`(단순 카운트) / `hn_decay` /
  `exp_decay`(반감기 지수감쇠) / `funnel`(view+cart×3+order×10 가중) / `funnel_hn`(가중+감쇠).
- hn 계열은 서빙 함수 import, 나머지 계열은 스크립트 내 정의(서빙에 없는 비교군이므로).
- 출력: 콘솔 테이블 + `results/YYYY-MM-DD-popular-methodology-bakeoff.md`.

## 4. 에러 처리

- 잘못된 interval → FastAPI enum 검증 422 (grip 동일).
- 빈 카테고리·빈 window → 빈 `result` 200 + `record_count` 관측 (기존 계약 유지).
- `compute_popularity`의 음수 age는 0으로 clamp.
- 서빙 쿼리는 window 조건으로 잘리므로 기존 `statement_timeout` 2s 그대로.

## 5. 테스트

- `tests/test_ranking.py`: `hacker_news_rank` 테스트 삭제 →
  `compute_popularity` 테스트(bucket 올림 경계, gravity=0이면 감쇠 미미, 음수 age clamp, 단조성).
- `tests/services/test_popular.py` 신규: `_compute_popularity`(bucket 합산·결정성),
  `_spread_by_seller`(gap 준수·불가능 시 점수 순 양보·집합 불변), `_derive_source_rankings`(tie-break).
- `tests/routers/test_product_popular.py` 갱신: interval 3종 동작, 잘못된 값 422,
  기본값 week, envelope·limit clamp·hygiene 필터 기존 계약 유지.
- conftest 세션 fixture가 `build_mart`를 실행하므로 `popular_universe`는 자동 반영.
- 실험 스크립트는 테스트 없음(grip 동일) — 실행법을 README에 기록.

## 6. 문서

- `docs/notes/serving.md`: popular 섹션 재작성 — interval dial 표, 신호별 decay→RRF 구조,
  spread 도입 이유, "batch interval-free / 서빙 dial" 설계 판단.
- README: 엔드포인트 표에 `interval` param 추가, 실험 스크립트 실행법 추가.

## 변경 파일 목록

| 구분 | 파일 |
|---|---|
| 신규 | `scripts/experiments/popular_hn_gate.py`, `scripts/experiments/popular_methodology_bakeoff.py`, `scripts/experiments/results/`(실행 산출물 md), `tests/services/test_popular.py` |
| 수정 | `scripts/build_mart.py`, `app/ranking.py`, `app/services/popular.py`, `app/routers/product_popular.py`, `app/models/products.py`, `tests/test_ranking.py`, `tests/routers/test_product_popular.py`, `docs/notes/serving.md`, `README.md` |
| 삭제 | (파일 없음 — `category_weekly_popularity` 관련 테이블·SQL·상수·함수 단위 삭제) |

## 성공 기준

1. `uv run pytest` 전체 green.
2. `uv run python scripts/experiments/popular_hn_gate.py` 가 gravity별 capture@K/recall@K
   테이블을 출력하고 `results/*.md`를 생성한다.
3. 실험 스크립트가 서빙 `compute_popularity`를 import한다 — 감쇠 로직의 중복 구현이 없다.
4. `GET /api/v1/products/popular?category_seq=&interval=` 계약: 기본 week, 잘못된 값 422,
   응답 envelope 불변.

## 후속 (이 스펙 범위 밖)

- Redis 캐시 레이어(fail-open, negative caching, TTL, 키 버저닝) — 다음 스펙 사이클.
- 생성기 시간 역학 도입 시 gate가 유의미한 결과를 내는지 재검 — 선택적 확장.
