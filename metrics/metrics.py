"""쇼핑 지면 KPI — related·popular·personalized 세 지면을 한 도구로 측정한다.

**측정하는 것은 숫자가 아니라 규율이다.** 합성 이벤트는 추천 노출의 영향을 받지 않으므로 지면 간
숫자가 비슷하고 before/after 차이도 없는 것이 정상이다. 재현 대상은 창 규율 — 길이정합 → `/일`
환산 → 표본 가드 → washout — 즉 "그 숫자로 무엇을 판정해도 되는가" 다.

지면이 실제로 다른 것은 **cutover 지정 방식과 문구뿐**이다(아래 SURFACES):
- related·personalized — `--deploy-date` 인자. 배포일이 정해져 있지 않아 호출자가 고른다.
- popular — 전환일이 하나로 정해진 지면이라 상수로 고정하고, 그 하루를 **washout 으로 양쪽에서
  제외**한 뒤 참고 라인으로 따로 보여준다.

실행:
    uv run python -m metrics.metrics related --deploy-date 2026-02-16 --daily
    uv run python -m metrics.metrics popular
    uv run python -m metrics.metrics personalized --baseline
"""

import argparse
import datetime
from dataclasses import dataclass, field
from typing import Any

import psycopg

from app.core.settings import settings

# =========== 판정 규율 상수 ===========

# 주문 표본이 이 아래면 주문 기반 **평균 Δ%** 를 판정 근거로 쓰지 말고 순위 검정으로 가라는 임계.
# 커머스 주문은 heavy-tail 이라 창을 어디서 자르냐로 CVR 이 크게 뒤집힌다. 미달이 "측정 불가"라는
# 뜻은 아니다 — 순위 검정은 표본이 작아도 유효하다. 이 가드는 **도구를 바꾸라는 신호**다.
# 미러 한계: 합성 데이터의 주문 표본은 이 임계를 넉넉히 넘어 가드가 항상 통과한다(형식 재현).
HEAVY_TAIL_PURCHASE_FLOOR = 100

# 창 길이에 선형인 누적 지표 — 전부 이벤트 건수·합계라 기간에 비례하므로 표에는 `/일` 환산으로
# 싣는다. imp_users 는 distinct 유저라 창을 늘리면 포화(sublinear) → 환산 대상이 아니다.
CUMULATIVE_KEYS = {"imp", "clk", "views", "carts", "orders", "gmv"}

# 행렬 열 구성 — 세 지면이 같은 열을 같은 순서로 쓴다. 나란히 읽는 지면이라 어긋나면 오독한다.
MATRIX_COLS: list[tuple[str, str, str | None]] = [
    ("imp/일", "imp", "{:,.0f}"),
    ("clk/일", "clk", "{:,.1f}"),
    ("CTR%", "ctr", "{:.3f}"),
    ("VIEW/일", "views", "{:,.1f}"),
    ("AtC/일", "carts", "{:,.1f}"),
    ("주문/일", "orders", "{:,.2f}"),
    ("CVR%", "cvr_pct", "{:.3f}"),
    ("GMV/일", "gmv", "₩{:,.0f}"),
    ("RPM", "rpm", "₩{:,.0f}"),
    ("AOV", "aov", "₩{:,.0f}"),
]

# 누적 원값 표본 라인 — 표는 `/일` 환산이라 표본 크기가 안 보이므로 따로 남긴다.
SAMPLE_COLS: list[tuple[str, str, str]] = [
    ("imp", "imp", "{:,.0f}"),
    ("VIEW", "views", "{:,.0f}"),
    ("주문", "orders", "{:,.0f}"),
    ("GMV", "gmv", "₩{:,.0f}"),
]


# =========== 지면 설정 ===========


@dataclass(frozen=True)
class Surface:
    """지면별로 정당하게 다른 것만 담는다 — SQL·지표·창 규율·렌더는 전부 공용."""

    title: str
    # 전환일이 정해진 지면만 채운다. before 는 이 날 0시까지, after 는 다음날부터 —
    # 전환 당일은 두 알고리즘이 섞여 오염이라 양쪽에서 뺀다.
    washout_day: str | None = None
    notes: list[str] = field(default_factory=list)


