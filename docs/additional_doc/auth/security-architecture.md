# 인증·인가·회원 보안 설계

## 적용 범위

- 익명 사용자는 `/`, `/health`, 로그인·가입·비밀번호 재설정, `/api/v1/public/summary`만 접근한다.
- `/catalog`, `/charts`, `/profile`, `/admin`, `/docs`와 대응 API는 전역 미들웨어가 매 요청마다
  세션과 페이지 권한을 검사한다.
- 역할 기본 권한과 사용자 override를 분리한다. 사용자 `allow/deny`가 역할 기본값보다 우선한다.
- 운영자는 게스트·일반회원만 관리하고, 최고관리자는 모든 회원·역할·페이지·시스템 정책을 관리한다.

| 역할 | 기본 접근 | Charts 레이아웃 | 관리 범위 |
|---|---|---|---|
| 게스트 | 카탈로그, 프로필, 이용권 | 기본 접근 없음. 별도 조회 허용을 받아도 쓰기 금지 | 본인 |
| 일반회원 | 게스트 + Charts Studio | 자신의 레이아웃 조회·추가·수정·삭제 | 본인 |
| 운영자 | 카탈로그·차트·API 문서·운영 콘솔 | 자신의 레이아웃 편집 | 게스트·일반회원 |
| 최고관리자 | 전체 | 자신의 레이아웃 편집 | 전체(자기 역할/상태 변경은 별도 차단) |

권한은 UI 숨김만으로 구현하지 않는다. 서버는 기본 거부(deny by default)와 매 요청 권한 검사를
수행한다. 이는 [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)의
권고와 같은 방향이다.

## 계정 생명주기

1. 이용약관·개인정보 처리 안내 동의 후 `guest + pending`으로 생성하고 중복되지 않는 패턴 닉네임을
   부여한다. 동의 시각과 약관 버전을 보존한다.
2. SMTP가 설정되면 1회용 이메일 인증 토큰을 보낸다. 인증 링크의 토큰은 URL fragment에 두어
   일반 access log와 Referer에 노출되지 않게 하고, 화면이 POST로 제출할 때만 소비한다.
3. 가입·인증 재발송·비밀번호 찾기는 계정 존재 여부와 관계없이 같은 문구를 반환해 계정 열거를 줄인다.
4. `AUTH_AUTO_APPROVE_VERIFIED=false`면 이메일 인증 후에도 운영자/최고관리자 승인이 필요하다.
5. SMTP가 없으면 관리자 화면의 승인 대기 목록에서 직접 승인한다.
6. 정지·거절 계정 및 만료 세션은 즉시 인증에서 제외된다.
7. 역할 또는 계정 상태가 바뀌면 기존 세션을 전부 폐기해 새 권한으로 다시 로그인하게 한다.
8. 최고관리자 수를 줄이는 전이는 전역 제어 행으로 직렬화하고 마지막 최고관리자 제거를 차단한다.

## 비밀번호와 재설정

- 비밀번호는 원문/복호화 가능한 형태로 저장하지 않고 Argon2id
  (`64 MiB, time_cost=3, parallelism=1`)로 해시한다.
- 단일 요소 로그인 기준 15~128자, 흔한 비밀번호 blocklist, 이메일 아이디/닉네임 포함 금지를 적용한다.
- 문자 종류 조합 규칙과 주기적 강제 변경은 적용하지 않는다.
- 로그인 5회 실패 시 15분 잠금과 endpoint rate limit을 함께 적용한다.
- 재설정 토큰은 CSPRNG로 생성하고 DB에는 pepper 기반 HMAC 해시만 저장한다. 1회 사용·기본 30분 만료다.
- 인증·재설정 토큰은 원자적 조건부 UPDATE로 소비해 동시 요청에서도 한 번만 성공한다.
- 존재/비존재 계정에 동일 문구를 반환하고, SMTP 전송은 응답 이후 background task로 넘긴다.

