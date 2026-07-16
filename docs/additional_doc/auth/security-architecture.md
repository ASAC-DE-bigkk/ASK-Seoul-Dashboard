# 인증·인가·회원 보안 설계

## 적용 범위

- 익명 사용자는 `/`, `/health`, 로그인·가입·비밀번호 재설정, `/api/v1/public/summary`만 접근한다.
- `/catalog`, `/charts`, `/profile`, `/admin`, `/docs`와 대응 API는 전역 미들웨어가 매 요청마다
  세션과 페이지 권한을 검사한다.
- 역할 기본 권한과 사용자 override를 분리한다. 사용자 `allow/deny`가 역할 기본값보다 우선한다.
- 운영자는 게스트·일반회원만 관리하고, 최고관리자는 모든 회원·역할·페이지·시스템 정책을 관리한다.

| 역할 | 기본 접근 | 관리 범위 |
|---|---|---|
| 게스트 | 카탈로그, 프로필, 이용권 | 본인 |
| 일반회원 | 게스트 + Charts Studio | 본인 |
| 운영자 | 카탈로그·차트·API 문서·운영 콘솔 | 게스트·일반회원 |
| 최고관리자 | 전체 | 전체(자기 역할/상태 변경은 별도 차단) |

권한은 UI 숨김만으로 구현하지 않는다. 서버는 기본 거부(deny by default)와 매 요청 권한 검사를
수행한다. 이는 [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)의
권고와 같은 방향이다.

## 계정 생명주기

1. 가입 시 `guest + pending`으로 생성하고 중복되지 않는 패턴 닉네임을 부여한다.
2. SMTP가 설정되면 1회용 이메일 인증 토큰을 보낸다.
3. `AUTH_AUTO_APPROVE_VERIFIED=false`면 이메일 인증 후에도 운영자/최고관리자 승인이 필요하다.
4. SMTP가 없으면 관리자 화면의 승인 대기 목록에서 직접 승인한다.
5. 정지·거절 계정 및 만료 세션은 즉시 인증에서 제외된다.
6. 역할 또는 계정 상태가 바뀌면 기존 세션을 전부 폐기해 새 권한으로 다시 로그인하게 한다.

## 비밀번호와 재설정

- 비밀번호는 원문/복호화 가능한 형태로 저장하지 않고 Argon2id
  (`64 MiB, time_cost=3, parallelism=1`)로 해시한다.
- 단일 요소 로그인 기준 15~128자, 흔한 비밀번호 blocklist, 이메일 아이디/닉네임 포함 금지를 적용한다.
- 문자 종류 조합 규칙과 주기적 강제 변경은 적용하지 않는다.
- 로그인 5회 실패 시 15분 잠금과 endpoint rate limit을 함께 적용한다.
- 재설정 토큰은 CSPRNG로 생성하고 DB에는 pepper 기반 HMAC 해시만 저장한다. 1회 사용·기본 30분 만료다.
- 존재/비존재 계정에 동일 문구를 반환하고, SMTP 전송은 응답 이후 background task로 넘긴다.

비밀번호 정책은 [NIST SP 800-63B](https://pages.nist.gov/800-63-4/sp800-63b.html)와
[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
재설정 흐름은 [OWASP Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)를
기준으로 잡았다.

## 세션·CSRF·브라우저 방어

- 브라우저에는 랜덤 세션 원문을 `HttpOnly; SameSite=Lax; Path=/` 쿠키로만 저장한다.
- 운영 HTTPS에서는 `Secure`와 `__Host-` prefix를 사용한다.
- DB에는 세션 원문 대신 HMAC 해시, CSRF 해시, 만료·폐기 시각, IP/UA 해시만 저장한다.
- 기본으로 User-Agent를 세션에 결속한다. IP 결속은 모바일/프록시 환경의 오탐 때문에 선택 사항이다.
- 상태 변경 요청은 Fetch Metadata, Origin/Referer, double-submit CSRF 토큰을 함께 검증한다.
- CSP, HSTS(HTTPS), frame deny, MIME sniff 방지, 민감 페이지 `Cache-Control: no-store`를 적용한다.

세션 쿠키 기준은 [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
CSRF 기준은 [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)을
참고한다.

## 앱 내부 요청 제한

`auth_access_policies`의 `request_limit` 정책을 system → role → user 순서로 병합한다.

```json
{
  "anonymous": {"second": 8, "minute": 120, "hour": 2000, "day": 10000},
  "authenticated": {"second": 20, "minute": 600, "hour": 10000, "day": 50000},
  "login": {"minute": 5, "hour": 20},
  "register": {"hour": 5, "day": 15},
  "forgot_password": {"hour": 5, "day": 10},
  "action": "reject"
}
```

`action`은 `reject`, `slow_down`, `drop`을 지원한다. 이 카운터는 프로세스 메모리 기반이므로
다중 worker/인스턴스 전체 한도를 보장하지 않는다. 운영에서는 WAF를 정본으로 두고, 필요하면
추후 Redis 기반 공용 limiter로 교체한다.

## 운영 필수 설정

```dotenv
AUTH_ENV=production
AUTH_PUBLIC_BASE_URL=https://dashboard.example.com
AUTH_ALLOWED_HOSTS=dashboard.example.com
AUTH_COOKIE_SECURE=true
AUTH_SESSION_PEPPER=<secret-manager에서 주입>
AUTH_TRUST_PROXY_HEADERS=true
AUTH_BIND_SESSION_USER_AGENT=true
AUTH_BIND_SESSION_IP=false
```

- `AUTH_TRUST_PROXY_HEADERS=true`는 원본 서버가 승인된 LB/WAF에서만 접근 가능하고, 해당 장비가
  전달 헤더를 덮어쓴다는 것이 보장될 때만 켠다.
- 최고관리자 bootstrap 환경변수는 최초 생성 후 제거한다.
- 공개 운영 전 최고관리자·운영자 MFA는 필수 보강 항목이다. 현재 구현은 MFA를 포함하지 않는다.
- 내장 blocklist는 최소 방어선이다. 공개 운영 전 정기 갱신되는 유출 비밀번호 목록을 로컬/사설
  서비스로 확장하되 비밀번호 원문을 제3자에게 전송하지 않는다.
- 정적 CSP에 `unsafe-inline`이 남아 있는 이유는 기존 단일 HTML 화면의 inline script/style 때문이다.
  외부 공개 전 파일 분리와 nonce/hash 기반 CSP로 강화한다.
