# related cascade backfill + KPI 미러링 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** grip-reco의 related 지면 구조(anchor 레벨 pivot + L4→L3→L2 popular cascade)와 KPI 측정 스크립트 형식을 이 repo에 이식한다.

**Architecture:** 원장(`service_db.category`)에 3층 트리(lv2 4→lv3 12→lv4 36)를 추가하고 `product_category`를 상품당 3행(레벨별)으로 확장 — grip의 anchor pivot이 읽는 구조 그대로. `mart.category_popularity`는 코드 무수정으로 레벨별 인기를 갖게 되어 cascade가 재사용한다. KPI는 `scripts/kpi/related.py` + 측정 SSOT md. 스펙: `docs/superpowers/specs/2026-07-28-related-cascade-kpi-design.md`.

**Tech Stack:** Python 3.13, FastAPI, psycopg(raw SQL, no ORM), pytest, uv.

## Global Constraints

- `__init__.py` 생성 금지 — `scripts/kpi/`, `tests/services/` 포함.
- 커밋 메시지에 Claude 관련 문구·`Co-Authored-By` trailer 금지.
- `now()` 금지 — 시각 기준은 데이터의 `max(timestamp)` (KPI `--end` 기본값 포함).
- 신규 의존성 금지 — KPI 스크립트는 stdlib(`argparse`, `datetime`) + psycopg만. pandas 금지.
- Docker 이미지는 `app/`만 복사 — `scripts/` 추가분은 이미지에 안 들어감(변경 불필요).
- ruff line-length 100. 각 태스크 끝에 `uv run ruff check .`.
- integration 테스트는 로컬 PG 필요(`docker compose up -d`). "전부 skip"은 실패로 간주.
- 고정 값(스펙 그대로): 트리 lv2 4개(seq 1~4)/lv3 12개(seq 5~16)/lv4 36개(seq 17~52), cascade 순회 `(4, 3, 2)`, 상한 `POPULAR_TOP_K_RECALL`(기존 100, 값 변경 금지), global fallback 없음. KPI `--days` 기본 30, `--daily` 없음, RPM = GMV/imp×1000.
- related 응답 계약 불변: envelope `{code, message, result}`, 미존재 404, limit clamp(1~50), self 제외, seller cap 5.

## File Structure

| 파일 | 책임 |
|---|---|
| `scripts/generate_data.py` (수정) | 카테고리 3층 트리, 상품→lv4 배정, product_category 3행 |
| `scripts/build_mart.py` (수정) | `USER_INFO_SQL`의 선호 카테고리를 lv3로 고정(1곳) |
| `app/services/related.py` (수정) | anchor pivot + `fetch_popular_cascade` |
| `app/routers/product_related.py` (수정) | cascade 호출로 교체 |
| `scripts/kpi/related.py` (신규) | KPI 측정 스크립트 |
| `scripts/kpi/related.md` (신규) | 측정 SSOT |
| `tests/test_generate_data.py` (신규) | 트리 구조 단위 테스트 (DB 불필요) |
| `tests/services/test_related.py` (신규) | cascade 단위(fake) + pivot·backfill 통합 |
| `docs/notes/serving.md`, `README.md` (수정) | cascade 단락, KPI 실행법 |

---

### Task 1: 카테고리 3층 트리 (생성기 + user_info lv3 고정)

**Files:**
- Modify: `scripts/generate_data.py`, `scripts/build_mart.py`
- Test: `tests/test_generate_data.py` (신규)

**Interfaces:**
- Consumes: 기존 `generate(seed, n_users, n_products, session_scale) -> Dataset`
- Produces (Task 2·3과 테스트가 의존):
  - `service_db.category(category_seq, category_name, level int NOT NULL, parent_seq int)` — lv2는 parent NULL
  - `service_db.product_category` 상품당 3행 (lv4·lv3·lv2)
  - `Dataset.categories: list[tuple[int, str, int, int | None]]` (seq, name, level, parent)
  - 모듈 상수 `LV3_BASE_SEQ = 5`, `LV4_BASE_SEQ = 17`
  - `Dataset.preferred` 값은 lv3 seq(5~16) — answer key 의미 보존

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_generate_data.py` 신규 파일:

```python
"""생성기 카테고리 트리 단위 테스트 — DB 불필요 (작은 파라미터로 순수 생성만 검증)."""

