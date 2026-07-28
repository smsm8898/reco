# related cascade backfill + KPI 미러링 설계

날짜: 2026-07-28
상태: 승인 대기

## 배경과 목표

grip-reco의 related 지면은 anchor의 카테고리를 원장에서 **레벨별로 pivot**(`{level: category_seq}`)해
가져오고, popular recall을 **L4→L3→L2 cascade**(dedup 누적, 상한 도달 시 조기 중단)로 채운다.
또 `src/kpi/shopping/related.py` + `related.md`로 배포 전후 KPI를 측정하는 규율이 있다.

이 repo에 두 가지를 이식한다. 목표는 이전 사이클(popular 튜닝 루프)과 동일하게 **코드
패턴·규율의 재현**이다:

1. **cascade backfill** — 카테고리 3층 트리(원장) + anchor pivot + `fetch_popular_cascade`
2. **KPI 미러링** — related 지면의 측정 스크립트 + 측정 SSOT md (형식 재현)

레벨별 category popular는 "airflow가 만든다"는 가정을 따르되, `product_category`를 레벨별로
확장하면 기존 `mart.category_popularity`(build_mart = airflow 스탠드인)가 **코드 무수정으로**
레벨별 인기를 갖게 되므로 신규 mart 테이블은 없다.

## 비목표

- global(전체) popular last-resort 없음 — grip이 2026-07-21 제거한 현재 상태를 미러링.
- KPI `--daily` 옵션 없음 (YAGNI). popular/personalized 지면 KPI 없음 (related만).
- 지면 귀속(referrer) 시뮬레이션 없음 — 생성기에 referrer/section 필드를 추가하지 않는다.
  KPI의 CTR·퍼널은 피드 전체 기준이며 이 한계를 md에 명시한다.
- before/after 차이 없음이 정상 — 합성 이벤트는 추천 노출의 영향을 받지 않는다. md에 명시.
- Redis 캐시 없음 (별도 사이클 유지). related의 RRF 융합·seller cap 로직 변경 없음.

## grip-reco 대응표

| grip-reco | 이 repo |
|---|---|
| `common.category` (level, parent 보유 원장) | `service_db.category`에 `level`·`parent_seq` 추가 |
| `common.product_category` (상품당 lv2/3/4 행) | `service_db.product_category` 상품당 3행으로 확장 |
| `fetch_anchor(seed) -> dict[int, int] \| None` (MAX CASE pivot) | `related.fetch_anchor` pivot으로 교체 |
| `fetch_popular_cascade(anchor_cats)` L4→L3→L2 dedup 누적 | `related.fetch_popular_cascade` 신규 |
| `feature_store.shopping_related_popular` (airflow 산출 rank) | `mart.category_popularity` 재사용 (확장 매핑 덕에 자동 레벨별) |
| `src/kpi/shopping/related.py` + `related.md` (Snowflake/pandas) | `scripts/kpi/related.py` + `related.md` (psycopg/stdlib) |

## 1. 원장·생성기 — 카테고리 3층 트리

`scripts/generate_data.py`의 **카테고리 구조만** 확장. 이벤트 생성 로직·확률·funnel은 무수정.

- 트리: **lv2 4개(seq 1~4) → lv3 12개(seq 5~16, 각 lv2당 3개) → lv4 36개(seq 17~52, 각
  lv3당 3개)**. parent는 결정적 산술로: lv3 `i`의 부모 = `(i-5)//3 + 1`, lv4 `j`의 부모 =
  `(j-17)//3 + 5`. 이름은 기존 명명 스타일에 레벨 표기 포함.
- DDL: `service_db.category`에 `level int NOT NULL`, `parent_seq int`(lv2는 NULL) 추가.
- 상품 배정: 상품은 **lv4 leaf 하나**에 배정(기존 12개 분배 로직을 36개 lv4로 적용).
  `product_category`에는 **조상 포함 3행**(lv4·lv3·lv2)을 기록 — grip의 anchor pivot이 읽는
  구조와 동일.
