from enum import StrEnum


class Interval(StrEnum):
    """popular 지면의 집계 관점 — 값은 query param 계약 (day/week/month)."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"