SURFACES: dict[str, Surface] = {
    "related": Surface(
        title="연관상품",
        notes=["CF(co-occurrence) ⊕ 카테고리 인기를 RRF 로 융합하는 지면."],
    ),
    "popular": Surface(
        title="카테고리 인기상품",
        # 미러에는 실제 배포가 없어 데이터 시간축(2026-01-01 ~ 04-01) 중앙에 픽션으로 고정한다.
        # 상수를 쓰는 이유는 "이 지면의 전환일은 하나로 정해져 있다"는 성질을 도구에 담기 위해서다.
        washout_day="2026-02-15",
        notes=["전환일은 washout 으로 양쪽에서 제외했다 — 버린 하루는 참고 라인으로 따로 보인다."],
    ),
    "personalized": Surface(
        title="개인화",
        notes=["인기를 융합이 아니라 backfill 로만 쓰는 지면 — 그 차이도 이 표에 섞여 있다."],
    ),
}


# =========== SQL ===========

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

# GMV = 주문수 × 현재 selling_price — 주문 시점 가격이 원천에 없는 시뮬레이션 한계 (mart 동일 관습)
ORDERS_SQL = """
    SELECT count(*) AS n_orders, coalesce(sum(p.selling_price), 0) AS gmv
    FROM activity.order_all o
    JOIN service_db.product_info p USING (product_seq)
    WHERE o.ordered_at >= %(start)s AND o.ordered_at < %(end)s
"""

# 일별 추이 — 소표본 진동을 눈으로 확인하는 용도(창 선택 artifact 를 Δ% 로 오독하지 않기 위해)
DAILY_VIEWS_SQL = """
    SELECT event_timestamp::date AS day, count(*) AS views
    FROM activity.logs
    WHERE log_type = 'VIEW_PRODUCT'
      AND event_timestamp >= %(start)s AND event_timestamp < %(end)s
    GROUP BY 1
"""

DAILY_ORDERS_SQL = """
    SELECT o.ordered_at::date AS day, count(*) AS orders,
           coalesce(sum(p.selling_price), 0) AS gmv
    FROM activity.order_all o
    JOIN service_db.product_info p USING (product_seq)
    WHERE o.ordered_at >= %(start)s AND o.ordered_at < %(end)s
    GROUP BY 1
"""

# --end 기본값 — now() 금지, 데이터의 '현재'(최신 이벤트 다음날 = exclusive 상한)
DEFAULT_END_SQL = "SELECT (max(event_timestamp))::date + 1 FROM activity.events_all"


# =========== 지표 파생 ===========


def pct(num: float, den: float) -> float:
    return num / den * 100 if den else 0.0


def div(num: float, den: float) -> float:
    return num / den if den else 0.0


def derive_metrics(
    imp: int, clk: int, imp_users: int, views: int, carts: int, orders: int, gmv: float
) -> dict[str, float]:
    """CTR=clk/imp, 퍼널 view→cart→order, CVR=주문/VIEW, RPM=GMV/imp×1000, AOV=GMV/주문."""
    return {
        "imp": float(imp),
        "clk": float(clk),
        "imp_users": float(imp_users),
        "ctr": pct(clk, imp),
        "views": float(views),
        "carts": float(carts),
        "orders": float(orders),
        "cvr_pct": pct(orders, views),
        "gmv": float(gmv),
        "rpm": div(gmv, imp) * 1000,
        "aov": div(gmv, orders),
    }


# =========== 조회 ===========


def default_end(conn: psycopg.Connection) -> str:
    """집계 상한 기본값 — 데이터 최신 다음날. now() 를 쓰면 고정 데이터인데 결과가 흔들린다."""
    return conn.execute(DEFAULT_END_SQL).fetchone()[0].isoformat()


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
        orders=n_orders,
        gmv=float(gmv),
    )


def fetch_daily(
    conn: psycopg.Connection, start: str, end: str
) -> list[tuple[Any, int, int, float]]:
    """(day, views, orders, gmv) — 두 원천을 날짜로 합친다. 주문 없는 날도 views 로 남는다."""
    params = {"start": start, "end": end}
    views = dict(conn.execute(DAILY_VIEWS_SQL, params).fetchall())
    orders = {day: (n, gmv) for day, n, gmv in conn.execute(DAILY_ORDERS_SQL, params).fetchall()}
    days = sorted(set(views) | set(orders))
    return [
        (day, views.get(day, 0), orders.get(day, (0, 0))[0], float(orders.get(day, (0, 0))[1]))
        for day in days
    ]


