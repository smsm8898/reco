# popular methodology bakeoff

- cutoff T: 2026-03-25 00:17:31.605757, 평가창 7일, K=20, 카테고리 매크로 평균
- hn 계열 dial: gravity=1.0, bucket=24h (서빙 week)

> 한계: 합성 인기도가 정적이라 감쇠 계열의 우위가 안 드러난다 — 형식이 목적.

| methodology | capture@20 | recall@20 |
|---|---|---|
| count | 0.8792 | 0.1220 |
| hn_decay | 0.8833 | 0.1220 |
| exp_decay | 0.8833 | 0.1220 |
| funnel | 0.8250 | 0.1220 |
| funnel_hn | 0.8375 | 0.1220 |
