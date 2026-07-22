"""응답 스키마 + ranked row → 계약 매퍼.

응답은 게이트웨이 직수신 envelope `{code, message, result}` — 게이트웨이는
HTTP 2xx AND code==200 을 성공 게이트로 쓴다. result 는 product_seq 리스트
(순서 = 랭킹). 표시 메타(name/price)·score 는 소비자가 쓰지 않으므로 싣지 않는다.
"""

from typing import Any

from pydantic import BaseModel


class RelatedResponse(BaseModel):
    code: int = 200
    message: str = "success"
    result: list[int]


class PersonalizedResponse(BaseModel):
    code: int = 200
    message: str = "success"
    result: list[int]


class PopularResponse(BaseModel):
    code: int = 200
    message: str = "success"
    result: list[int]


def to_result(rows: list[dict[str, Any]]) -> list[int]:
    """ranked row(dict) → product_seq 리스트 (입력 순서 보존)."""
    return [row["product_seq"] for row in rows]