from collections import Counter, defaultdict

from scripts.generate_data import generate

SMALL = {"seed": 7, "n_users": 30, "n_products": 60, "session_scale": 0.3}


def test_category_tree_shape() -> None:
    categories = generate(**SMALL).categories

    levels = Counter(level for _, _, level, _ in categories)
    assert levels == {2: 4, 3: 12, 4: 36}


def test_parent_links_are_consistent() -> None:
    categories = {seq: (level, parent) for seq, _, level, parent in generate(**SMALL).categories}

    for level, parent in categories.values():
        if level == 2:
            assert parent is None
        else:
            assert categories[parent][0] == level - 1  # 부모는 정확히 한 레벨 위


def test_products_map_to_three_levels() -> None:
    dataset = generate(**SMALL)
    level_of = {seq: level for seq, _, level, _ in dataset.categories}

    levels_by_product: dict[int, set[int]] = defaultdict(set)
    for product_seq, category_seq in dataset.product_categories:
        levels_by_product[product_seq].add(level_of[category_seq])

    assert len(levels_by_product) == SMALL["n_products"]
    assert all(levels == {2, 3, 4} for levels in levels_by_product.values())


def test_preferred_categories_are_lv3() -> None:
    dataset = generate(**SMALL)
    level_of = {seq: level for seq, _, level, _ in dataset.categories}

    assert all(level_of[c] == 3 for prefs in dataset.preferred.values() for c in prefs)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_generate_data.py -v`
Expected: FAIL — `categories` 튜플이 2-요소(`(seq, name)`)라 unpack 에러 또는 level 부재

- [ ] **Step 3: 생성기 구현**

`scripts/generate_data.py` 수정.

(a) 상수 추가 (`CATEGORY_NAMES` 정의 위):

```python
# 카테고리 3층 트리 — lv2(1~4) → lv3(5~16, 기존 12개 이름) → lv4(17~52).
# 유저 선호·이벤트 로직은 lv3 기준 유지(기존 12개 개념 보존), 상품은 lv4 leaf 배정.
N_LV2 = 4
LV3_PER_LV2 = 3
LV4_PER_LV3 = 3
LV3_BASE_SEQ = 5
LV4_BASE_SEQ = 17
```

(b) `Dataset.categories` 주석 교체:

```python
    categories: list[tuple[int, str, int, int | None]]  # seq, name, level, parent(lv2=None)
```

(c) `generate()` 안의 기존 두 줄

```python
    categories = [(seq, name) for seq, name in enumerate(CATEGORY_NAMES, start=1)]
    category_seqs = [seq for seq, _ in categories]
