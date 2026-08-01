import math
from collections.abc import Sequence
from typing import Any

GRAVITY = 1.8  # Hacker News 기본값 — interval별 오버라이드는 서빙 INTERVAL_CONFIG 몫
RRF_K = 60  # Reciprocal Rank Fusion k 기본값 (Cormack et al. 2009)


def compute_popularity(
    signal: float, age: float, *, bucket: float, gravity: float = GRAVITY
) -> float:
    """HN 시간감쇠: signal / (ceil(age / bucket) + 2) ** gravity.

    age·bucket 단위는 시간(h). bucket 올림이라 같은 bucket 안의 이벤트는 같은 감쇠를
    받는다 — 감쇠 해상도(1h/24h)를 dial로 고를 수 있게 한 것 (grip-reco 시그니처).
    음수 age(미래 이벤트)는 0으로 clamp.
    """
    return signal / (math.ceil(max(age, 0.0) / bucket) + 2) ** gravity


def apply_rrf(
    rankings: list[tuple[str, list[dict[str, Any]]]],
    *,
    k: int = RRF_K,
    id_key: str = "product_seq",
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion (Cormack et al. 2009).

    각 ranked list에서의 순위 r에 대해 1/(k+r)을 합산한다. 점수 스케일이 다른
    신호들(view 수백 vs order 한 자릿수)을 정규화 없이 융합할 수 있다는 것이 요점.
    반환 row에는 융합 점수가 `rrf_score`로 붙는다.
    """
    scores: dict[Any, float] = {}
    merged: dict[Any, dict[str, Any]] = {}
    for _, rows in rankings:
        for position, row in enumerate(rows, start=1):
            key = row[id_key]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
            merged.setdefault(key, {}).update(row)

    fused = [{**row, "rrf_score": scores[key]} for key, row in merged.items()]
    fused.sort(key=lambda r: -r["rrf_score"])
    return fused


def apply_same_seller_cap(
    rows: list[dict[str, Any]],
    *,
    cap: int,
    seller_of: dict[int, int],
) -> list[dict[str, Any]]:
    """결과 내 같은 셀러 상품을 cap개로 제한한다 (순서 보존) — 다양성 장치.

    셀러는 row 가 아니라 `seller_of`(product_seq → seller_seq)에서 읽는다 — 후보 테이블에
    셀러가 없는 지면이 있어서, row 에 억지로 끼워 넣는 대신 조회 맵을 넘긴다.
    """
    counts: dict[Any, int] = {}
    kept: list[dict[str, Any]] = []
    for row in rows:
        seller = seller_of.get(row["product_seq"])
        if seller is not None:
            if counts.get(seller, 0) >= cap:
                continue
            counts[seller] = counts.get(seller, 0) + 1
        kept.append(row)
    return kept


def derive_source_rankings(
    rows: list[dict[str, Any]], signals: Sequence[str]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """RRF 입력 파생 — 카운트 컬럼 여러 개를 가진 row 한 벌을 신호별 ranked list 로 편다.

    `[{seq, view, cart}, ...]` → `[("view", [seq…]), ("cart", [seq…])]`.
    컬럼명을 그대로 source label 로 쓴다 — 어느 신호가 기여했는지 로그·디버깅에서 바로 읽힌다.
    """
    rankings: list[tuple[str, list[dict[str, Any]]]] = []
    for signal in signals:
        # 값이 0 이면 그 신호의 순위에서 뺀다 — "신호 없음"과 "신호 최하위"는 다르다.
        # 남겨두면 view 가 0 인 상품도 view 순위에 들어 1/(k+rank) 를 받는다.
        scored = [row for row in rows if row[signal] > 0]
        scored.sort(key=lambda row: row[signal], reverse=True)
        rankings.append((signal, scored))
    return rankings


def apply_seller_spread(
    rows: list[dict[str, Any]], min_gap: int, seller_of: dict[int, int]
) -> list[dict[str, Any]]:
    """같은 셀러가 min_gap 칸 안에 재등장하지 않도록 재배치. 집합 불변(cap과 달리 버리지 않는다).

    **원리 — 슬롯을 앞에서부터 채우는 greedy.** 결과를 한 칸씩 만들어 가면서 매 칸마다 "직전
    min_gap-1칸에 나온 셀러"를 금지 집합(window)으로 잡고, 남은 후보를 **점수 순서대로** 훑어
    그 집합에 없는 첫 후보를 놓는다. 점수 순으로 훑으니 제약을 지키는 선에서는 늘 최고 점수를
    고르게 되고, 밀려난 후보는 사라지지 않고 다음 칸에서 다시 후보가 된다.

    제약을 만족하는 후보가 하나도 없으면(남은 게 전부 최근 셀러) `next(..., 0)` 의 기본값으로
    **점수 1등에 양보**한다 — 다양성보다 지면을 채우는 쪽이 우선이다. 그래서 min_gap 은 보장이
    아니라 best-effort 이고, 대신 입력 원소는 하나도 잃지 않는다(누락·중복 없음).

    **복잡도 O(n²)** (n=len(rows), g=min_gap). while 이 n 회, 각 회차가 window 구성 O(g) +
    후보 스캔 O(n) + `list.pop(i)` O(n) → O(n·(n+g)). n 이 truncation 끝난 top-limit(≤50)이라
    무시할 수준이다 — 뒤집으면 후보 전체(수백~수천)에 돌리면 안 되는 이유이기도 하다.

    truncation 후에 실행해야 한다 — 먼저 펼치면 어떤 상품이 잘리는지(선택)가 바뀐다.
    min_gap <= 1이면 no-op. 셀러는 cap 과 같이 `seller_of` 에서 읽는다.
    """
    if min_gap <= 1:
        return rows
    remaining = list(rows)
    out: list[dict[str, Any]] = []
    while remaining:
        window = {seller_of[r["product_seq"]] for r in out[-(min_gap - 1) :]}
        pick = next(
            (i for i, r in enumerate(remaining) if seller_of[r["product_seq"]] not in window), 0
        )
        out.append(remaining.pop(pick))
    return out
