# Reco

커머스 플랫폼을 위한 추천 서빙 API.

## 구조

- 이 repo는 **서빙**(realtime) 담당 — 요청을 받아 추천 결과(product_seq 리스트)를 반환한다.
- 추천 재료(mart·CF)를 만드는 **batch**는 별도 파이프라인의 몫이다 (전체 시스템에선 airflow).
  여기 `scripts/`에는 로컬 실행용 틀(generate_data·build_mart·train_cf)만 둔다 —
  로컬에서 데이터를 채워 API를 띄워보려면 그걸 쓰면 된다.

## 실행

```bash
uv sync
docker compose up -d                    # PostgreSQL
uv run uvicorn app.main:app --reload

curl http://localhost:8000/health       # liveness
curl http://localhost:8000/ready        # readiness (PG 연결 확인)
```

설정은 환경변수 또는 `.env`로 덮어쓸 수 있다 (`.env.example` 참고). 
기본값이 docker-compose 로컬 PostgreSQL과 일치한다.

## 엔드포인트

| 엔드포인트 | 설명 | 상태 |
|---|---|---|
| `GET /health` | liveness | ✅ |
| `GET /ready` | readiness — PG 연결 확인 | ✅ |
| `GET /metrics` | Prometheus RED 메트릭 | ✅ |
| `GET /api/v1/products/{id}/related` | 관련 상품 추천 — view·cart·order·카테고리 인기 신호를 RRF로 융합 | ✅ |
| `GET /api/v1/products/personalized` | 개인화 추천 — ALS CF 융합, 비로그인은 인기도 콜드스타트 | ✅ |
| `GET /api/v1/products/popular` | 카테고리 인기 — interval dial(day/week/month)별 HN 감쇠 + RRF (`category_seq` 필수) | ✅ |

응답은 `{code, message, result}` envelope이고 `result`는 product_seq 리스트(순서 = 랭킹)다.
존재하지 않는 상품은 404, 범위 밖 `limit`은 거절 대신 자동 보정(1~50).

구좌별 라우터는 `ENABLE_RELATED`/`ENABLE_PERSONALIZED`/`ENABLE_POPULAR` 플래그로 조건부
마운트된다 — 같은 이미지를 지면별 Deployment로 나눠 독립 스케일링하기 위한 이음매
(기본 전부 off, 배포마다 opt-in). 로컬은 `.env`로 셋 다 켠다.

설계 노트: [serving](docs/notes/serving.md) · [observability](docs/notes/observability.md) ·
[deployment-topology](docs/notes/deployment-topology.md)

## 오프라인 실험 (scripts/experiments/)

dial(gravity 등)은 배포 전 오프라인 gate로 고른다 — 실험 스크립트는 서빙 함수를
import해서 sweep하므로(중복 구현 없음) 실험 결과가 곧 서빙 코드 검증이다.

- `uv run python -m scripts.experiments.popular_hn_gate` — gravity sweep (capture@K/recall@K)

결과는 `scripts/experiments/results/*.md`. 합성 데이터의 인기도는 정적이라 수치 차이는
거의 없다 — 목적은 하네스의 형식(point-in-time 분할, 서빙 함수 재사용)이다.

## 테스트 · 린트

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
