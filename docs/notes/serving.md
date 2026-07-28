# 추천 3구좌 설계

쇼핑 지면의 세 구좌를 각각 GET 리소스로 제공한다.

| 구좌 | 엔드포인트 | 한 줄 |
|---|---|---|
| 연관 상품 | `GET /products/{id}/related` | 지금 보는 상품과 함께 소비되는 상품 |
| 개인화 | `GET /products/personalized` | 이 유저의 취향에 맞춘 상품 |
| 인기 | `GET /products/popular` | 카테고리에서 지금 뜨는 상품 — interval dial로 day/week/month 관점 선택 |

**공통 원칙 — batch는 recall, 서빙은 ranking.** mart 테이블은 후보와 신호(카운트·임베딩)만
담고, 어떻게 섞고 자를지는 요청 시점에 서빙이 정한다. 신호는 하루 단위로 갱신되지만 융합
방식·필터는 실험하며 계속 바뀌고, 상품 상태는 분 단위로 변하기 때문이다.

**공통 계약**: 응답은 `{code, message, result}` envelope, `result`는 product_seq 리스트(순서 =
랭킹). 없는 상품은 404, 범위 밖 `limit`은 clamp(1~50). 서빙 시점 **위생 5-rule**(노출·미만료·
미삭제·가격>0·재고)로 후보를 한 쿼리에 거른다 — 상품 상태는 실시간이라 batch가 아닌 서빙이
거른다.

---

## 1. 연관 상품 — `GET /products/{id}/related`

지금 보고 있는 상품(anchor)과 **함께 소비되는** 상품을 추천한다.

### 사용 알고리즘
- **item-to-item co-occurrence** — 같은 세션/유저 안에서 anchor와 함께 발생한 view·cart·order
  카운트를 batch가 recall해두고(`mart.product_related`), 서빙이 세 신호를 각각 순위 리스트로
  분해해 **카테고리 인기**까지 4개를 **RRF(Reciprocal Rank Fusion)** 로 융합한다.
- `score(d) = Σ 1/(k + rankₗᵢₛₜ(d)), k=60` — 순위만 쓰므로 스케일이 다른 신호(view 수백 vs
  order 한 자릿수)를 정규화 없이 섞는다.
- anchor 조회 하나가 **존재 확인(404) + 카테고리 취득(인기 신호 입력)** 을 겸한다. 마지막에
  **same-seller cap**(셀러당 5개)으로 한 셀러의 지면 도배를 막는다.

### 특징
- 인기가 fallback이 아니라 **상시 융합 신호** — co-occurrence가 빈약한 신상품·롱테일 anchor를
  카테고리 인기가 자연스럽게 메운다.
- **판매중지 상품도 유효한 anchor** — 그 상품 페이지에서 지면이 뜨므로. STOP 상품은 추천
  *결과*에서만 위생 필터로 걸린다.

### 기대효과
- 상세·장바구니 지면의 **교차 판매(cross-sell)** 와 세션 지속 — "이것도 함께 보는 상품"으로
  탐색을 이어준다.
- 신상품도 빈 지면 없이 채워져 콜드스타트 구간의 이탈을 줄인다.

---

## 2. 개인화 — `GET /products/personalized`

유저의 행동 이력으로 **개인별 취향**에 맞춘 상품을 추천한다.

### 사용 알고리즘
- **ALS 행렬분해(collaborative filtering)** — implicit ALS 모델 하나를 batch에서 학습해
  (user × product × confidence) 행렬을 분해하고, 유저·상품을 같은 잠재 공간에 놓는다.
  한 모델에서 출력 둘: `recommend`(user-CF) + `similar_items`(선호 상품의 item-CF).
- 유저 프로필(`mart.user_info`)로 anchor를 잡고 user-CF와 item-CF를 **RRF 융합** → 이미 본
  상품(시드) 제외 → 부족분은 **선호 카테고리 인기로 backfill**.
- 게스트(user_seq ≤ 0)·이력 없음은 신호 계층 fast-path로 **전역 인기 콜드스타트**.

### 특징
- 인기를 **융합이 아니라 backfill**로만 쓴다 — RRF에 넣으면 범용 인기 상품이 CF와 순위를
  겨뤄 개인화를 희석시킨다. 개인화의 주인공은 CF, 인기는 못 채운 자리만 맡는다.
  (연관 구좌는 반대로 인기를 융합에 넣는다 — 같은 재료, 다른 역할.)
