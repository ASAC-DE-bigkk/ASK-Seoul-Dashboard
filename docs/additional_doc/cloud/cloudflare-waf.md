# Cloudflare — Proxy/WAF/Rate Limiting

## 권장 경로

```text
Internet → Cloudflare proxy/WAF → Tunnel 또는 잠긴 origin → app
```

1. DNS record를 proxied로 전환하고 WAF managed rules를 적용한다.
2. 로그인·가입·비밀번호 찾기·이메일 인증·MFA, Charts query, 일반 API를 서로 다른 rate
   limiting rule로 분리한다.
3. 처음에는 Log/Managed Challenge로 오탐을 확인한 뒤 Block을 적용한다.
4. origin은 Cloudflare Tunnel을 사용하거나 Cloudflare IP/인증된 origin pull만 허용해 우회를 막는다.
5. 봇/credential stuffing이 보이면 Bot Management/Turnstile을 로그인·가입 흐름에 단계적으로 붙인다.

Cloudflare rate limiting rule은 표현식에 맞는 요청 수와 기간, 초과 시 action을 정의한다.
기능 가용 범위는 플랜에 따라 다르다. 공식 기준은
[Cloudflare rate limiting rules](https://developers.cloudflare.com/waf/rate-limiting-rules/)와
[best practices](https://developers.cloudflare.com/waf/rate-limiting-rules/best-practices/)를 확인한다.

## 시작값 예시

| 범위 | 시작값 | 동작 |
|---|---:|---|
| 로그인 | IP당 1분 8회 | Managed Challenge 또는 Block |
| 가입·비밀번호 찾기 | IP당 5분 10회 | Managed Challenge |
| 이메일 인증·MFA | IP당 5분 30회 | Managed Challenge |
| Charts query | 사용자/IP당 1분 30회 | Block |
| 일반 API | IP당 1분 300회 | Block |

## 앱 설정

- `AUTH_PUBLIC_BASE_URL=https://최종도메인`, `AUTH_COOKIE_SECURE=true`.
- `CF-Connecting-IP` 또는 전달 IP는 origin 직접 접근이 차단되고 Cloudflare만 도달할 때만 신뢰한다.
  현재 앱은 표준 `X-Forwarded-For` 첫 항목을 선택적으로 사용하므로, 앞단 프록시가 이 값을
  안전하게 정규화하도록 구성한다.
- WAF 차단 이벤트와 앱 429/로그인 실패/잠금 이벤트를 같은 대시보드에서 상관 분석한다.
