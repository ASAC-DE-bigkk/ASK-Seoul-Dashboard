# 로컬 데이터 분석 runbook — AI 에이전트용

대상: 팀원의 신뢰된 로컬 PC에서 ASK SEOUL Dashboard의 로컬 분석 환경을 준비·검증하거나,
`AUTH_MODE=local_auto` 관련 코드를 수정하는 AI 에이전트.

목표는 로그인 편의를 제공하면서 기존 사용자·세션·CSRF·권한·레이아웃 계약을 보존하는 것이다.
이 문서는 AI에게 dev/main 배포, 상위 데이터 스택 변경, secret 열람 또는 데이터 삭제 권한을 주지 않는다.

## 0. 먼저 읽을 정본

repository checkout에서 다음 순서로 읽는다.

1. `dashboard/AGENTS.md`
2. `dashboard/docs/HERITAGE.md`
3. `dashboard/docs/README.md`
4. 이 문서
5. 사람의 실행 절차가 필요하면 `dashboard/docs/local-analysis-human-guide.md`
6. 실제 구현:
   - `dashboard/app/auth/config.py`
   - `dashboard/app/auth/middleware.py`
   - `dashboard/app/auth/service.py`
   - `dashboard/app/auth/dependencies.py`
   - `dashboard/app/charts/router.py`
7. 환경·배포 경계:
   - `dashboard/.env.local.example`
   - `dashboard/.env.example`
   - `dashboard/deploy/runtime.dev.env.example`
   - `dashboard/deploy/runtime.env.example`
   - `dashboard/deploy/bootstrap-dev.sh`
   - `dashboard/deploy/deploy.sh`
8. 회귀 계약:
   - `dashboard/tests/test_security_config.py`
   - `dashboard/tests/test_auth_flow.py`

문서와 코드가 다르면 실행 코드를 확인하고 같은 변경에서 문서를 갱신한다.

## 1. 실행 모드 판정

| 사용자 목표 | 사용할 모드 | AI의 판단 |
|---|---|---|
| 개인 PC에서 Catalog·Charts 데이터를 빠르게 확인 | `local_auto` | 이 runbook으로 진행 가능 |
| 로그인·가입·승인·MFA 기능 개발 | `required` | `docs/maintanance/README.md`의 실제 인증 흐름 사용 |
| 배포된 dev 접속 또는 장애 확인 | `required` | 이 문서를 중단하고 deployment/operations 문서 사용 |
| main 운영 활성화 | `required` | 별도 설계·승인 없이는 진행 금지 |

Git branch, `AUTH_ENV != production`, 접속 URL만 보고 인증 우회를 결정하지 않는다.
로컬 분석은 명시적인 `AUTH_MODE=local_auto`와 아래 모든 보호 조건이 필요하다.

로컬 실행 토폴로지는 다음 하나로 고정한다.

```text
sample/docker-compose.yml의 trino(:30586) → dashboard/FastAPI(:8765) → loopback browser
```

상위 `sample/`이 로컬 Trino의 정본이다. `dashboard/deploy/trino/`는 dev 서버용 최소 companion이므로
로컬에서 두 번째 Trino로 실행하지 않는다. 로컬과 dev는 topology가 아니라 Trino 버전, catalog,
relation, 질의 제한의 데이터 계약을 맞춘다.

## 2. 변경하면 안 되는 불변식

`local_auto`의 설정 검증은 다음을 모두 강제해야 한다.

- `AUTH_ENV=development`
- `AUTH_MODE=local_auto`
- SQLite `DATABASE_URL`
- `http://127.0.0.1:<port>`, `http://localhost:<port>` 또는 IPv6 loopback public origin
- loopback 값만 포함한 `AUTH_ALLOWED_HOSTS`
- `AUTH_TRUST_PROXY_HEADERS=false`
- `AUTH_COOKIE_SECURE=false`
- bootstrap 관리자 자격증명 미설정

요청 처리 불변식:

- 자동 세션은 직접 연결한 client IP가 loopback인 안전한 GET/HEAD에만 발급한다.
- 대상은 보호 페이지 또는 `/api/v1/auth/session`뿐이다.
- 예약 계정은 `member/active`이며 `operator/admin`으로 승격하지 않는다.
- 고정 비밀번호를 제공하지 않고 무작위 비밀번호 hash만 DB에 둔다.
- 기존 `AuthSession`, HMAC token hash, CSRF cookie, idle/absolute 만료, 세션 상한을 재사용한다.
- `current_user` 의존성과 Charts API의 `user.id` 레이아웃 격리를 우회하지 않는다.
- 인증되지 않은 최초 POST가 자동 인증되면 안 된다.
- `/admin`과 관리 API는 계속 거부한다.
- dev/main runtime은 `AUTH_MODE=required`이며 배포 스크립트가 다른 명시값을 거부한다.

## 3. 허용 범위와 금지 작업

사용자가 로컬 환경 준비를 요청했다면 수행 가능한 작업:

- repository와 현재 변경 상태 read-only 확인
- 기존 `sample/docker-compose.yml`의 Trino 기동·상태·read-only query 확인
- `.env.local.example`에서 ignored `.env.local` 생성
- 로컬 `.venv` 생성과 이미 승인된 의존성 설치
- 로컬 SQLite schema 초기화
- Uvicorn을 `127.0.0.1`에 기동
- `/health`, 자동 세션, Catalog/Charts 접근, 관리자 차단 검증
- 테스트와 정적 검증 실행
- secret 없는 결과 보고

별도 승인 없이 수행하지 않는 작업:

- 상위 `sample/.env`, Trino/R2 catalog 또는 데이터 파이프라인 변경
- `dashboard/deploy/trino/`로 로컬 두 번째 Trino 생성
- dev/main runtime 파일의 실제 값 변경이나 서버 배포
- Uvicorn `0.0.0.0` bind, reverse proxy 또는 외부 tunnel 추가
- `AUTH_MODE=local_auto`의 fail-closed 조건 완화
- 로컬 분석 계정에 관리자 권한 부여
- 실제 이메일·공용 비밀번호·token·cookie를 코드나 문서에 저장
- 전체 인증 DB, 레이아웃, cache 또는 Docker volume 삭제
- 사용자 소유 `.env.local`을 출력하거나 응답에 복사
- unrelated worktree 변경 수정·삭제·커밋

## 4. 시작 전 점검 — 기본 read-only

작업 위치를 확인한다.

```bash
pwd
git -C dashboard status --short --branch
git submodule status dashboard
test -f dashboard/.env.local.example
grep -q '^AUTH_MODE=local_auto$' dashboard/.env.local.example
test -f dashboard/app/auth/config.py
test -f dashboard/tests/test_security_config.py
```

`dashboard/`가 이미 현재 디렉터리라면 `git status --short`처럼 경로를 조정한다.
기존 변경은 사용자 소유이므로 작업 범위와 겹치는지 먼저 확인한다.

Dashboard revision은 목적에 따라 처리한다.

- feature branch 또는 dirty worktree의 변경을 시험하는 요청이면 현재 checkout을 그대로 사용한다.
  `git submodule update`, branch switch, pull을 실행하지 않는다.
- 팀에 병합된 공용 Dashboard를 시험하는 요청이고 worktree가 깨끗할 때만 `sample/` 루트에서
  `./scripts/update-nested-git.sh dashboard`를 실행한다. 이 명령은 `.gitmodules`의 `main`만
  fast-forward하며 Dashboard 이외의 서브모듈은 건드리지 않는다.
- 일반 `git submodule update --init dashboard`는 상위 gitlink의 고정 revision을 복원하므로
  공용 최신 소스 확보 수단으로 사용하지 않는다.
- 로컬 실행 revision이 상위 gitlink와 달라 `M dashboard`가 표시되는 것은 허용한다. 로컬 검증을
  이유로 상위 gitlink를 stage·commit하지 않는다. gitlink 승격은 별도 통합 이슈/PR의 책임이다.

