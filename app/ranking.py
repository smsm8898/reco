"""서빙 랭킹 유틸 — 신호 융합(RRF), 다양성 cap, 시간감쇠(Hacker News)."""

import math
from typing import Any

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


def apply_rrf(
    rankings: list[tuple[str, list[dict[str, Any]]]],
    *,
    k: int = 60,
    id_key: str,
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
    fused.sort(key=lambda r: (-r["rrf_score"], r[id_key]))
    return fused


def apply_same_seller_cap(
    rows: list[dict[str, Any]],
    *,
    cap: int,
    seller_key: str = "seller_seq",
) -> list[dict[str, Any]]:
    """결과 내 같은 셀러 상품을 cap개로 제한한다 (순서 보존) — 다양성 장치."""
    counts: dict[Any, int] = {}
    kept: list[dict[str, Any]] = []
    for row in rows:
        seller = row.get(seller_key)
        if seller is not None:
            if counts.get(seller, 0) >= cap:
                continue
            counts[seller] = counts.get(seller, 0) + 1
        kept.append(row)
    return kept