# =========== 창 분할 ===========


def _shift(day: str, days: int) -> str:
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=days)).isoformat()


def build_periods(
    end: str, days: int, before_end: str | None, after_start: str | None
) -> list[tuple[str, str, str]]:
    """(label, start, end)[]. cutover 경계가 없으면 최근 창 단일, 있으면 before/after.

    - baseline: `[end-days, end)`
    - cutover: `before=[before_end-days, before_end)`,
      `after=[after_start, min(after_start+days, end))`

    after 가 성숙하면(≥days) 경계 양옆 **동일 길이로 잘라 정합**한다 — `/일` 환산은 누적값의
    스케일만 고치고 요일 구성·계절성은 못 고치므로, 데이터가 있으면 길이정합이 엄격히 낫다.
    아직 짧으면 비대칭을 유지한다(before 의 정밀도를 버리고 짧은 창끼리 비교하는 게 더 나쁘다)
    → 그 구간은 Δ% 가 아니라 일별 분포 순위로 읽는다.

    `before_end != after_start` 면 그 사이 하루가 washout 으로 양쪽에서 빠진다.
    """
    if before_end is None or after_start is None:
        return [("recent", _shift(end, -days), end)]
    if end <= after_start:
        raise ValueError(
            f"after 윈도우가 비었다 — end({end}) 가 after 시작({after_start})보다 뒤여야 한다"
        )
    return [
        (f"before (~{before_end})", _shift(before_end, -days), before_end),
        (f"after ({after_start}~)", after_start, min(_shift(after_start, days), end)),
    ]


