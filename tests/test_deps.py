"""요청 공통 유틸 — limit clamp 와 access 로그용 결과 수 기록. DB 불필요."""

from types import SimpleNamespace

from app.deps import MAX_LIMIT, clamp_limit, record_count
from app.models.response import to_result


def test_limit_is_clamped_not_rejected() -> None:
    """범위 밖 limit은 에러 대신 clamp — 잘못된 값에도 지면은 뜬다."""
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1
    assert clamp_limit(MAX_LIMIT + 1) == MAX_LIMIT
    assert clamp_limit(10) == 10


def test_record_count_puts_result_count_on_request_state() -> None:
    """라우터가 세팅한 값을 access 로그가 reco.result_count 로 승격한다 — 빈응답률 측정의 재료."""
    request = SimpleNamespace(state=SimpleNamespace())

    returned = record_count(request, [1, 2, 3])

    assert returned == [1, 2, 3]  # 응답 조립 한 줄에 끼워 쓰도록 그대로 돌려준다
    assert request.state.reco_result_count == 3


def test_to_result_extracts_product_seq_preserving_order() -> None:
    rows = [{"product_seq": 9, "rrf_score": 0.1}, {"product_seq": 4, "rrf_score": 0.2}]

    assert to_result(rows) == [9, 4]
