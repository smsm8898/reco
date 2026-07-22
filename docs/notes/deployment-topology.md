# 배포 토폴로지 — 플래그로 나누는 modular monolith

## 무엇을

세 구좌(연관·개인화·인기) 라우터를 `ENABLE_*` 플래그로 **조건부 마운트**한다. 같은 이미지
하나를, 배포마다 다른 플래그로 띄워 **지면별 서비스**로 나눈다.

```python
# app/main.py
if settings.ENABLE_PERSONALIZED:
    app.include_router(personalized_router)
if settings.ENABLE_POPULAR:
    app.include_router(popular_router)
if settings.ENABLE_RELATED:
    app.include_router(related_router)
```

```
같은 이미지(reco:tag)
├─ Deployment: reco-related       ENABLE_RELATED=true      → /related 만 서빙, HPA 독립
├─ Deployment: reco-personalized  ENABLE_PERSONALIZED=true → /personalized 만
└─ Deployment: reco-popular       ENABLE_POPULAR=true      → /popular 만
```

기본값은 **전부 false** — 각 Deployment가 자기 지면을 **명시적으로 opt-in**한다. 실수로 한
pod이 모든 지면을 떠안는 일이 없고, 배포 매니페스트만 봐도 그 pod이 무엇을 서빙하는지 드러난다.
(로컬은 `.env`로 셋 다 켜 모놀리스로 실행.)

## 왜 이게 "MSA"이고, 왜 full MSA는 아닌가

이건 **배포 가능한 modular monolith**다 — scale-cube의 Y축(기능) 분할을 *하나의 아티팩트*로
한다. 정통 microservices와의 차이:

| | 이 방식 (flag 분할) | 정통 MSA |
|---|---|---|
| 코드/이미지 | 하나 | 서비스마다 별도 |
| DB | 하나 | 서비스마다 별도(소유) |
| 통신 | 함수 호출 | 네트워크(HTTP/gRPC/이벤트) |
| 얻는 것 | **독립 스케일링·배포 격리** | + 독립 릴리스·기술 스택·장애 격리 |
| 치르는 비용 | 거의 없음 | 네트워크 지연·분산 트랜잭션·서비스 디스커버리·계약 버저닝 |

## 왜 이 규모에 full MSA를 안 하나 (의도적 선택)

작은 앱을 별도 DB·네트워크 호출로 쪼개는 건 유명한 안티패턴이다 — "분산 모놀리스"가 되어
MSA의 비용은 다 내고 이득은 못 얻는다. microservices premium(네트워크·분산 트랜잭션·분산
관찰성·운영 오버헤드)은 **조직이 커서 팀별 독립 릴리스가 필요할 때** 내는 비용이지, 기술적
규모만으로 낼 것이 아니다.

그래서 이 repo는 flag 분할까지만 한다: **독립 스케일링(연관 트래픽 급증이 개인화 pod을 안
굶김)** 과 배포 격리는 얻되, DB·코드는 하나로 둔다. 진짜로 도메인·팀이 갈릴 때 바운디드
컨텍스트(catalog / ordering / recommendation) 기준으로 서비스를 쪼개고 각자 DB를 소유하면
된다 — 그건 이 규모에선 과하다.

## 역할 분담

- **reco (이 repo)**: 플래그 + 조건부 마운트라는 *이음매*와 부팅 로그의 플래그 스냅샷.
- **gitops (상위 3단계)**: 같은 이미지를 지면별 Deployment로 띄우는 Helm — 지면마다 하나만
  켠 env, 지면별 HPA·리소스. "지표 내는 쪽"이 reco, "토폴로지 잡는 쪽"이 gitops.