def window_days(periods: list[tuple[str, str, str]]) -> list[int]:
    """기간별 창 길이(일) — 길이정합 여부 판정과 `/일` 환산의 분모."""
    return [
        (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
        for _, start, end in periods
    ]


def window_note(days: list[int], end: str) -> str:
    """창 길이 정합/비대칭에 따라 판정 방법을 다르게 안내한다 — 세 지면 공통 규율."""
    if len(days) == 1:
        return ""
    if len(set(days)) == 1:
        n = days[0]
        note = (
            f"> 창 길이정합: 경계 양옆 {n}d 로 잘랐다(`--days`). after 데이터가 {end} 까지 더"
            " 있어도 비대칭을 `/일` 로 흡수하면 스케일만 맞고 요일 구성·계절성은 안 맞는다 →"
            " 성숙했으면 정합이 낫다. 효과 지속은 창을 늘리지 말고 `--daily` 추이로 본다.\n"
        )
        if n % 7:
            note += (
                f"> ⚠️ {n}d 는 7의 배수가 아니라 before/after 요일 구성이 다르다 → 28·49d 권장.\n"
            )
        return note
    return (
        f"> ⚠️ after({days[1]}d)가 before({days[0]}d)보다 짧다 = **비대칭**. 누적은 `/일` 로"
        " 환산했지만 요일 구성·계절성은 정합되지 않았다. 판정은 Δ% 가 아니라 before 일별 분포 내"
        " 순위로 하고, after 가 성숙하면 자동 정합된다.\n"
    )


# =========== 렌더 ===========


def per_day(metrics: dict[str, float], days: int) -> dict[str, float]:
    """누적 지표만 일평균 환산 — 비율 지표와 distinct 유저는 원값 그대로."""
    return {
        key: div(value, days) if key in CUMULATIVE_KEYS else value for key, value in metrics.items()
    }


def matrix_cell(key: str, fmt: str | None, metrics: dict[str, float]) -> str:
    """행렬 셀 렌더 — fmt=None 은 그 지면에서 측정 불가한 열(`-`)."""
    return "-" if fmt is None else fmt.format(metrics[key])


def render_matrix(labels: list[str], by_period: list[dict[str, float]], days: list[int]) -> str:
    """행=기간 × 열=지표 고정폭 표. 2기간이면 Δ% 행 자동 추가. pandas 없이(미러 관습)."""
    scaled = [per_day(m, n) for m, n in zip(by_period, days, strict=True)]
    rows = [[matrix_cell(key, fmt, m) for _, key, fmt in MATRIX_COLS] for m in scaled]
    if len(scaled) == 2:
        before, after = scaled
        rows.append(
            [
                "—"
                if fmt is None or not before[key]
                else f"{(after[key] - before[key]) / before[key] * 100:+.1f}"
                for _, key, fmt in MATRIX_COLS
            ]
        )
        labels = [*labels, "Δ%"]
    header = ["period", *[h for h, _, _ in MATRIX_COLS]]
    table = [header, *[[label, *row] for label, row in zip(labels, rows, strict=True)]]
    widths = [max(len(cell) for cell in col) for col in zip(*table, strict=True)]
    return "\n".join(
        "  ".join(cell.rjust(width) for cell, width in zip(line, widths, strict=True))
        for line in table
    )


def sample_line(labels: list[str], by_period: list[dict[str, float]], days: list[int]) -> str:
    parts = []
    for label, metrics, n in zip(labels, by_period, days, strict=True):
        cells = " · ".join(f"{h} {fmt.format(metrics[key])}" for h, key, fmt in SAMPLE_COLS)
        parts.append(f"{label.split(' (')[0]} {n}d: {cells}")
    return "> 표본(누적 원값) — " + " / ".join(parts)


def daily_table(rows: list[tuple[Any, int, int, float]]) -> str:
    """일별 표 — 소표본 진동·말단 절단(right-censoring)을 눈으로 확인하는 용도."""
    lines = [f"{'day':>10}  {'VIEW':>8}  {'주문':>6}  {'GMV':>12}"]
    lines += [
        f"{str(day):>10}  {views:>8,}  {orders:>6,}  {f'₩{gmv:,.0f}':>12}"
        for day, views, orders, gmv in rows
    ]
    return "\n".join(lines)


def washout_line(conn: psycopg.Connection, washout_day: str) -> str:
    """제외한 전환일의 실측 — 버리는 하루를 안 보이게 감추지 않기 위한 참고 라인."""
    metrics = fetch_window(conn, washout_day, _shift(washout_day, 1))
    cells = " · ".join(f"{h} {fmt.format(metrics[key])}" for h, key, fmt in SAMPLE_COLS)
    return f"> 전환일 {washout_day}(washout, 양쪽 제외 — 두 알고리즘 혼재) — {cells}"


def purchase_sample_caveat(by_period: list[dict[str, float]]) -> str | None:
    """주문 표본이 임계 미달이면 캐비엇 문자열, 충분하면 None.

    정밀도는 **작은 쪽** 창에 묶인다 — after 만 커도 판정이 안 되므로 min 을 쓴다.
    """
    orders = min(m["orders"] for m in by_period)
    if orders >= HEAVY_TAIL_PURCHASE_FLOOR:
        return None
    return (
        f"> 🚨 **주문 표본(작은 쪽 창) {orders:,.0f}건 < {HEAVY_TAIL_PURCHASE_FLOOR} → 주문 기반"
        " 지표(주문/CVR/GMV/RPM/AOV)의 평균 Δ% 는 판정 근거가 못 된다.** 커머스 주문은"
        " heavy-tail 이라 창을 어디서 자르냐로 CVR 이 크게 뒤집힌다."
        "\n>    **판정은 `--daily` 로 일별을 뽑아 중앙값과 순위 검정(Mann-Whitney U)으로 하라** —"
        " 표본이 작아도 정규성 가정 없이 방향을 확정할 수 있다. CTR·VIEW/imp 는 표본이 커서"
        " 이 제약과 무관하다.\n"
    )


# =========== 실행 ===========


def resolve_cutover(
    surface: Surface, deploy_date: str | None, baseline: bool
) -> tuple[str | None, str | None]:
    """지면 설정과 인자에서 (before_end, after_start) 를 정한다 — 둘 다 None 이면 baseline.

    전환일을 고정해 둔 지면(popular)은 그 하루를 washout 으로 양쪽에서 빼고, 인자로 받는
    지면(related·personalized)은 배포일을 경계로 삼는다. 후자는 배포 당일이 after 첫날이 되므로
    그 하루가 오염돼 있다면 **배포 다음날**을 주어야 한다.
    """
    if baseline:
        return None, None
    if surface.washout_day:
        return surface.washout_day, _shift(surface.washout_day, 1)
    if deploy_date:
        return deploy_date, deploy_date
    return None, None


def report(
    surface_key: str,
    end: str | None,
    days: int,
    deploy_date: str | None,
    baseline: bool,
    daily: bool,
) -> None:
    surface = SURFACES[surface_key]

    with psycopg.connect(settings.pg_conninfo()) as conn:
        end = end or default_end(conn)
        before_end, after_start = resolve_cutover(surface, deploy_date, baseline)
        if after_start and end <= after_start:
            print(f"> after 윈도우가 아직 비었다(end={end} ≤ {after_start}) → baseline 으로 표시\n")
            before_end = after_start = None
        periods = build_periods(end, days, before_end, after_start)
        by_period = [fetch_window(conn, start, stop) for _, start, stop in periods]
        show_washout = bool(surface.washout_day and before_end)
        washout = washout_line(conn, surface.washout_day) if show_washout else ""
        daily_rows = fetch_daily(conn, periods[0][1], periods[-1][2]) if daily else []

    labels = [label for label, _, _ in periods]
    spans = window_days(periods)
    mode = (
        "baseline (최근 창)"
        if len(periods) == 1
        else f"cutover 비교 ({'길이정합' if len(set(spans)) == 1 else '비대칭→/일 환산'})"
    )
    span_text = " · ".join(
        f"{label.split(' (')[0]}={n}d" for label, n in zip(labels, spans, strict=True)
    )

    print(f"# 쇼핑 KPI — {surface.title} (로컬)  · {mode} · end={end}, {span_text}\n")
    print(window_note(spans, end), end="")
    print(render_matrix(labels, by_period, spans))
    print("\n" + sample_line(labels, by_period, spans))
    if washout:
        print(washout)
    if daily:
        print(f"\n일별 [{periods[0][1]}, {periods[-1][2]}) (소표본 진동 확인)\n")
        print(daily_table(daily_rows))

    print(
        "\n> `/일`=창 길이가 다를 수 있어 누적을 일평균 환산한 값. 비율(CTR/CVR/RPM/AOV)은 길이"
        " 무관 → 원값. 누적 원값은 표본 라인. CTR=clk/imp, CVR=주문/VIEW, GMV=주문수×현재가."
    )
    for note in surface.notes:
        print(f"> {note}")
    print(
        "> 로컬 데이터에는 지면 귀속이 없어 세 지면이 같은 모집단을 본다 — 숫자가 비슷한 것이"
        " 정상이고 재현 대상은 창 규율이다. 합성 이벤트는 추천 노출과 무관해 before/after"
        " 무차이가 정상."
    )
    if (caveat := purchase_sample_caveat(by_period)) is not None:
        print(caveat, end="")
    if before_end and before_end == after_start:
        print(
            f"> ⚠️ 배포 당일({before_end})이 after 첫날이다. 그날 낮에 교체됐거나 앞단 캐시가"
            " 있었다면 그 하루는 두 알고리즘 혼재 = 오염 → `--deploy-date` 를 **배포 다음날**로"
            " 주어 washout 하라(popular 처럼 전환일이 고정된 지면은 양쪽에서 빠진다)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="쇼핑 지면 KPI (로컬 미러)")
    parser.add_argument("surface", choices=sorted(SURFACES), help="측정할 지면")
    parser.add_argument("--end", default=None, help="집계 종료(exclusive). 기본=데이터 최신 다음날")
    parser.add_argument("--days", type=int, default=30, help="기간당 윈도우 길이(일). 기본 30")
    parser.add_argument(
        "--deploy-date", default=None, help="cutover 기준일(전환일이 고정된 지면은 무시)"
    )
    parser.add_argument("--baseline", action="store_true", help="cutover 비교 대신 최근 창 하나")
    parser.add_argument("--daily", action="store_true", help="일별 표 추가")
    args = parser.parse_args()
    report(args.surface, args.end, args.days, args.deploy_date, args.baseline, args.daily)


if __name__ == "__main__":
    main()
