# 관찰성 — RED 메트릭 + 구조화 로그

관찰성은 두 축으로 나눈다: **집계(metrics)는 Prometheus**, **개별 사건(logs)은 Loki**.
"얼마나 자주/빠른가"는 메트릭이 싸고, "그 요청이 무엇이었나"는 로그가 답한다.

```
요청 → [CorrelationId → access_log 미들웨어 → Instrumentator → 라우터]
         request_id 발급   JSON 로그 1줄        RED 카운트/히스토그램
                             (Loki)              (/metrics → Prometheus)
```

## RED 메트릭 (Prometheus)

RED = Rate · Errors · Duration — 요청 구동 서비스의 표준 3지표. `prometheus-fastapi-instrumentator`가
매 요청을 측정해 `/metrics`로 노출하고, Prometheus가 주기적으로 당겨간다(pull).

| 지표 | 메트릭 | PromQL 예 |
|---|---|---|
| Rate | `http_requests_total{handler,method,status}` | `sum(rate(http_requests_total[5m])) by (handler)` |
| Errors | 같은 카운터의 status 라벨 | `rate(http_requests_total{status="5xx"}[5m])` |
| Duration | `http_request_duration_seconds_bucket{handler,method}` | `histogram_quantile(0.99, sum(rate(...bucket[5m])) by (le, handler))` |

- **handler 라벨은 라우트 템플릿**(`/api/v1/products/{product_seq}/related`)이지 concrete URL이 아니다 —
  상품 seq마다 시계열이 폭발하는 카디널리티 사고를 막는다.
- `/health`·`/ready`·`/metrics`는 instrument에서 제외 — probe 트래픽이 지표를 오염시키지 않게.
- 히스토그램 버킷은 현 지연 대역(수십~수백 ms)을 촘촘히 나눠 p99 보간 오차를 줄였다.

## 구조화 access 로그 (Loki)

미들웨어가 요청당 **canonical 한 줄**(JSON)을 stdout에 emit한다. Loki는 stdout을 수집하므로
앱은 "잘 구조화된 한 줄"만 책임지면 된다.

- **flat JSON + HTTP 필드는 OTel 관례명**(`http.request.method`, `http.route`,
  `http.response.status_code`, `duration_ms`): Loki `| json` 파싱과 Grafana 표준 조회에
  바로 맞는다. 서비스 식별은 `service`/`version`/`env` 필드로 붙는다.
- **request_id**: `CorrelationIdMiddleware`가 요청마다 발급하고 contextvars로 그 요청의 모든
  로그 라인에 자동 주입 → 한 요청의 로그를 request_id로 묶어 추적.
- **`reco.result_count`**: 라우터가 결과 개수를 `request.state`에 세팅하면 access 라인의 필드로
  승격된다. 이걸로 **빈 추천율**을 LogQL로 잰다:
  `count_over_time({service="reco"} | json | reco_result_count="0" [5m])`.
  HTTP는 200인데 추천이 비어 나가는 "조용한 실패"는 RED로는 안 잡히고 이 필드로만 보인다.
- 실패 요청은 쿼리(product_seq/category_seq)까지 남겨, 어떤 입력이 터졌는지 로그만으로 재현.

## 왜 둘 다 필요한가 — 메트릭과 로그의 분업

- RED만 보면 "p99가 튀었다"는 알지만 **어떤 요청**인지 모른다.
- 로그만 보면 개별 사건은 알지만 "지금 에러율이 정상인가"를 빠르게 못 잰다.
- 그래서 메트릭으로 이상을 감지(alert)하고, request_id로 로그를 파고들어 원인을 찾는다.
- reco 고유의 "빈 추천율"은 HTTP 지표(RED) 바깥이라 로그 필드로 보강했다 — 서비스마다
  "성공인데 쓸모없는 응답"의 정의가 다르고, 그건 도메인만 안다.

## 로컬에서 보기

```bash
# JSON 로그(운영 포맷)로 띄우려면 env=dev
env=dev uv run uvicorn app.main:app
curl localhost:8000/metrics          # RED 원본
```

Prometheus/Loki 자체는 gitops 단계(모니터링 스택)에서 붙인다 — 이 repo는 "지표를 내는 쪽"만 책임진다.