상위 데이터 스택은 값을 읽지 말고 상태만 확인한다.

```bash
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info
```

Trino가 없더라도 Catalog·인증 UI 작업은 진행할 수 있다. Charts 실데이터 검증만 blocker로 구분한다.
`AUTH_MODE=local_auto` 예제가 없으면 인증 설정부터 바꾸지 말고, 먼저 현재 Dashboard revision이 사용자
요청 대상인지 판정한다. 공용 `main` 확인 요청이면 위 선택 갱신을 사용하고, feature 작업본이면 해당
branch의 구현 상태를 보고한다.

## 5. 승인된 로컬 준비와 실행

사람용 가이드의 명령을 그대로 사용한다. 터미널 A에서는 `sample/`의 기존 Trino만 기동한다.

```bash
docker compose up -d trino
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info
docker compose exec trino trino --execute 'SELECT 1'
docker compose exec trino trino --execute 'SHOW SCHEMAS FROM iceberg_dev'
docker compose exec trino trino --execute 'SHOW TABLES FROM iceberg_dev.commerce'
```

마지막 두 catalog query에는 `sample/.env`의 `R2_DEV_*` 설정이 필요하다. AI는 값 존재 여부나 실패를
secret 없이 보고하고, 별도 승인 없이 값을 열람·출력·편집하지 않는다. 터미널 B에서는 Dashboard를
준비하고 실행한다.

```bash
cd dashboard
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env.local || cp .env.local.example .env.local

set -a
source .env.local
set +a

.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

주의:

- 기존 `.env.local`이 있으면 덮어쓰지 않는다.
- 환경 파일의 실제 전체 내용을 출력하지 않는다.
- bind 실패가 sandbox 제한이라면 정해진 승인 절차를 사용하고 보안 조건을 낮추지 않는다.
- 서버는 실행 중인 동안 60초 이상 상태 업데이트 없이 방치하지 않는다.

두 번째 실행부터는 `.venv` 생성·의존성 설치·환경 파일 복사를 생략하고, 새 Dashboard 터미널에서
`set -a; source .env.local; set +a`를 다시 실행한 뒤 Uvicorn을 기동한다.

## 6. 검증 순서

### 6-1. 서버·인증·권한

```bash
curl -fsS http://127.0.0.1:8765/health
```

브라우저 또는 cookie를 안전하게 처리하는 HTTP client로 다음을 확인한다.

1. `GET /api/v1/auth/session` → HTTP 200, `authenticated=true`, `role=member`
2. `GET /catalog` → HTTP 200
3. `GET /api/v1/charts/meta` → HTTP 200
4. CSRF 없는 인증 후 Charts POST → HTTP 403
5. 같은 origin의 정상 CSRF POST → 인증을 통과
6. `GET /admin` → HTTP 403 또는 `/profile?denied=1` redirect
7. non-loopback client → 자동 세션 미발급, 보호 화면 로그인 redirect/API 401
8. `GET /charts?selftest=1` 완료 → 브라우저 탭 제목 `SELFTEST_ALL_PASS`

세션 cookie나 CSRF 원문은 출력·보고하지 않는다. 임시 cookie jar를 만들었다면 작업 종료 전에
정확한 임시 경로만 확인하고 삭제한다.

작업 종료는 Uvicorn `Ctrl+C` 후 `sample/` 루트의 `docker compose stop trino`만 사용한다.
`down -v`, volume 삭제, 전체 DB·cache 삭제는 로컬 종료 절차가 아니다.

### 6-2. 코드 변경 시 회귀 검증

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app scripts
node --check app/static/charts/js/*.js
node --check app/static/auth/*.js
bash -n \
  deploy/deploy.sh \
  deploy/bootstrap-dev.sh \
  deploy/install-docker-rocky.sh \
  deploy/trino/deploy.sh
git diff --check
```

