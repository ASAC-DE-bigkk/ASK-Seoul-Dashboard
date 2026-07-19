# 초기 최고관리자 접속 및 운영 원칙

> 기본 계정과 기본 비밀번호는 없다. 실제 이메일·비밀번호·MFA seed·복구 코드는 저장소·문서·로그에 기록하지 않는다.

## 최초 구성

production은 HTTPS origin/host, `DATABASE_URL`, 32자 이상의 `AUTH_SESSION_PEPPER`,
별도 `AUTH_MFA_MASTER_KEY`, `AUTH_COOKIE_SECURE=true`,
`AUTH_REQUIRE_MFA_FOR_PRIVILEGED=true`가 필요하다. 시크릿은 Secret Manager에서 주입한다.

기록이 남지 않는 보호된 대화형 터미널에서 실행한다.
production 환경변수는 실행 전에 Secret Manager가 주입한 상태여야 한다.

```bash
# sample/에서
cd dashboard
umask 077
.venv/bin/python scripts/init_auth_db.py
.venv/bin/python scripts/create_admin.py
.venv/bin/python scripts/setup_mfa.py
.venv/bin/uvicorn app.main:app --port 8765
```

- 로그인: `${AUTH_PUBLIC_BASE_URL}/auth/login`
- 운영 콘솔: `${AUTH_PUBLIC_BASE_URL}/admin`
- 계정 이메일과 15~128자 비밀번호는 CLI에서 지정한다.
- TOTP와 일회용 복구 코드는 암호화된 운영 금고에 보관한다.

## 기동 확인

1. `/health`가 `200`과 `database=ok`를 반환하는지 확인한다.
2. 비밀번호와 TOTP로 로그인해 `/admin` 및 회원 목록 접근을 확인한다.
3. Charts 운영 시 Trino `/v1/info`와 `/charts` 대표 조회도 확인한다.

## 운영 원칙

- DB 백업 후 migration job 하나를 먼저 실행하고 앱을 순차 기동한다.
- production 앱 프로세스는 스키마를 변경하지 않고 migration 결과만 검증한다.
- DB dump와 session pepper·MFA master key는 분리 보관한다.
- 결제 알림 worker는 1~5분, 인증 cleanup dry-run은 일 1회 실행한다.
- cleanup은 대상 건수와 백업 확인 후에만 `--apply`한다.
- 사용자 레이아웃 초기화를 위해 전체 DB를 삭제하지 않는다.
- `setup_mfa.py --reset-existing`은 본인 확인이 끝난 break-glass 상황에서만 사용한다.

세부 절차: [운영 매뉴얼](../operations.md) ·
[인증 보안](../additional_doc/auth/security-architecture.md) ·
[DB 스키마](../additional_doc/database/schema.md)