- 유저 선호·이벤트: 기존 12개 카테고리 개념은 **lv3로 유지** — `preferred`(answer key),
  `pick_category`는 lv3 기준 그대로, `pick_product(lv3)`만 해당 lv3의 lv4 자식들에 속한 상품
  풀로 전개(상품 풀 자체는 기존과 동일).
- `Dataset.categories`는 `(category_seq, category_name, level, parent_seq)` 튜플로,
  `product_categories`는 상품당 3행으로 바뀐다. `COPY_TARGETS`·`digest()` 자동 반영.
- 파급: `mart.category_popularity`·`mart.popular_universe`는 SQL 무수정 — product_category
  확장으로 레벨별 행이 자연 생성된다(집계 키가 category_seq이므로). `/popular`는 어느 레벨
  category_seq로도 조회 가능해진다 — 부작용이 아니라 grip과 같은 성질.
- 예외 1곳: `mart.user_info`의 선호 카테고리 산정은 **lv3 필터가 필요**하다 — 레벨별 3행
  환경에서는 집계 범위가 넓은 lv2가 항상 이겨 선호가 lv2로 뭉개진다. `USER_INFO_SQL`의
  viewed CTE에 `JOIN service_db.category ... WHERE level = 3`을 추가해 기존 의미(12개
  카테고리 선호)를 보존한다.

## 2. 서빙 — anchor pivot + cascade

`app/services/related.py`:

```sql
-- _ANCHOR_SQL 교체: 존재 확인(404) + 레벨별 카테고리 취득을 한 쿼리로 (grip pivot 미러)
SELECT p.product_seq,
       MAX(CASE WHEN c.level = 2 THEN c.category_seq END) AS lv2_category_seq,
       MAX(CASE WHEN c.level = 3 THEN c.category_seq END) AS lv3_category_seq,
       MAX(CASE WHEN c.level = 4 THEN c.category_seq END) AS lv4_category_seq
FROM service_db.product_info p
LEFT JOIN service_db.product_category pc USING (product_seq)
LEFT JOIN service_db.category c USING (category_seq)
WHERE p.product_seq = %s
GROUP BY p.product_seq
```

```python
async def fetch_anchor(pool, product_seq: int) -> dict[int, int] | None
    # 미존재 → None (router가 404). 매핑 없으면 {} — grip 계약 동일.
    # 반환: {level: category_seq}, None 레벨은 생략.

async def fetch_popular_cascade(pool, anchor_cats: dict[int, int]) -> list[dict[str, Any]]
    # L4→L3→L2 순회. 레벨별 조회는 기존 popular.fetch_by_category(pool, seq,
    # POPULAR_TOP_K_RECALL) 재사용. product_seq dedup 누적, POPULAR_TOP_K_RECALL(100)
    # 채우면 조기 중단. 매치 없으면 빈 리스트(popular 미제공, global fallback 없음).
    # hygiene 미적용 — build_related_result 몫 (기존과 동일).
```

라우터(`app/routers/product_related.py`): `fetch_anchor` 먼저(404 겸용) →
`asyncio.gather(fetch_cf, fetch_popular_cascade(anchor_cats))` → `build_related_result`
**무수정**(popular 리스트 출처만 교체 — RRF는 리스트 순서만 쓰므로 레벨 간 rank 값 충돌 무관).

## 3. KPI — `scripts/kpi/related.py` + `scripts/kpi/related.md`

grip `src/kpi/shopping/related.py`의 형식을 로컬 단일 substrate(PG)로 이식. stdlib
argparse + psycopg만 사용(pandas·신규 의존성 금지). Docker 이미지 제외(`scripts/` 정책).

- **지표** (`derive_metrics`):
  - `imp` = `events_all.view_item_list` 수, `clk` = `select_item` 수, `CTR = clk/imp`
  - 퍼널: `logs.VIEW_PRODUCT` → `logs.ADD_CART` → `order_all` 건수
  - `CVR = order/view_product`, `GMV = Σ(주문수 × selling_price)`, `RPM = GMV/imp × 1000`
