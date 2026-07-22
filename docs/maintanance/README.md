# 개발 실행·초기 최고관리자·운영 원칙

> 기본 계정과 기본 비밀번호는 없다. 실제 이메일·비밀번호·MFA seed·복구 코드는
> 저장소·문서·로그에 기록하지 않는다.

## 팀원 로컬 분석 빠른 시작 — 로그인 생략

로컬에서 데이터를 분석하고 Charts Studio 레이아웃을 확인하는 작업은 실제 계정 생성 없이
시작할 수 있다. 이 모드는 배포 dev/main과 분리된 로컬 SQLite만 사용한다.
처음 실행하는 팀원은 [사람용 로컬 분석 가이드](../local-analysis-human-guide.md)를 정본으로
따르고, AI에게 준비를 맡길 때는 [AI용 runbook](../local-analysis-agent-runbook.md)을 사용한다.

macOS/Linux · Git Bash:

```bash
cd <프로젝트 루트>/sample
docker compose up -d trino
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info

cd dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env.local || cp .env.local.example .env.local
set -a
source .env.local
set +a

.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Windows(PowerShell):

```powershell
cd <프로젝트 루트>\sample
docker compose up -d trino

cd dashboard
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
```

`run_local.ps1`이 `.env.local` 생성·로드 → `init_auth_db.py` → uvicorn을 일괄 수행한다. bash용
`set -a; source .env.local; set +a`는 PowerShell에서 동작하지 않는다(SHARE.md §0.1).

`http://127.0.0.1:8765/charts`를 열면 `local-analyst@localhost.invalid` 예약 계정이
`operator` 역할로 로컬 DB에 한 번 생성되고 정상 AuthSession과 CSRF 쿠키가 자동 발급된다.
레이아웃은 이 사용자 ID에 저장된다. 운영자 권한이라 운영 콘솔과 '권한별 화면 관리'(게스트/멤버
화면 미리보기)를 사용할 수 있으며, 최고관리자 전용 기능(IP 자동차단 해제·MFA 강제 초기화 등)만
제한된다. 고정 비밀번호나 실제 이메일은 없다.

다음 중 하나라도 어긋나면 앱은 `local_auto`로 기동하지 않는다.

- `AUTH_ENV=development`, `AUTH_MODE=local_auto`
- SQLite `DATABASE_URL`
- loopback HTTP `AUTH_PUBLIC_BASE_URL`과 loopback-only `AUTH_ALLOWED_HOSTS`
- `AUTH_TRUST_PROXY_HEADERS=false`, `AUTH_COOKIE_SECURE=false`
- 비어 있는 `AUTH_BOOTSTRAP_ADMIN_EMAIL`·`AUTH_BOOTSTRAP_ADMIN_PASSWORD`

서버를 `0.0.0.0`에 bind하거나 reverse proxy 뒤에 두지 말고 위 명령처럼 `127.0.0.1`에만 bind한다.
실제 로그인·가입·승인·MFA 흐름을 개발할 때는 아래의 `AUTH_MODE=required` 환경을 사용한다.

## 실제 인증 흐름 개발 빠른 시작

Python 3.9+와 Docker가 필요하다. 로그인·회원·권한·카탈로그 화면은 Trino 없이도
기동할 수 있다. Charts Studio의 실데이터 조회에는 Trino와 `sample/.env`에 주입된
R2/Iceberg catalog 연결 설정이 필요하다.

```bash
cd <프로젝트 루트>/sample
docker compose up -d trino
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info
docker compose exec trino trino --execute 'SELECT 1'

cd dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env || cp .env.example .env
set -a
source .env
set +a

.venv/bin/python scripts/init_auth_db.py
.venv/bin/python scripts/create_admin.py
.venv/bin/python scripts/setup_mfa.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Windows(PowerShell)에서는 `run_local.ps1`(로컬 자동 인증 전용) 대신 `.env`를 직접 로드하고
`.venv\Scripts\...`로 실행한다:

```powershell
cd <프로젝트 루트>\sample; docker compose up -d trino
cd dashboard
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # 값 편집 후 아래로 로드
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object { $n,$v = $_ -split '=',2; Set-Item "env:$($n.Trim())" $v.Trim() }
.venv\Scripts\python scripts\init_auth_db.py
.venv\Scripts\python scripts\create_admin.py
.venv\Scripts\python scripts\setup_mfa.py
.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

