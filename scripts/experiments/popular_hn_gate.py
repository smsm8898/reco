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