- **창 분할** (`build_periods(end, days, deploy_date)` — grip 시그니처 미러):
  - `--deploy-date` 없으면 최근 `days`일 단일 baseline 창
  - 있으면 `before = [deploy-days, deploy)`, `after = [deploy, end)`
  - `--end` 기본값 = **데이터 최신 날짜**(`max(event_timestamp)`, now() 금지), `--days` 기본 30
- 출력: 기간(label)별 지표 콘솔 테이블 (문자열 포매팅).
- `scripts/kpi/related.md` (측정 SSOT — grip의 .md sibling 관습):
  - 지표 정의와 SQL 근거, 단일 substrate(로컬 PG)임을 명시
  - **한계 명시 2개**: (1) 지면 귀속 불가 — referrer가 없어 CTR·퍼널이 피드 전체 기준,
    (2) 합성 이벤트는 추천 노출과 무관 — before/after 무차이가 정상이며 형식 재현이 목적

## 4. 에러 처리

- anchor 미존재 → 404 (기존 계약 유지). 카테고리 매핑 없는 상품 → cascade 빈 리스트,
  CF만으로 정상 응답. 전부 빈 경우 → 빈 result 200 (기존 계약).
- KPI: `--deploy-date`가 데이터 범위 밖이면 빈 창 지표 0으로 출력 (crash 금지,
  0-나눗셈은 grip처럼 `_pct`/`_div` 가드 함수로 처리).

## 5. 테스트

- **기존 스위트 green이 1차 게이트** — 생성기 스키마 변경 후 conftest 세션 fixture가
  재빌드하므로 자동 검증. `dataset.product_categories` 사용 테스트는 의미 보존(레벨 확장
  후에도 "요청 카테고리에 속함"은 성립).
- 신규 (tests/routers/test_product_related.py 또는 신규 파일):
  - anchor pivot: 존재 상품이 lv2·lv3·lv4 세 레벨을 모두 반환
  - **cascade backfill 고정**: lv4가 얇은 카테고리(상품 ~11개 < 100)에서 결과에 lv3 상품이
    유입되는 것 + product_seq 중복 없음(dedup) assert
  - 상한 동작: 누적이 `POPULAR_TOP_K_RECALL`을 넘지 않음
- 생성기: 카테고리 트리 구조 단위 테스트(레벨 수 4/12/36, parent 링크 정합, 상품당
  product_category 3행).
- KPI 스크립트는 테스트 없음(grip 동일) — 실행 검증 + README 기록.

## 6. 문서

- `docs/notes/serving.md` related 섹션: cascade 단락 추가(L4→L3→L2, dedup 누적, 조기 중단,
  global fallback 없음의 이유).
- README: related 행에 cascade 언급, KPI 실행법(`uv run python -m scripts.kpi.related ...`) 추가.

## 변경 파일 목록

| 구분 | 파일 |
|---|---|
| 신규 | `scripts/kpi/related.py`, `scripts/kpi/related.md`, `tests/test_generate_data.py`(카테고리 트리 단위 테스트), `tests/services/test_related.py`(cascade 단위·pivot/backfill 통합) |
| 수정 | `scripts/generate_data.py`, `scripts/build_mart.py`(USER_INFO_SQL lv3 필터 1곳), `app/services/related.py`, `app/routers/product_related.py`, `docs/notes/serving.md`, `README.md` |
| 무수정 확인 | `app/services/popular.py`(`fetch_by_category` 재사용), `app/ranking.py`, `app/models/products.py` |

## 성공 기준

1. `uv run pytest` 전체 green (기존 + 신규).
2. cascade backfill 통합 테스트가 lv3 유입·dedup·상한을 고정한다.
3. `uv run python -m scripts.kpi.related --deploy-date <날짜>` 가 before/after 지표 테이블을
   출력하고, 옵션 없이 실행하면 최근 창 baseline을 출력한다.
4. related 응답 계약 불변: envelope, 미존재 404, limit clamp, self 제외, seller cap.

## 후속 (이 스펙 범위 밖)

- Redis 캐시 레이어 — 이전 스펙의 후속 항목 유지.
- 생성기에 지면 귀속(referrer) 도입 시 KPI가 지면 단위로 유의미해지는지 재검 — 선택적 확장.