비밀번호 정책은 [NIST SP 800-63B](https://pages.nist.gov/800-63-4/sp800-63b.html)와
[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
재설정 흐름은 [OWASP Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)를
기준으로 잡았다.

## 이메일 전송 보안

- SMTP가 설정되지 않으면 이메일 인증·비밀번호 찾기는 관리자 승인/지원 절차로 안전하게 폴백한다.
- `SMTP_HOST`와 `SMTP_FROM_EMAIL`은 함께 설정해야 하며 boolean·port 오타는 기동 시 실패시킨다.
- production에서는 STARTTLS 또는 SMTP SSL이 기본 필수다. 네트워크와 인증이 별도로 보호된 내부
  relay가 평문 SMTP만 제공하는 예외에만 `SMTP_ALLOW_PLAINTEXT=true`를 명시한다.
- 전송 예외에는 서버 응답·계정·비밀번호를 싣지 않고, 사용자 응답에도 SMTP 자격증명을 노출하지 않는다.

## MFA

- RFC 6238 TOTP와 일회용 복구 코드를 지원한다.
- TOTP 원문 seed를 DB에 저장하지 않는다. 사용자 UUID와 랜덤 salt를
  `AUTH_MFA_MASTER_KEY`로 파생해 재생성하고, DB에는 salt·활성 시각·마지막 사용 counter만 저장한다.
- 이미 사용한 TOTP time counter는 다시 허용하지 않아 replay를 막는다.
- 로그인 challenge는 세션과 분리된 짧은 수명의 해시 토큰이며 IP/UA에 결속하고 최대 5회만 시도한다.
- 복구 코드는 HMAC 해시만 저장하며 한 번 사용하면 즉시 폐기된다. 새 코드 발급 시 이전 코드는 모두
  무효화한다.
- 운영자·최고관리자는 운영 환경에서 MFA가 강제되며 비활성 설정으로 기동할 수 없다. MFA가 없는 사용자는 해당 역할로
  승격할 수 없고, 기존 권한 계정의 비-MFA 세션도 인증 시 폐기된다.
- 게스트·일반회원이 인증 앱과 복구 코드를 모두 잃은 경우 최고관리자가 회원 관리 화면에서 본인 확인
  사유를 남기고 MFA·복구 코드·기존 세션을 함께 폐기할 수 있다.
- 운영자·최고관리자는 화면 초기화를 허용하지 않는다. 서버 운영자가 현재 비밀번호와 대화형 확인을 거친
  `.venv/bin/python scripts/setup_mfa.py --email <계정> --reset-existing` break-glass 재등록만 사용한다.
  새 TOTP가 먼저 검증되어야 기존 요소가 원자적으로 교체된다.
- 최종 검증 실패·중단·만료 또는 활성화 전 노출 seed는 폐기하고 새 setup으로 등록한다.
  활성화 후 seed 노출은 자격증명 유출로 처리해 `--reset-existing`으로 회전하고 기존 세션을 폐기한다.
- `AUTH_MFA_MASTER_KEY`를 잃거나 바꾸면 기존 TOTP seed를 복구할 수 없다. DB 백업과 분리된
  secret manager에 버전·복구 절차와 함께 보관하며 일반적인 키 회전처럼 무심코 교체하지 않는다.

기준은 [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238),
[NIST SP 800-63B OTP](https://pages.nist.gov/800-63-4/sp800-63b/authenticators/)와
[OWASP MFA](https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html)를
참고한다.

## 세션·CSRF·브라우저 방어

- 브라우저에는 랜덤 세션 원문을 `HttpOnly; SameSite=Lax; Path=/` 쿠키로만 저장한다.
- 운영 HTTPS에서는 `Secure`와 `__Host-` prefix를 사용한다.
- DB에는 세션 원문 대신 HMAC 해시, CSRF 해시, 만료·폐기 시각, IP/UA 해시만 저장한다.
- 기본 세션은 절대 만료 12시간·idle 120분, 로그인 상태 유지는 절대 30일·idle 7일이다.
  사용자당 활성 세션은 기본 10개로 제한하고 오래된 세션부터 폐기한다.
- 프로필에서 현재 세션을 식별하고 개별 폐기 또는 현재 세션을 제외한 전체 폐기를 할 수 있다.
- 기본으로 User-Agent를 세션에 결속한다. IP 결속은 모바일/프록시 환경의 오탐 때문에 선택 사항이다.
- 상태 변경 요청은 Fetch Metadata, Origin/Referer, double-submit CSRF 토큰을 함께 검증한다.
- 요청한 HTML 페이지의 inline script SHA-256 hash만 CSP에 추가하고, HSTS(HTTPS), frame deny,
  MIME sniff 방지, COOP/CORP, 민감 페이지 `Cache-Control: no-store`를 적용한다.
- `/docs`의 Swagger UI도 정확한 버전과 SRI를 고정하며, 문서 화면에서 임의 상태 변경 요청을 보내지
  못하도록 submit 기능을 비활성화한다.
- 요청 body는 ASGI stream을 읽는 시점에 `AUTH_MAX_REQUEST_BYTES`를 적용해 Content-Length가 없거나
  거짓인 요청도 제한한다.

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
  "verify_email": {"hour": 5, "day": 15},
  "forgot_password": {"hour": 5, "day": 10},
  "password_reset": {"hour": 10, "day": 30},
  "mfa": {"minute": 10, "hour": 50},
  "charts_query": {"second": 8, "minute": 120, "hour": 1000, "day": 5000},
  "action": "reject"
}
```

`action`은 `reject`, `slow_down`, `drop`을 지원한다. 이 카운터는 프로세스 메모리 기반이므로
다중 worker/인스턴스 전체 한도를 보장하지 않는다. 운영에서는 WAF를 정본으로 두고, 필요하면
추후 Redis 기반 공용 limiter로 교체한다.

## 운영 필수 설정

```dotenv
AUTH_ENV=production
AUTH_MODE=required
AUTH_PUBLIC_BASE_URL=https://dashboard.example.com
AUTH_ALLOWED_HOSTS=dashboard.example.com
AUTH_COOKIE_SECURE=true
AUTH_SESSION_PEPPER=<32자 이상, secret-manager에서 주입>
AUTH_MFA_MASTER_KEY=<32자 이상, 장기 보관 secret>
AUTH_REQUIRE_MFA_FOR_PRIVILEGED=true
AUTH_SESSION_IDLE_MINUTES=120
AUTH_MAX_SESSIONS_PER_USER=10
AUTH_REMEMBER_IDLE_DAYS=7
AUTH_MAX_REQUEST_BYTES=262144
AUTH_TRUST_PROXY_HEADERS=true
AUTH_BIND_SESSION_USER_AGENT=true
AUTH_BIND_SESSION_IP=false
SMTP_USE_TLS=true
SMTP_USE_SSL=false
SMTP_ALLOW_PLAINTEXT=false
```

`AUTH_MODE`의 기본값은 `required`다. `local_auto`는 팀원 PC의 loopback HTTP + SQLite에서만
일반 회원 세션을 자동 발급하며 proxy header를 신뢰하지 않는다. 배포 dev/main runtime은
모두 `required`를 명시하고, 배포 스크립트도 다른 값을 거부한다. 브랜치 이름이나
`AUTH_ENV != production` 조건은 인증 우회 기준으로 사용하지 않는다.

예약된 로컬 분석 계정은 항상 `member/active`이며 사용자별 `charts=allow` override를 멱등적으로
확보한다. 이는 오래된 로컬 SQLite의 역할 권한이 현재 기본값과 달라도 자신의 Charts에 진입하기 위한
로컬 전용 복구이며, 전역 member 역할 권한이나 `required` 환경의 일반 계정을 변경하지 않는다.
Charts 레이아웃 쓰기 API는 page 권한과 별도로 `member/operator/admin` 역할을 확인하고 모든 CRUD에
현재 `user_id`를 사용한다. 따라서 별도 조회 권한을 받은 guest도 레이아웃을 바꿀 수 없고, member도
다른 사용자의 페이지 ID를 조회하거나 수정할 수 없다. Charts 화면도 guest에게 `READ ONLY`를 표시하고
레이아웃 추가·편집·이름변경·복제·삭제 UI를 숨긴다. 화면은 세션 응답의 서버 계산 capability
`can_edit_charts`만 소비하며, 보안 정본은 UI가 아니라 서버의 역할 검사다.

- `AUTH_TRUST_PROXY_HEADERS=true`는 원본 서버가 승인된 LB/WAF에서만 접근 가능하고, 해당 장비가
  전달 헤더를 덮어쓴다는 것이 보장될 때만 켠다.
- 기본 최고관리자 자격증명은 없다. production은 `AUTH_BOOTSTRAP_ADMIN_*`를 거부하므로,
  기록이 남지 않는 보호된 대화형 TTY에서 `.venv/bin/python scripts/create_admin.py`로 생성한다.
- 사용자가 이미 있는 DB에 별도 최고관리자를 추가할 때만 `--create-additional`과 화면의 정확한
  확인 문구를 사용한다. 기존 계정 승격은 `--promote-existing` 확인 절차를 따른다.
- 최초 최고관리자를 만든 뒤 같은 TTY에서 `.venv/bin/python scripts/setup_mfa.py`로 MFA를
  등록한다. 화면에 표시되는 seed·URI·복구 코드는 녹화·로그에 남기지 않고 암호화된 운영 금고에 보관한다.
- 권한 계정의 인증 요소를 모두 분실했거나 활성 seed/URI 노출이 확인된 경우에만 서버 콘솔에서
  `.venv/bin/python scripts/setup_mfa.py --reset-existing`를 실행한다.
- 내장 blocklist는 최소 방어선이다. 공개 운영 전 정기 갱신되는 유출 비밀번호 목록을 로컬/사설
  서비스로 확장하되 비밀번호 원문을 제3자에게 전송하지 않는다.
- inline event handler는 제거해 `script-src-attr 'none'`을 적용했다. 일반 inline script도 응답
  body의 SHA-256 hash와 일치할 때만 실행된다. 기존 단일 HTML 화면의 inline style 때문에
  `style-src 'unsafe-inline'`은 아직 남아 있으므로 공개 운영 전 CSS class로 이동한다.
- production 앱 worker는 DDL/migration을 실행하지 않는다. 단일
  `.venv/bin/python scripts/init_auth_db.py` job을 먼저 완료한 뒤 worker를 기동한다.