```

을 트리 생성으로 교체:

```python
    categories: list[tuple[int, str, int, int | None]] = [
        (seq, f"group-{seq}", 2, None) for seq in range(1, N_LV2 + 1)
    ]
    lv3_seqs: list[int] = []
    for idx, name in enumerate(CATEGORY_NAMES):
        seq = LV3_BASE_SEQ + idx
        categories.append((seq, name, 3, idx // LV3_PER_LV2 + 1))
        lv3_seqs.append(seq)
    lv4_seqs: list[int] = []
    lv4_parent: dict[int, int] = {}
    for j in range(len(CATEGORY_NAMES) * LV4_PER_LV3):
        seq = LV4_BASE_SEQ + j
        name = f"{CATEGORY_NAMES[j // LV4_PER_LV3]}-{j % LV4_PER_LV3 + 1}"
        categories.append((seq, name, 4, LV3_BASE_SEQ + j // LV4_PER_LV3))
        lv4_seqs.append(seq)
        lv4_parent[seq] = LV3_BASE_SEQ + j // LV4_PER_LV3
    lv3_parent = {seq: (seq - LV3_BASE_SEQ) // LV3_PER_LV2 + 1 for seq in lv3_seqs}
```

(d) 상품 루프에서 카테고리 배정 부분 교체 — 기존:

```python
        category_seq = rng.choice(category_seqs)
        product_name = f"{CATEGORY_NAMES[category_seq - 1]} item {product_seq}"
```

을:

```python
        lv4_seq = rng.choice(lv4_seqs)
        lv3_seq = lv4_parent[lv4_seq]
        product_name = f"{CATEGORY_NAMES[lv3_seq - LV3_BASE_SEQ]} item {product_seq}"
```

으로, 그리고 기존 `product_categories.append((product_seq, category_seq))` 을:

```python
        product_categories += [
            (product_seq, lv4_seq),
            (product_seq, lv3_seq),
            (product_seq, lv3_parent[lv3_seq]),
        ]
```

으로 교체.

(e) lv3 기준 유지 — `category_seqs` 사용처 3곳을 `lv3_seqs`로 교체:
- `preferred = {user_seq: rng.sample(lv3_seqs, k=rng.choice([2, 3])) ...}`
- `by_category` 루프: `for category_seq in lv3_seqs:` (product_categories의 lv3 행만 걸리므로 로직 동일)
- `pick_category`의 fallback: `return rng.choice(lv3_seqs)`

(f) DDL의 category 테이블 교체:

```sql
CREATE TABLE service_db.category (
    category_seq int PRIMARY KEY,
    category_name text NOT NULL,
    level int NOT NULL,
    parent_seq int
);
```

(g) `COPY_TARGETS`의 categories 항목 교체:

```python
    ("categories", "service_db.category (category_seq, category_name, level, parent_seq)"),
```

- [ ] **Step 4: build_mart의 user_info를 lv3로 고정**

`scripts/build_mart.py`의 `USER_INFO_SQL` `viewed` CTE — 기존:

```sql
    FROM mart.user_recent_views urv
    JOIN service_db.product_category pc USING (product_seq)
```

을:

```sql
    FROM mart.user_recent_views urv
    JOIN service_db.product_category pc USING (product_seq)
    -- 선호 카테고리는 lv3 기준 — product_category가 레벨별 3행이라 필터 없이는
    -- 집계 범위가 넓은 lv2가 항상 이겨 선호가 lv2로 뭉개진다
    JOIN service_db.category c USING (category_seq)
    WHERE c.level = 3
```

으로 교체 (WHERE가 이미 있으면 AND로 결합 — 현재 이 CTE에는 WHERE 없음).

- [ ] **Step 5: 신규 테스트 통과 + 전체 스위트 green 확인**

Run: `uv run pytest tests/test_generate_data.py -v` → PASS
Run: `uv run pytest && uv run ruff check .` → 전체 PASS (conftest가 test DB를 새 스키마로 재빌드 — personalized·related·popular 기존 계약이 그대로 성립해야 한다. 실패 시 이 태스크에서 원인 파악, 다음 태스크로 넘기지 말 것)

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_data.py scripts/build_mart.py tests/test_generate_data.py
git commit -m "feat(data): 카테고리 3층 트리 — lv2/lv3/lv4, product_category 레벨별 3행"
```

---

### Task 2: anchor 레벨 pivot + popular cascade

**Files:**
- Modify: `app/services/related.py`, `app/routers/product_related.py`, `docs/notes/serving.md`
- Test: `tests/services/test_related.py` (신규 — `__init__.py` 만들지 말 것)

**Interfaces:**
- Consumes: Task 1의 category 트리·3행 매핑, 기존 `popular.fetch_by_category(pool, category_seq, fetch_limit) -> list[dict]`(각 row는 `{product_seq, score, rank}`), 기존 `related.POPULAR_TOP_K_RECALL = 100`
- Produces:
  - `related.fetch_anchor(pool, product_seq) -> dict[int, int] | None` — **반환 형태 변경**: 미존재 `None`, 그 외 `{level: category_seq}`(매핑 없으면 `{}`)
  - `related.fetch_popular_cascade(pool, anchor_cats: dict[int, int]) -> list[dict[str, Any]]`
  - `build_related_result`·`fetch_cf`·RRF·seller cap은 무수정

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/services/test_related.py` 신규 파일:

```python
"""related 서비스 테스트 — cascade는 fake 주입 단위 테스트, pivot·backfill은 test DB 통합."""

import asyncio
from collections import defaultdict
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from app.services import popular, related
from scripts.generate_data import Dataset
from tests.conftest import db_url, postgres_available

_db = pytest.mark.skipif(not postgres_available(), reason="PostgreSQL 없음 — integration skip")


def _rows(*seqs: int) -> list[dict[str, Any]]:
    return [{"product_seq": seq, "score": 1.0, "rank": i} for i, seq in enumerate(seqs, 1)]


def test_cascade_visits_lv4_first_and_dedups(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {40: _rows(1, 2), 30: _rows(2, 3), 20: _rows(4)}

    async def fake_fetch(pool, category_seq, limit):
        return responses[category_seq]

    monkeypatch.setattr(popular, "fetch_by_category", fake_fetch)

    rows = asyncio.run(related.fetch_popular_cascade(None, {4: 40, 3: 30, 2: 20}))

    # lv4(1,2) → lv3에서 2는 dedup·3 유입 → lv2에서 4
    assert [r["product_seq"] for r in rows] == [1, 2, 3, 4]


def test_cascade_stops_at_recall_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(pool, category_seq, limit):
        base = category_seq * 1000
        return _rows(*range(base, base + related.POPULAR_TOP_K_RECALL))

    monkeypatch.setattr(popular, "fetch_by_category", fake_fetch)

    rows = asyncio.run(related.fetch_popular_cascade(None, {4: 1, 3: 2, 2: 3}))

    assert len(rows) == related.POPULAR_TOP_K_RECALL
    assert all(r["product_seq"] < 2000 for r in rows)  # lv4(base 1000)에서 다 채움 — 상위 미유입


def test_cascade_without_mapping_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(pool, category_seq, limit):
        raise AssertionError("매핑이 없으면 조회 자체가 없어야 한다")

    monkeypatch.setattr(popular, "fetch_by_category", fake_fetch)

    assert asyncio.run(related.fetch_popular_cascade(None, {})) == []


@_db
def test_anchor_pivot_returns_three_levels(dataset: Dataset) -> None:
    level_of = {seq: level for seq, _, level, _ in dataset.categories}

    async def run():
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            return (
                await related.fetch_anchor(pool, 1),
                await related.fetch_anchor(pool, 9_999_999),
            )
        finally:
            await pool.close()

    anchor, missing = asyncio.run(run())

    assert missing is None
    assert sorted(anchor) == [2, 3, 4]
    assert all(level_of[seq] == level for level, seq in anchor.items())


@_db
def test_cascade_backfills_thin_lv4_from_higher_levels(dataset: Dataset) -> None:
    # 테스트 스케일(상품 400)에서 lv4는 카테고리당 ~11개 — 단독으로 100을 못 채워
    # 상위 레벨 backfill이 실제로 동작한다
    products_in: dict[int, set[int]] = defaultdict(set)
    for product_seq, category_seq in dataset.product_categories:
        products_in[category_seq].add(product_seq)

    async def run():
        pool = AsyncConnectionPool(db_url(), open=False)
        await pool.open()
        try:
            anchor = await related.fetch_anchor(pool, 1)
            return anchor, await related.fetch_popular_cascade(pool, anchor)
        finally:
            await pool.close()

    anchor, rows = asyncio.run(run())
    seqs = [r["product_seq"] for r in rows]

    assert len(seqs) == len(set(seqs))  # dedup
    assert len(seqs) <= related.POPULAR_TOP_K_RECALL
    assert any(seq not in products_in[anchor[4]] for seq in seqs)  # lv4 밖 유입 = backfill
    assert set(seqs) <= products_in[anchor[2]]  # 전부 anchor의 lv2 우산 안 (global 미유입)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/services/test_related.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'fetch_popular_cascade'`

- [ ] **Step 3: 서비스 구현**

`app/services/related.py` 수정.

(a) import에 popular 추가: `from app.services import hygiene, popular`

(b) `_ANCHOR_SQL` 교체 (grip pivot 미러):

```python
# anchor 존재 확인(404 겸용) + 레벨별 카테고리 취득을 한 쿼리로 (grip-reco pivot 미러).
# LEFT JOIN이라 매핑 없는 상품도 1행 — 레벨 컬럼만 NULL이 된다.
_ANCHOR_SQL = """
    SELECT p.product_seq,
           MAX(CASE WHEN c.level = 2 THEN c.category_seq END) AS lv2_category_seq,
           MAX(CASE WHEN c.level = 3 THEN c.category_seq END) AS lv3_category_seq,
           MAX(CASE WHEN c.level = 4 THEN c.category_seq END) AS lv4_category_seq
    FROM service_db.product_info p
    LEFT JOIN service_db.product_category pc USING (product_seq)
    LEFT JOIN service_db.category c USING (category_seq)
    WHERE p.product_seq = %s
    GROUP BY p.product_seq
"""
```

(c) `fetch_anchor` 교체:

```python
async def fetch_anchor(pool: AsyncConnectionPool, product_seq: int) -> dict[int, int] | None:
    """anchor의 레벨별 카테고리 — 미존재면 None(router가 404), 매핑 없으면 {}."""
    async with pool.connection() as conn:
        cur = await conn.execute(_ANCHOR_SQL, (product_seq,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        level: int(row[idx]) for level, idx in ((2, 1), (3, 2), (4, 3)) if row[idx] is not None
    }
```

(d) Fetch 섹션에 cascade 추가:

```python
async def fetch_popular_cascade(
    pool: AsyncConnectionPool, anchor_cats: dict[int, int]
) -> list[dict[str, Any]]:
    """popular cascade — L4→L3→L2 dedup 누적, POPULAR_TOP_K_RECALL 채우면 조기 중단.

    좁은 leaf(lv4)의 인기를 우선하되 얇으면 상위 레벨이 backfill한다. 매치되는
    카테고리가 없으면 popular 미제공 — global fallback은 두지 않는다(무관 상품이
    '연관' 지면에 섞이는 것보다 빈 신호가 낫다). hygiene 미적용 — build_related_result 몫.
    """
    popular_rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for level in (4, 3, 2):
        if len(popular_rows) >= POPULAR_TOP_K_RECALL:
            break
        category_seq = anchor_cats.get(level)
        if category_seq is None:
            continue
        level_rows = await popular.fetch_by_category(pool, category_seq, POPULAR_TOP_K_RECALL)
        for row in level_rows:
            if row["product_seq"] in seen:
                continue
            seen.add(row["product_seq"])
            popular_rows.append(row)
            if len(popular_rows) >= POPULAR_TOP_K_RECALL:
                break
    return popular_rows
```

(e) 모듈 docstring의 호출 순서 설명(1~3번 항목)을 새 흐름으로 갱신:

```python
"""연관 상품 서빙 — batch(recall)가 모아둔 신호를 요청 시점에 융합(ranking)한다.

router(product_related.py)의 실제 호출 순서:
1. fetch_anchor — 존재 확인(미존재 404) + 레벨별(lv2/3/4) 카테고리 pivot을 한 쿼리로
2. fetch_cf / fetch_popular_cascade(L4→L3→L2 dedup 누적)를 asyncio.gather로 동시 취득
3. build_related_result 가 위생 필터 → 신호 3개 ⊕ popular RRF 융합 → self 제외
   → same-seller cap → top-limit 으로 product_seq 리스트를 만든다 (응답 조립은 라우터 몫)
"""
```

- [ ] **Step 4: 라우터 교체**

`app/routers/product_related.py` — import에서 `popular` 제거(`from app.services import related`), 본문의 anchor·gather 부분 교체:

```python
    # 1) anchor 확정 — 존재하지 않는 상품은 404 (없는 리소스의 하위 자원)
    anchor_cats = await related.fetch_anchor(pool, product_seq)
    if anchor_cats is None:
        raise HTTPException(status_code=404, detail="product not found")

    # 2) CF 신호와 popular cascade는 서로 독립 — 동시 취득
    cf_rows, popular_rows = await asyncio.gather(
        related.fetch_cf(pool, product_seq),
        related.fetch_popular_cascade(pool, anchor_cats),
    )
```

- [ ] **Step 5: 테스트 + 전체 스위트**

Run: `uv run pytest tests/services/test_related.py tests/routers/test_product_related.py -v`
Expected: PASS (기존 related 계약 — 404·self 제외·seller cap·clamp — 전부 유지)
Run: `uv run pytest && uv run ruff check .` → 전체 PASS

- [ ] **Step 6: serving.md에 cascade 단락 추가**

`docs/notes/serving.md`의 연관 상품 섹션 "사용 알고리즘"에 항목 추가:

```markdown
- **popular cascade (L4→L3→L2)** — anchor의 카테고리를 레벨별로 pivot해 좁은 leaf(lv4)의
  인기부터 채우고, 얇으면 상위 레벨(lv3→lv2)이 dedup 누적으로 backfill한다(100 채우면
  조기 중단). global 인기 last-resort는 두지 않는다 — 무관 상품이 '연관' 지면에 섞이는
  것보다 빈 신호가 낫다.
```

- [ ] **Step 7: Commit**

```bash
git add app/services/related.py app/routers/product_related.py tests/services/test_related.py docs/notes/serving.md
git commit -m "feat(related): anchor 레벨 pivot + popular cascade backfill (L4→L3→L2)"
```

---

### Task 3: KPI 미러 — `scripts/kpi/related.py` + SSOT md

**Files:**
- Create: `scripts/kpi/related.py`, `scripts/kpi/related.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `activity.events_all`(view_item_list/select_item), `activity.logs`, `activity.order_all`, `service_db.product_info.selling_price`, `settings.pg_conninfo()`
- Produces: `uv run python -m scripts.kpi.related [--end] [--days] [--deploy-date]` — 기간×지표 콘솔 테이블. 테스트 없음(grip 동일 — 실행 검증).

- [ ] **Step 1: 스크립트 작성**

`scripts/kpi/related.py` 신규 파일 (`scripts/kpi/`에 `__init__.py` 만들지 말 것):

```python
"""연관상품 지면 KPI — grip-reco src/kpi/shopping/related.py 형식 미러링.

grip은 GA(EVENTS_ALL) 노출·클릭 + ES referrer 전환 귀속의 2-substrate 하이브리드지만,
로컬 합성 데이터에는 지면 귀속(referrer)이 없다 — 전 지표가 피드 전체 기준 단일
substrate(PG)로 단순화된다. --deploy-date의 before/after 창 분할은 형식 재현이다:
합성 이벤트는 추천 노출의 영향을 받지 않으므로 전후 차이가 없는 것이 정상.
지표 정의·한계는 related.md 참조. north-star = RPM (grip 동일).

실행: uv run python -m scripts.kpi.related [--end YYYY-MM-DD] [--days 30] [--deploy-date YYYY-MM-DD]
"""

import argparse
import datetime

import psycopg

from app.core.settings import settings

IMPRESSION_SQL = """
    SELECT count(*) FILTER (WHERE event_name = 'view_item_list') AS imp,
           count(*) FILTER (WHERE event_name = 'select_item') AS clk,
           count(DISTINCT user_seq) FILTER (WHERE event_name = 'view_item_list') AS imp_users
    FROM activity.events_all
    WHERE event_timestamp >= %(start)s AND event_timestamp < %(end)s
"""

FUNNEL_SQL = """
    SELECT log_type, count(*) AS n_events
    FROM activity.logs
    WHERE event_timestamp >= %(start)s AND event_timestamp < %(end)s
    GROUP BY log_type
"""

# GMV = 주문수 × 현재 selling_price — 주문 시점 가격이 원천에 없는 시뮬레이션 한계 (mart와 동일 관습)
ORDERS_SQL = """
    SELECT count(*) AS n_orders, coalesce(sum(p.selling_price), 0) AS gmv
    FROM activity.order_all o
    JOIN service_db.product_info p USING (product_seq)
    WHERE o.ordered_at >= %(start)s AND o.ordered_at < %(end)s
"""

# --end 기본값 — now() 금지, 데이터의 '현재'(최신 이벤트 다음날 = exclusive 상한)
DEFAULT_END_SQL = "SELECT (max(event_timestamp))::date + 1 FROM activity.events_all"


# ── 지표 파생 (순수 함수) ─────────────────────────────────────────────────────


def _pct(num: float, den: float) -> float:
    return num / den * 100 if den else 0.0


def _div(num: float, den: float) -> float:
    return num / den if den else 0.0


def derive_metrics(
    imp: int, clk: int, imp_users: int, views: int, carts: int, n_orders: int, gmv: float
) -> dict[str, float]:
    """CTR=clk/imp(피드 전체), 퍼널 view→cart→order, CVR=주문/VIEW, RPM=GMV/imp×1000."""
    return {
        "imp": imp,
        "clk": clk,
        "imp_users": imp_users,
        "ctr": _pct(clk, imp),
        "views": views,
        "carts": carts,
        "orders": n_orders,
        "cvr_pct": _pct(n_orders, views),
        "gmv": gmv,
        "rpm": _div(gmv, imp) * 1000,
        "aov": _div(gmv, n_orders),
    }


# ── fetch ────────────────────────────────────────────────────────────────────


def fetch_window(conn: psycopg.Connection, start: str, end: str) -> dict[str, float]:
    params = {"start": start, "end": end}
    imp, clk, imp_users = conn.execute(IMPRESSION_SQL, params).fetchone()
    funnel = dict(conn.execute(FUNNEL_SQL, params).fetchall())
    n_orders, gmv = conn.execute(ORDERS_SQL, params).fetchone()
    return derive_metrics(
        imp=imp,
        clk=clk,
        imp_users=imp_users,
        views=funnel.get("VIEW_PRODUCT", 0),
        carts=funnel.get("ADD_CART", 0),
        n_orders=n_orders,
        gmv=float(gmv),
    )


# ── 기간 (baseline 또는 before/after) — grip build_periods 미러 ────────────────


def build_periods(end: str, days: int, deploy_date: str | None) -> list[tuple[str, str, str]]:
    """(label, start, end)[]. deploy_date 없으면 최근 창 단일, 있으면 before/after.

    - baseline: [최근 days 창]
    - before/after: before=[deploy-days, deploy), after=[deploy, end) — cutover 비교
    """
    if deploy_date:
        before_start = (
            datetime.date.fromisoformat(deploy_date) - datetime.timedelta(days=days)
        ).isoformat()
        return [
            (f"before (~{deploy_date})", before_start, deploy_date),
            (f"after ({deploy_date}~)", deploy_date, end),
        ]
    start = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days)).isoformat()
    return [("recent", start, end)]


# ── 렌더 (행=기간 × 열=지표, before/after면 Δ% 행) — pandas 없이 고정폭 ─────────

MATRIX_COLS: list[tuple[str, str, str]] = [
    ("imp", "imp", "{:,.0f}"),
    ("clk", "clk", "{:,.0f}"),
    ("CTR%", "ctr", "{:.3f}"),
    ("VIEW", "views", "{:,.0f}"),
    ("AtC", "carts", "{:,.0f}"),
    ("orders", "orders", "{:,.0f}"),
    ("CVR%", "cvr_pct", "{:.3f}"),
    ("GMV", "gmv", "{:,.0f}"),
    ("RPM", "rpm", "{:,.1f}"),
    ("AOV", "aov", "{:,.0f}"),
]


def render_table(labels: list[str], by_period: list[dict[str, float]]) -> str:
    rows = [[fmt.format(m[key]) for _, key, fmt in MATRIX_COLS] for m in by_period]
    if len(by_period) == 2:  # before/after → Δ% 행 (grip 미러)
        before, after = by_period
        rows.append(
            [
                f"{(after[key] - before[key]) / before[key] * 100:+.1f}" if before[key] else "—"
                for _, key, _ in MATRIX_COLS
            ]
        )
        labels = [*labels, "Δ%"]
    header = ["period", *[h for h, _, _ in MATRIX_COLS]]
    table = [header, *[[label, *row] for label, row in zip(labels, rows)]]
    widths = [max(len(cell) for cell in col) for col in zip(*table)]
    return "\n".join(
        "  ".join(cell.rjust(width) for cell, width in zip(line, widths)) for line in table
    )


# ── 실행 ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="쇼핑 KPI — 연관상품 (로컬 미러)")
    parser.add_argument("--end", default=None, help="집계 종료(exclusive). 기본=데이터 최신 다음날")
    parser.add_argument("--days", type=int, default=30, help="기간당 윈도우 길이(일). 기본 30")
    parser.add_argument("--deploy-date", default=None, help="설정 시 before/after 비교. 미설정=baseline")
    args = parser.parse_args()

    with psycopg.connect(settings.pg_conninfo()) as conn:
        end = args.end or conn.execute(DEFAULT_END_SQL).fetchone()[0].isoformat()
        periods = build_periods(end, args.days, args.deploy_date)
        by_period = [fetch_window(conn, start, stop) for _, start, stop in periods]

    mode = "before/after (cutover 비교 — 형식 재현)" if args.deploy_date else "baseline (최근 창)"
    print(f"# 쇼핑 KPI — 연관상품 (로컬)  · {mode} · [{periods[0][1]}, {end})\n")
    print(render_table([label for label, _, _ in periods], by_period))
    print(
        "\n> CTR=clk/imp (피드 전체 — 지면 귀속 없음). CVR=주문/VIEW. GMV=주문수×현재가."
        "\n> 합성 이벤트는 추천 노출과 무관 — before/after 무차이가 정상. 상세는 related.md."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 측정 SSOT 작성**

`scripts/kpi/related.md` 신규 파일:

```markdown
# 연관상품 지면 KPI — 측정 SSOT (로컬 미러)

grip-reco `src/kpi/shopping/related.md`의 로컬 대응물. 지표 정의가 코드와 다르면 이 문서가 SSOT.

## 지표 정의

| 지표 | 정의 | 소스 |
|---|---|---|
| imp | `view_item_list` 이벤트 수 | `activity.events_all` |
| clk | `select_item` 이벤트 수 | `activity.events_all` |
| CTR% | clk / imp × 100 | 〃 (동일 substrate — self-consistent) |
| VIEW | `VIEW_PRODUCT` 로그 수 | `activity.logs` |
| AtC | `ADD_CART` 로그 수 | `activity.logs` |
| orders | 주문 건수 | `activity.order_all` |
| CVR% | orders / VIEW × 100 | 교차 (logs × order_all) |
| GMV | 주문수 × 현재 `selling_price` | order_all ⋈ product_info |
| RPM | GMV / imp × 1000 — north-star | 교차 (grip 동일 관습) |
| AOV | GMV / orders | 〃 |

## grip 대비 구조 차이

- grip은 2-substrate(GA 참여 ‖ ES referrer 전환)를 ‖로 분리 표기 — 로컬은 단일 PG substrate라 구분 없음.
- grip의 지면 귀속(REFERRERTYPE=40/REFERRERSEQ=24)에 해당하는 필드가 로컬 데이터에 없다.
- `--daily`, imp_sessions, AtW(찜), 주문 라인/건 구분은 로컬 데이터에 개념이 없어 생략.

## 한계 (해석 시 주의)

1. **지면 귀속 불가** — referrer가 없어 CTR·퍼널이 related 지면이 아니라 피드 전체 기준이다.
   이 스크립트는 측정 코드의 형식(창 분할·퍼널·SSOT 관습)을 재현하는 것이 목적이다.
2. **before/after 무차이가 정상** — 합성 이벤트는 추천 노출의 영향을 받지 않는다.
   `--deploy-date`는 cutover 비교의 형식만 재현한다.

## 실행

- baseline: `uv run python -m scripts.kpi.related`
- cutover 비교: `uv run python -m scripts.kpi.related --deploy-date 2026-03-01`
```

- [ ] **Step 3: 실행 검증**

dev DB가 준비돼 있어야 한다 (이전 사이클에서 사용 — 없으면 `docker compose up -d && uv run python -m scripts.generate_data && uv run python -m scripts.build_mart`. 단, **Task 1이 카테고리 스키마를 바꿨으므로 dev DB는 어차피 재생성 필요**: `uv run python -m scripts.generate_data && uv run python -m scripts.build_mart`).

Run: `uv run python -m scripts.kpi.related`
Expected: recent 1행 테이블 (imp/clk/CTR/VIEW/AtC/orders/CVR/GMV/RPM/AOV)

Run: `uv run python -m scripts.kpi.related --deploy-date 2026-03-01`
Expected: before/after 2행 + Δ% 행 (값은 비슷 — 정상)

- [ ] **Step 4: README 갱신**

- related 엔드포인트 행의 설명을 cascade 반영으로 교체 (표 형식 유지):

```markdown
| `GET /api/v1/products/{product_seq}/related` | 연관 상품 — CF 3신호 ⊕ popular cascade(L4→L3→L2) RRF 융합 | ✅ |
```

- 오프라인 실험 섹션 아래에 KPI 섹션 추가:

```markdown
## KPI (scripts/kpi/)

지면별 KPI 측정 스크립트 — 측정 정의는 각 `.md`(SSOT) 참조. 로컬 데이터에는 지면
귀속이 없어 형식 재현이 목적이다(창 분할·퍼널·before/after 관습).

- `uv run python -m scripts.kpi.related [--deploy-date YYYY-MM-DD]` — 연관상품 지면
```

- [ ] **Step 5: lint + 전체 테스트**

Run: `uv run ruff check . && uv run pytest`
Expected: 전부 PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/kpi/related.py scripts/kpi/related.md README.md
git commit -m "feat(kpi): related 지면 KPI 미러 — 창 분할·퍼널·RPM (scripts/kpi)"
```