- `create_admin.py`에서 실제 이메일과 15~128자 비밀번호를 입력한다.
- 관리자 MFA는 개발 환경에서도 권장하며 production에서는 필수다.
- 관리자 생성과 MFA 설정은 입력이 기록되지 않는 대화형 터미널에서만 실행된다.
- 기존 사용자가 있는 DB에 관리자를 추가하거나 승격할 때는 CLI가 안내하는 명시적 옵션과
  확인 문구가 필요하다.

### MFA 앱 등록

`setup_mfa.py`는 QR 코드를 표시하지 않는다.

1. 인증 앱에서 **계정 추가 → 설정 키 직접 입력**을 선택하고 CLI의
   `Authenticator secret`을 등록한다. 방식은 시간 기반 TOTP, SHA-1, 6자리, 30초다.
2. 앱에 표시되는 현재 6자리를 CLI의 `Current 6-digit authenticator code`에 입력한다.
3. `MFA enabled` 뒤에 표시되는 일회용 복구 코드를 별도 암호화 금고에 보관한다.

CLI가 남은 횟수를 안내하는 동안은 같은 앱 항목의 갱신된 6자리를 다시 입력한다. 5회 실패로
종료되거나 설정이 중단·만료됐거나 활성화 전 seed/URI가 노출됐다면 해당 앱 항목을 폐기하고
스크립트를 다시 실행해 새 seed로 등록한다. 활성화 후 노출됐다면 `--reset-existing`으로
회전한다. seed·URI·복구 코드를 채팅, 티켓, 브라우저 주소창에 붙여넣지 않는다.

접속:

- 랜딩: `http://127.0.0.1:8765/`
- 로그인: `http://127.0.0.1:8765/auth/login`
- 운영 콘솔: `http://127.0.0.1:8765/admin`
- Charts Studio: `http://127.0.0.1:8765/charts`

SMTP를 설정하지 않은 개발 환경에서는 가입자가 `승인 대기` 상태로 등록된다.
최고관리자가 `/admin`에서 승인하고 역할·페이지 권한을 지정한다. 비밀번호 찾기 메일도
SMTP가 없으면 발송되지 않으므로 관리자 지원 절차를 사용한다.

## 기동 확인

```bash
curl -fsS http://127.0.0.1:8765/health
```

1. 응답이 HTTP `200`이고 `database`가 `ok`인지 확인한다.
2. 생성한 계정으로 로그인해 `/admin` 회원 목록 접근을 확인한다.
3. Charts 운영 시 Trino `/v1/info`와 `/charts` 대표 조회도 확인한다.

## Production 최초 구성

다음 값은 평문 `.env`가 아니라 Secret Manager에서 주입한다.

- `AUTH_ENV=production`
- `AUTH_MODE=required`
- HTTPS `AUTH_PUBLIC_BASE_URL`과 제한된 `AUTH_ALLOWED_HOSTS`
- 명시적 `DATABASE_URL`과 원격 RDB 인증서·hostname 검증
  (`PostgreSQL: sslmode=verify-full`, `MySQL/MariaDB: ssl_ca + ssl_check_hostname=true`)
- 32자 이상의 `AUTH_SESSION_PEPPER`와 별도 `AUTH_MFA_MASTER_KEY`
- `AUTH_COOKIE_SECURE=true`, `AUTH_REQUIRE_MFA_FOR_PRIVILEGED=true`

DB 백업 후 migration job 하나에서 `scripts/init_auth_db.py`를 먼저 실행한다. 이어 보호된
대화형 터미널에서 `scripts/create_admin.py`, `scripts/setup_mfa.py`를 실행하고 앱을
순차 기동한다. production 앱 프로세스는 스키마를 변경하지 않고 결과만 검증한다.
앱 포트는 인터넷에 직접 공개하지 않고 HTTPS LB/reverse proxy 뒤의 private origin으로
배치한다. 전달 헤더는 origin 직접 접근이 차단된 경우에만 신뢰한다.

## 운영 원칙

- TOTP·일회용 복구 코드와 DB dump·session pepper·MFA master key는 각각 분리 보관한다.
- 결제 알림 worker는 1~5분, 인증 cleanup dry-run은 일 1회 실행한다.
- cleanup은 대상 건수와 백업 확인 후에만 `--apply`한다.
- 사용자 레이아웃 초기화를 위해 전체 DB를 삭제하지 않는다.
- `setup_mfa.py --reset-existing`은 본인 확인이 끝난 break-glass 상황에서만 사용한다.

세부 기동·중지: [운영 매뉴얼](../operations.md) ·
보안 설정: [인증 보안](../additional_doc/auth/security-architecture.md) ·
RDB 설정: [DB 스키마](../additional_doc/database/schema.md)
