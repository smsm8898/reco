from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    result: list[int]

def to_result(rows: list[dict[str, Any]]) -> list[int]:
    """ranked row(dict) → product_seq 리스트 (입력 순서 보존)."""
    return [row["product_seq"] for row in rows]
