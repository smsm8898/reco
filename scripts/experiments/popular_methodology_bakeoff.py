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
        WHERE event_timestamp < %(cutoff)s
          AND event_timestamp >= %(cutoff)s - make_interval(hours => %(window_hours)s)
        UNION ALL
        SELECT product_seq, date_trunc('hour', ordered_at), 0, 0, 1
        FROM activity.order_all
        WHERE ordered_at < %(cutoff)s
          AND ordered_at >= %(cutoff)s - make_interval(hours => %(window_hours)s)
    )
    SELECT pc.category_seq, e.product_seq, e.bucket_ts,
           sum(e.is_view) AS num_view, sum(e.is_cart) AS num_cart, sum(e.is_order) AS num_order
    FROM events e
    JOIN service_db.product_category pc USING (product_seq)
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
