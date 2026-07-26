# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 의존성 먼저 (레이어 캐시 — 코드만 바뀌어도 dep 재설치 안 함)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache --no-dev

# 서빙 코드만 복사 (batch scripts·tests·docs 는 이미지에 넣지 않는다)
COPY app/ ./app/

ENV PATH="/app/.venv/bin:$PATH" \
    UV_NO_SYNC=1

EXPOSE 8000

# 단독 `docker run` 으로도 뜨도록 기본 CMD 를 둔다 (gitops 단계에서 helm 이 덮어씀).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
