# GCP — External Application Load Balancer + Cloud Armor

## 권장 경로

```text
Internet → External Application Load Balancer → Cloud Armor policy → private backend
```

1. backend service에 Cloud Armor security policy를 연결한다.
2. preconfigured WAF rule은 preview mode로 시작해 오탐을 확인한다.
3. 로그인·가입·비밀번호 찾기 경로에는 IP 기준 `throttle` 또는 `rate_based_ban` 규칙을 둔다.
4. 일반 API에는 별도 상한을 둔다.
5. load balancer와 health check 외 경로에서 backend에 직접 접근하지 못하게 방화벽/IAM을 제한한다.

Cloud Armor는 `throttle`과 `rate_based_ban`을 제공하며, preview 로그로 적용 효과를 먼저 볼 수 있다.
한도는 연결된 backend/리전 단위로 집계될 수 있으므로 멀티 리전 전체 트래픽을 단일 전역 한도로
착각하지 않는다. 자세한 동작은 [Cloud Armor rate limiting overview](https://docs.cloud.google.com/armor/docs/rate-limiting-overview)를
기준으로 확인한다.

## 시작값 예시

| 범위 | 시작값 | 동작 |
|---|---:|---|
| 로그인 | IP당 60초 8회 | `throttle`, 반복 초과 시 ban |
| 가입·비밀번호 찾기 | IP당 300초 10회 | 429 |
| 일반 API | IP당 60초 300회 | 429 |

공유 NAT가 많은 서비스라면 IP만 쓰지 말고 경로, 지역, reCAPTCHA assessment 또는 인증 후 사용자
정책을 조합한다.

## 앱 설정

- 최종 HTTPS 도메인과 `AUTH_PUBLIC_BASE_URL`을 일치시킨다.
- `AUTH_COOKIE_SECURE=true`.
- 전달 IP를 신뢰하려면 backend 직접 접근을 차단하고 LB가 덮어쓴 헤더만 받아야 한다.
- Cloud Logging의 정책 preview/deny와 앱의 `auth_audit_logs`를 함께 모니터링한다.