- 콜드스타트 여부는 응답에 드러나지 않는다 — 유저를 몰라도 추천은 항상 최선의 응답을 준다.
- gender/birth_year는 현재 프로필 보관용 — 성별×연령대 인기 후보가 다음 확장 지점.

### 기대효과
- 취향 반영으로 **CTR·CVR·재방문** 상승 — 홈/개인화 지면의 핵심 지표.
- 이력이 적은 유저도 item-CF·인기로 안전 착지, 신규 유저 경험이 비지 않는다.

---

## 3. 인기 — `GET /products/popular?category_seq=&interval=`

카테고리에서 **지금 뜨는** 상품을 추천한다. `interval`(day/week/month)로 "지금"을 보는
관점을 고른다.

### 사용 알고리즘
- **interval-free universe + 서빙 dial** — batch(`mart.popular_universe`)는 카테고리별 30일
  hourly bucket 원본(신호 4종 + seller_seq)만 recall해 쌓아둔다. 감쇠 강도(gravity)·감쇠
  해상도(bucket)·집계 창(window) 같은 dial은 여기 담지 않는다 — 실험하며 계속 바뀌는 값을
  batch에 박아두면 dial 하나 바꿀 때마다 재적재가 필요해지기 때문이다. dial은 전부 서빙의
  `INTERVAL_CONFIG`가 쥐고 있어, 값을 바꿔도 mart는 그대로고 오프라인 실험이 서빙 함수를
  직접 sweep할 수 있다 (grip-reco 미러링).

  | interval | gravity | bucket | window |
  |---|---|---|---|
  | day | 1.8 | 1h | 72h |
  | week | 1.0 | 24h | 240h |
  | month | 0.5 | 24h | 720h |

- **신호 4종 감쇠 합산 → RRF 융합** — `num_view`/`num_cart`/`num_order`/`gmv` 각각에
  `compute_popularity`(HN 시간감쇠: `signal / (ceil(age/bucket) + 2)^gravity`)를 적용해
  상품 단위로 합산한 뒤, 신호별 ranked list 4개를 **RRF(k=60)** 로 융합한다 — 정규화 없이
  스케일이 다른 신호(view는 수백, gmv는 큰 정수)를 섞을 수 있는 것이 RRF의 요점이다
  (연관 구좌와 같은 방식).
- **seller spread는 truncation 후** — 위생 필터 → RRF 융합 → top-`limit` 자르기까지 끝난
  다음에 같은 셀러가 min_gap=5 슬롯 안에 다시 나오지 않도록 greedy 재배치한다. **순서만
  바꾸고 선택(어떤 상품이 뽑혔는지)은 바꾸지 않는다** — 먼저 펼치고 자르면 잘리는 상품
  자체가 달라진다.

### 특징
- **재적재 없는 튜닝** — gravity·bucket·window가 batch 상수가 아니라 서빙의
  `INTERVAL_CONFIG`에 있어, dial을 바꿔도 mart 재빌드가 필요 없다. day/week/month는 같은
  universe를 다르게 자르고 다르게 감쇠할 뿐이다.
- **dial 선정은 오프라인 gate 몫** — 표의 수치는 `scripts/experiments/`의 오프라인 gate가
  스윕해 고른 값이다. 서빙 코드는 실험이 넘겨준 상수를 그대로 쓴다.
- 신호에 `gmv`가 들어가 "많이 본" 것과 "많이 판" 것 사이 균형도 RRF가 자연히 잡는다.

### 기대효과
- interval 하나로 "오늘 뜨는 것"부터 "이번 달 검증된 것"까지 한 엔드포인트에서 관점을
  선택할 수 있다.
- dial 튜닝과 universe 재적재가 분리돼 실험 반복 주기가 batch 스케줄에 묶이지 않는다.

---

## 테스트

- 순수 함수(RRF·same-seller cap·HN 시간감쇠 합산)는 DB 없이 단위 테스트(`tests/test_ranking.py`).
- 엔드포인트는 test DB에 데이터·mart를 실제로 빌드해 정렬·중복·위생·카테고리 소속·404·clamp를
  검증한다(`tests/routers/`, 공용 fixture는 `conftest.py`).