최소 회귀 항목은 `tests/test_security_config.py`에 유지한다.

- 잘못된 `AUTH_MODE` 거부
- test/production에서 `local_auto` 거부
- PostgreSQL, non-loopback origin/Host, trusted proxy 거부
- loopback에서 member 세션과 CSRF 발급
- 최초 POST 자동 인증 금지
- 관리자 접근 거부
- non-loopback 자동 인증 금지

## 7. 장애 판정표

| 관찰 결과 | 판정과 다음 행동 |
|---|---|
| `AUTH_MODE` 형식 오류 | 허용값은 `required`, `local_auto`뿐이다. 임의 alias를 추가하지 않는다. |
| development가 아니어서 기동 거부 | 로컬 작업인지 다시 확인한다. dev/main 설정을 local로 낮추지 않는다. |
| SQLite가 아니어서 기동 거부 | 배포 DB 또는 공유 DB일 가능성이 있다. 로컬 전용 SQLite 경로를 사용한다. |
| origin/Host가 loopback이 아님 | 외부 노출을 시도하지 말고 `127.0.0.1` 기준으로 되돌린다. |
| proxy header 신뢰 때문에 거부 | 로컬 직접 접속에서는 false가 정답이다. proxy를 추가하지 않는다. |
| 브라우저만 `403 origin mismatch` | `localhost`와 `127.0.0.1` 혼용 여부를 먼저 확인한다. auth DB나 cookie 방어를 약화하지 않는다. |
| 자동 계정 역할/상태 오류 | fail closed가 정상이다. DB 상태와 변경 이력을 보고하고 자동 승격·삭제하지 않는다. |
| Charts meta는 정상, query만 실패 | 인증이 아니라 Trino/R2 또는 query 문제로 분리한다. |
| layout이 다른 환경과 다름 | 로컬 DB 격리가 정상이다. DB 파일을 공유하지 않는다. |
| dev/main 배포에서 local_auto 감지 | 배포 중단이 정상이다. runtime을 `required`로 복구하고 원인을 보고한다. |

## 8. 구현 변경 시 동기화 범위

로컬 인증 계약을 변경하면 같은 작업에서 확인·갱신한다.

- 설정: `app/auth/config.py`, `.env.local.example`, `.env.example`, deploy runtime examples
- 세션·인가: `app/auth/middleware.py`, `app/auth/service.py`, `app/auth/dependencies.py`
- 사용자별 레이아웃: `app/charts/router.py`, `app/charts/layouts.py`
- 배포 차단: `deploy/bootstrap-dev.sh`, `deploy/deploy.sh`
- 테스트: `tests/test_security_config.py`, 필요 시 `tests/test_auth_flow.py`
- 문서: 이 문서, `local-analysis-human-guide.md`, `maintanance/README.md`,
  `operations.md`, `additional_doc/auth/security-architecture.md`, `HERITAGE.md`, 각 인덱스

자동 로그인을 구현한다는 이유로 Charts router의 `Depends(current_user)`를 제거하거나,
레이아웃을 전역 파일로 되돌리지 않는다.

## 9. 사람에게 보고하는 형식

secret 없이 아래 형식으로 보고한다.

```text
대상: 팀원 로컬 PC / dashboard
작업 모드: local_auto | required
bind: 127.0.0.1:<port> | 미기동
인증 DB: SQLite | 기타(중단)
Trino: healthy | 미기동 | 미확인
자동 세션: member 성공 | 미검증 | 실패
CSRF: 정상 차단/허용 | 미검증 | 실패
관리자 차단: 정상 | 실패
레이아웃: 저장·새로고침 유지 | 미검증 | 실패
테스트: 실행 명령과 pass/fail 개수
수행한 변경: 파일 목록과 요약
남은 blocker: 없음 또는 사람이 수행할 정확한 한 단계
```

세션·CSRF 값, DSN, R2 값, 실제 이메일·비밀번호, `.env.local` 내용은 보고에 포함하지 않는다.
