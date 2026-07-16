# AWS — CloudFront/ALB + AWS WAF

앱의 인메모리 limiter보다 앞에서 공격 트래픽을 끊는 구성이다.

## 권장 경로

```text
Internet → CloudFront(optional) → AWS WAF Web ACL → ALB → private app targets
```

1. Web ACL을 CloudFront 또는 ALB에 연결한다.
2. AWS Managed Rules의 common, known bad inputs, IP reputation 계열을 먼저 `Count`로 적용한다.
3. `/api/v1/auth/login`, `/register`, `/forgot-password`에 낮은 rate-based rule을 별도로 둔다.
4. `/api/v1/*` 전체에는 더 높은 일반 API 한도를 둔다.
5. 3~7일 로그를 보고 정상 NAT/기업망을 확인한 후 `Block` 또는 CAPTCHA/challenge로 전환한다.
6. ALB security group은 CloudFront를 쓰면 CloudFront origin-facing prefix list/승인 경로만,
   직접 ALB면 필요한 공개 경로만 허용한다. 앱 인스턴스는 ALB security group에서만 받는다.

AWS WAF rate-based rule은 조건에 맞는 요청을 집계하고 evaluation window/limit/action으로 제한한다.
공식 동작은 [AWS WAF rate-based rules](https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-statement-type-rate-based.html)를
기준으로 확인한다.

## 시작값 예시

| 범위 | 시작값 | 동작 |
|---|---:|---|
| 로그인 | IP당 5분 30회 | 초기는 Count, 이후 Block/CAPTCHA |
| 가입·비밀번호 찾기 | IP당 5분 15회 | Block |
| 일반 API | IP당 5분 1,500회 | Block 또는 challenge |
| 알려진 악성 IP | managed IP reputation | Block |

정답값이 아니라 초기값이다. 공유 NAT, 모니터링, 내부 운영 봇을 로그에서 분리해 조정한다.

## 앱 설정

- `AUTH_PUBLIC_BASE_URL`은 최종 HTTPS 도메인으로 둔다.
- `AUTH_COOKIE_SECURE=true`.
- proxy header를 사용할 때는 외부에서 앱/target에 직접 접근할 수 없게 하고 ALB/CloudFront가
  `X-Forwarded-For`를 통제하는 경우에만 `AUTH_TRUST_PROXY_HEADERS=true`.
- WAF 로그는 Firehose/S3 또는 CloudWatch로 보내고 429/403, 로그인 실패, 계정 잠금 감사 로그를
  같은 시간축에서 조회한다.
