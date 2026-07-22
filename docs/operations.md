# 운영 매뉴얼 — 솔루션 기동(up) / 중지(down)

대상: 이 데모(카탈로그 + Charts Studio)를 켜고 끄고 초기화하는 사람.
구성 요소는 **① 데이터 스택(docker compose)**, **② 인증/권한 RDB**,
**③ 대시보드 서버(FastAPI)**다.

```
브라우저 ↔ ③ FastAPI(:8765) ↔ ② 인증/권한 RDB
                  ├─질의→ ① Trino(:30586) → R2/Iceberg gold
                  └─읽기→ snapshot/catalog_snapshot.json
Airflow DAG ─적재────────────────────→ R2/Iceberg gold
```

로컬에서는 상위 `sample/docker-compose.yml`이 Trino를 소유하고 Dashboard는
`http://127.0.0.1:30586`으로 재사용한다. `dashboard/deploy/trino/`는 dev 서버용 companion이며
로컬용 두 번째 Trino가 아니다. 로컬과 dev는 컨테이너 배치가 아니라 Trino catalog·relation·질의
계약을 동일하게 유지한다. 로컬의 완전한 최초/재실행 절차는
[사람용 로컬 분석 가이드](local-analysis-human-guide.md)를 따른다.

---

## 1. 기동 (up)

### 1-1. 데이터 스택 (전제)

`sample/` 루트(docker-compose.yml 위치)에서:

```bash
cd <프로젝트 루트>/sample
docker compose up -d            # 전체 스택
# Charts Studio 만 쓸 거면 최소 요건은 trino 하나다:
docker compose up -d trino
```

Trino가 R2/Iceberg gold를 읽으려면 `sample/.env`에 허가된 catalog·object storage
연결 설정이 주입되어 있어야 한다. 이 파일은 `dashboard/.env`와 별개이며 커밋하지 않는다.

기동 확인:

```bash
docker compose ps                                   # trino 가 healthy 인지
curl -s http://127.0.0.1:30586/v1/info | head -c 80  # {"nodeId":...,"state":"ACTIVE"...}
docker compose exec trino trino --execute 'SELECT 1'  # query engine 확인
```

| 서비스 | 포트 | Charts Studio 와의 관계 |
|---|---|---|
| trino | 127.0.0.1:30586 | **필수** — gold 집계 질의 대상 |
| airflow-apiserver | 127.0.0.1:30585 | 선택 — gold 를 갱신하는 적재 파이프라인 |
| postgres | 호스트 미노출 | Airflow 메타 DB, 대시보드 인증 DB와 별개 |
| marquez-api / marquez-web | 5000 / 3000 | 무관 (OpenLineage 워크로드) |

### 1-2. 환경별 Docker Compose (local / dev)

대시보드를 환경별로 Docker Compose로 지정 실행할 수 있다. 운영(prod)은 이번 범위 밖이며,
서버 배포는 이미지 기반 `deploy/compose.yaml`(CI/서버 전용)을 사용한다.

**local — 로그인 없이 운영자 자동 로그인, Trino는 상위 `sample/`에서 재사용**

```bash
cd <프로젝트 루트>/sample && docker compose up -d trino      # ① 상위 Trino
cd dashboard
docker compose -f deploy/compose.local.yaml up --build       # ② 앱(운영자 자동 로그인)
# → http://127.0.0.1:8765/charts
```

- 비밀값 불필요(세션 pepper는 컨테이너 `data` 볼륨에 자동 생성). 선택 오버라이드는
  `deploy/env/local.env.example`.
- 앱 컨테이너는 클라이언트 IP가 Docker 게이트웨이(사설)로 보이므로 자동 로그인을 위해
  `AUTH_LOCAL_AUTO_CLIENT_CIDRS`(사설 대역)를 신뢰한다. **반드시 127.0.0.1 에만 publish**하며
  이 전제에서만 안전하다(`0.0.0.0` 금지).

**dev — 실제 로그인(required) + PostgreSQL, Trino는 별도 compose로 배포**

```bash
cd dashboard
cp deploy/env/dev.env.example deploy/env/dev.env       # 값 채우기(관리자·postgres·pepper)
docker compose -f deploy/trino/compose.yaml --env-file deploy/env/trino.env up -d   # ① Trino 별도
docker compose -f deploy/compose.dev.yaml  --env-file deploy/env/dev.env   up --build # ② 앱+DB
# → http://127.0.0.1:8765  (dev.env의 부트스트랩 관리자로 로그인)
```

- `deploy/trino/compose.yaml`이 `elt_net`을 만들고 앱이 external로 참가해 `http://trino:8080`으로
  질의한다. Trino용 R2 dev 값은 `deploy/trino/runtime.env.example` 참고.
- `deploy/env/*.env`(실제 값)는 `.gitignore` 대상이며 `*.example`만 커밋한다.

### 1-3. 대시보드 서버 (호스트에서 직접 실행)

팀원이 로그인 없이 로컬 데이터 분석만 수행할 때:

처음 실행하거나 장애를 해결할 때는
[사람용 로컬 분석 가이드](local-analysis-human-guide.md)를 정본으로 사용한다. AI에게 준비·검증을
맡길 때는 [AI용 로컬 분석 runbook](local-analysis-agent-runbook.md)을 함께 제공한다.

실행 환경은 Windows(PowerShell)와 macOS/Linux(bash) 둘 다 지원한다. 앱은 `.env` 파일을 자동
로드하지 않으므로 `.env.local` 주입은 실행 셸이 담당한다. bash 예시를 PowerShell에 그대로
붙여넣으면 `source`·`set -a`가 없어 `AUTH_MODE`가 주입되지 않는다.

macOS/Linux · Git Bash:

```bash
cd sample/dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env.local || cp .env.local.example .env.local
set -a; source .env.local; set +a
.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Windows PowerShell:

```powershell
cd sample\dashboard
py -3.13 -m venv .venv     # 안정 버전 권장(3.14 등 최신은 pydantic_core 휠 부재 가능)
.venv\Scripts\pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1   # .env.local 로드 → init_auth_db → uvicorn 일괄
```

기본 `powershell`(5.1)은 `.ps1` 실행을 정책으로 막을 수 있어 `-ExecutionPolicy Bypass`가 필요하다.
`No module named 'pydantic_core._pydantic_core'`가 나오면 venv Python과 네이티브 바이너리 버전이
어긋난 것이므로 `.venv`를 안정 버전으로 재생성한다(`Remove-Item .venv -Recurse -Force` 후 재설치).

이 경로는 로컬 SQLite의 일반 회원 세션을 자동 발급하며 관리자 권한은 제공하지 않는다.
배포 dev/main 또는 실제 인증 흐름 검증에는 사용하지 않는다.

실제 로그인·회원가입 흐름 개발을 위한 최초 1회:

```bash
cd sample/dashboard
python3 -m venv .venv                         # macOS/Linux (Windows: py -3 -m venv .venv)
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
cp .env.example .env
# .env 값을 편집한 뒤:
set -a; source .env; set +a
.venv/bin/python scripts/init_auth_db.py
.venv/bin/python scripts/create_admin.py
# production에서는 최초 관리자 MFA를 반드시 등록한다.
.venv/bin/python scripts/setup_mfa.py

# 인증 요소를 모두 분실했거나 활성 seed/URI 노출이 확인된 break-glass 상황에서 사용
.venv/bin/python scripts/setup_mfa.py --reset-existing
```

인증 앱 등록과 seed 폐기 기준은
[개발 실행·초기 최고관리자 매뉴얼](maintanance/README.md#mfa-앱-등록)을 따른다.

기동:

```bash
# 새 로컬 셸을 열 때마다 환경을 다시 로드한다. (macOS/Linux · Git Bash)
set -a
source .env
set +a
.venv/bin/uvicorn app.main:app --port 8765
# 개발 중이면 --reload 를 붙인다
```

Windows PowerShell에서 `.env`(실제 인증 흐름)를 로드하고 기동:

```powershell
# 새 로컬 셸을 열 때마다 환경을 다시 로드한다.
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  Set-Item "env:$($name.Trim())" $value.Trim()
}
.venv\Scripts\uvicorn app.main:app --port 8765   # 개발 중이면 --reload
```

production은 `.env`를 source하지 않고 service manager/Secret Manager가 환경을 주입한다.
포트 8765는 공개하지 않고 HTTPS LB/reverse proxy 뒤 private origin으로 배치한다.
`AUTH_TRUST_PROXY_HEADERS=true`는 origin 직접 접근이 차단된 경우에만 사용한다.

진입점:

| URL | 화면 |
|---|---|
| http://127.0.0.1:8765/ | 랜딩 |
| http://127.0.0.1:8765/auth/login | 로그인 |
| http://127.0.0.1:8765/auth/resend-verification | 이메일 인증 재전송 |
| http://127.0.0.1:8765/profile | 프로필·온톨로지·이용권 |
| http://127.0.0.1:8765/admin | 회원·권한·정책·결제 운영 콘솔 |
| http://127.0.0.1:8765/catalog | 데이터 마켓플레이스(카탈로그) |
| http://127.0.0.1:8765/charts | **Charts Studio** |
| http://127.0.0.1:8765/docs | Swagger (권한 계정용 읽기 전용 API 계약) |
| http://127.0.0.1:8765/health | 앱·인증 DB readiness |

기동 검증(권장): 최고관리자로 로그인한 동일 브라우저에서
`http://127.0.0.1:8765/charts?selftest=1` 접속 → 탭 제목이 `SELFTEST_ALL_PASS`면
레이아웃 CRUD·질의·온톨로지 폴백까지 전부 정상.

### 1-3. 환경변수

| 변수 | 기본값 | 용도 |
|---|---|---|
| `AUTH_ENV` | `development` | production 보안 검증 활성화 기준 |
| `AUTH_MODE` | `required` | `required`는 인증 필수, `local_auto`는 fail-closed 로컬 자동 회원 세션 |
| `AUTH_ALLOWED_HOSTS` | `127.0.0.1,localhost` | 허용 Host 헤더 목록 |
| `CHARTS_TRINO_URL` | `http://127.0.0.1:30586` | Trino 주소 |
| `CHARTS_TRINO_USER` | `charts-studio` | X-Trino-User 헤더 |
| `CHARTS_CACHE_TTL` | `600` (초) | 질의 결과 디스크 캐시 신선 기간 |
| `CHARTS_MAX_CONCURRENT_QUERIES` | `4` | 프로세스별 Trino live 질의 동시 실행 상한 |
| `DATABASE_URL` | `sqlite:///./data/ask_seoul.db` | 인증·권한·레이아웃 RDB |
| `AUTH_PUBLIC_BASE_URL` | `http://127.0.0.1:8765` | 쿠키/CSRF/이메일 링크 기준 origin |
| `AUTH_SESSION_PEPPER` | 개발만 자동 생성 | 운영 필수 secret |
| `AUTH_MFA_MASTER_KEY` | 개발은 pepper에서 파생 | 운영 TOTP seed 파생용 장기 secret |
| `AUTH_REQUIRE_MFA_FOR_PRIVILEGED` | production이면 true | 운영자·최고관리자 MFA 강제 |
| `AUTH_SESSION_IDLE_MINUTES` | `120` | 일반 세션 idle 만료 |
| `AUTH_REMEMBER_IDLE_DAYS` | `7` | 로그인 유지 세션 idle 만료 |
| `AUTH_MAX_SESSIONS_PER_USER` | `10` | 사용자별 활성 세션 상한 |
| `AUTH_MAX_REQUEST_BYTES` | `262144` | 요청 body 최대 byte |
| `AUTH_COOKIE_SECURE` | production이면 true | HTTPS 전용 세션 쿠키 |
| `SMTP_USE_TLS` / `SMTP_USE_SSL` | true / false | SMTP 암호화 방식. 동시에 true 금지 |
| `SMTP_ALLOW_PLAINTEXT` | false | 별도 보호된 내부 relay 예외만 명시적으로 허용 |

운영 전체 설정은 [additional_doc/auth/security-architecture.md](additional_doc/auth/security-architecture.md)를 따른다.
production은 `DATABASE_URL` 명시와 원격 RDB 인증서 hostname 검증을 필수로 한다.

---

## 2. 중지 (down)

```bash
# ③ 대시보드 서버 — 포그라운드면 Ctrl+C
# 백그라운드/production은 기동에 사용한 service manager에서 해당 unit만 중지한다.

# ① 데이터 스택 — sample/ 루트에서
docker compose stop        # 컨테이너 보존(권장 — 다음 up 이 빠르다)
docker compose down        # 컨테이너 제거(볼륨은 유지). -v 는 데이터 삭제이므로 쓰지 않는다
```

순서는 서버 먼저, 스택 나중이 안전하다(역순도 동작은 한다 — 서버는 Trino 다운 시 stale 캐시로 응답).

---

## 3. 초기화 / 리셋

| 하고 싶은 것 | 방법 |
|---|---|
| 특정 사용자의 레이아웃을 기본 시드로 되돌리기 | 운영 승인 후 해당 사용자의 `auth_dashboard_layouts` 행만 삭제. 다음 접근 때 `layouts.seed.json`을 다시 복제 |
| 질의 캐시 비우기 | 앱을 중지하고 `dashboard/app/charts/data/cache/`의 대상 파일을 확인한 뒤 그 내부 캐시만 삭제 |
| 카탈로그 메타 전체 갱신 | `python extract.py` → `snapshot/catalog_snapshot.json` 재생성 (스택 기동 + culture dbt manifest/catalog 전제) |
| basic domain 부분 갱신 | `python extract.py --refresh-basic-domain commerce`처럼 지정 domain만 현재 Trino에서 재측정. 비대상 domain과 `external` 분류 보존 |

> 참고: 레이아웃은 RDB 사용자 데이터다. 전체 DB 파일 삭제로 초기화하지 말고 대상 사용자 행을 좁혀 처리한다.
> `app/charts/data/cache/`는 계속 gitignore 대상 런타임 캐시이며, 커밋되는 기본 레이아웃은 시드뿐이다.

### 3-1. 정기 유지보수

로컬 셸에서는 먼저 `set -a; source .env; set +a`를 실행한다. cron/systemd와 production은
대상 앱과 같은 환경을 service manager/Secret Manager에서 주입해야 한다.

```bash
# 중단 후 남은 결제 알림 outbox 복구·전송
.venv/bin/python scripts/process_notifications.py --limit 100 --stale-minutes 5

# 만료 토큰·challenge·오래된 세션·사용된 복구 코드·종료 알림·만료 IP block 대상 건수만 확인
.venv/bin/python scripts/cleanup_auth.py

# 확인한 대상에 한해 실제 반영
.venv/bin/python scripts/cleanup_auth.py --apply
```

알림 처리는 1~5분 간격, 인증 임시데이터 정리는 일 1회부터 시작하고 실제 트래픽·보존 정책에 맞춰
조정한다. cleanup은 기본 dry-run이므로 출력 건수와 백업 상태를 확인한 뒤 `--apply`를 사용한다.

---

## 4. 장애 시 확인 순서

1. `/health` 200 및 `database=ok`인가 → 아니면 서버와 `DATABASE_URL` 연결부터 확인
2. 타일 배지가 `stale` 인가 → Trino 다운. `docker compose ps` / `curl :30586/v1/info`
3. 503 "Trino 접속 불가이고 캐시도 없습니다" → 스택 기동 후 타일의 ↻(다시 조회)
4. "데이터가 없습니다" → 차트 필터 값 확인 (연월 형식 `YYYY-MM` 등)
5. 새 gold 테이블/컬럼이 안 보임 → `extract.py` 로 스냅샷 갱신 (질의 자체는 cast 기반이라 낡은 스냅샷에도 안전)

---

## 5. dev/main 서버 자동 배포

`dashboard/` 저장소는 다음 경로로 독립 배포한다.

서버 소유자가 처음부터 실제 배포를 수행할 때는
[사람용 종단간 순차 실행서](deployment/end-to-end-human-runbook.md)를 사용한다. 설치 전제의
세부 판단은 [사람용 서버 준비 안내](deployment/server-setup-human.md), 대상 서버에서 AI가
점검·준비할 때는 권한과 금지 작업을 분리한
[서버 AI runbook](deployment/server-agent-runbook.md)을 먼저 적용한다.

이 절의 인증·PostgreSQL·배포 구성은 현재 작업 브랜치의 PR 범위다. 서버에 이미 설치된 구성이
아니며, PR이 `dev`에 merge되어 workflow가 처음 실행될 때 PostgreSQL 컨테이너와 영속 volume이
생성된다. merge 전에는 대상 서버를 변경하지 않는다.

```text
dev push  ─→ GitHub development Environment ─→ dev 서버
main push ─→ test + GHCR image build only ─→ 서버 배포 안 함
                  │
       test → GHCR SHA 이미지 → SSH
                              ├─일시 R2 secret → Trino health → 즉시 삭제
                              └─migration → Compose up → /health
                                                       └─실패 시 직전 앱 이미지 복구
```

- 이미지는 `ghcr.io/<owner>/<repo>:<40자리 commit SHA>`로 고정한다.
- 서버의 기존 runtime 설정과 인증 시크릿은 CI가 덮어쓰지 않는다. dev 최초 배포에서 파일이
  없을 때만 서버의 `bootstrap-dev.sh`가 난수 값을 생성한다.
- 앱 기동 전에 단일 `migrate` 컨테이너가 DB schema v6 계약을 적용한다.
- 앱 컨테이너는 production에서 DDL을 실행하지 않고 결과만 검증한다.
- 새 앱이 120초 안에 healthy가 되지 않으면 직전 Compose와 이미지로 자동 복구한다.
  additive DB migration 자체는 되돌리지 않으므로 main 배포 전 DB 백업은 별도로 보장해야 한다.
- 현재 `main`은 `main-deployment-disabled` job으로 배포 금지를 명시한다. production 서버 배포를
  시작하기 전에는 별도 검토 없이 이 조건을 제거하지 않는다.

### 5-1. 현재 dev 서버 결정

현재 dev는 공개 HTTPS 없이 SSH tunnel로만 접근한다.

| 항목 | 값 |
|---|---|
| SSH host | `exisnet.iptime.org` |
| SSH port / user | `3707` / `exi` |
| 권장 배포 경로 | `/home/exi/apps/ask-seoul-dashboard-dev` |
| 권장 runtime env | `/home/exi/.config/ask-seoul/dashboard-dev.env` |
| Compose project | `ask-seoul-dashboard-dev` |
| 앱 bind | 서버 `127.0.0.1:8765` |
| 서비스 DB | 전용 PostgreSQL 컨테이너 + `auth_postgres` volume |
| 차트 데이터 | `elt_net`의 최소 dev Trino companion → R2/Iceberg |

서버 아키텍처는 서버에서 확인한다.

```bash
uname -m
# x86_64       → amd64
# aarch64/arm64 → arm64
```

현재 workflow는 GitHub-hosted amd64 이미지 빌드이므로 서버가 arm64라면 배포 전에 build runner를
조정해야 한다.

dev runtime 파일은 [runtime.dev.env.example](../deploy/runtime.dev.env.example)을 계약으로 사용한다.
PR merge 후 첫 `dev` 배포 때 `bootstrap-dev.sh`가 PostgreSQL 비밀번호, session pepper, MFA master key를
서버 안에서 생성해 아래 경로에 mode `0600`으로 한 번만 저장하며 이후 배포에서는 덮어쓰지 않는다.
PostgreSQL은 호스트 port를 공개하지 않고 dashboard 전용 network에서만 접근한다.

```bash
stat -c '%a %n' /home/exi/.config/ask-seoul/dashboard-dev.env
# 600 /home/exi/.config/ask-seoul/dashboard-dev.env
```

배포 후 로컬 PC에서 tunnel을 열고 브라우저로 접속한다.

```bash
ssh -p 3707 \
  -L 8765:127.0.0.1:8765 \
  exi@exisnet.iptime.org

# tunnel을 유지한 상태에서
open http://127.0.0.1:8765
```

이 구조에서는 dashboard port를 공유기나 인터넷에 추가로 포워딩하지 않는다. SSH port `3707`만
기존 접근 통제와 fail2ban 등의 보호를 받는다.

### 5-2. 데이터 경로와 향후 전환 경계

현재 dashboard는 R2 자격증명을 직접 가지지 않는다.

```text
dashboard
  └─ CHARTS_TRINO_URL=http://trino:8080
       └─ iceberg_dev REST catalog
            ├─ R2_DEV_DATA_CATALOG_URI / WAREHOUSE / TOKEN
            └─ R2_DEV_ENDPOINT / ACCESS_KEY / SECRET_KEY
```

R2 connector 계약의 정본은 상위 `trino/catalog/iceberg_dev.properties`다. dev 서버 배포
secret의 정본은 GitHub `development` Environment이고, 상위 `sample/.env`는 로컬 데이터
스택 입력으로만 사용한다. dashboard snapshot의 relation도
`iceberg_dev.<schema>.<gold_table>`을 사용한다. 따라서 R2 endpoint, bucket, token,
access key를 dashboard runtime env에 복사하지 않는다.

대상 서버에 기존 Trino가 없으면 [deploy/trino/README.md](../deploy/trino/README.md)의
최소 companion을 workflow가 기동한다. R2 6개 값은 실행별 mode `0600` 임시 파일을 통해
Trino에만 주입하고 즉시 삭제한다. 전체 계약은
[R2/Trino secret 관리](deployment/r2-secret-management.md)를 따른다.

향후 데이터 제공 계층을 SQLite/D2로 바꾸는 작업은 `app/charts/trino.py`를 대체할 query adapter,
snapshot relation 계약, 동시성·캐시·백필 경로를 함께 변경해야 한다. 제품명과 접속 API가 확정되기
전까지 현재 Trino 경로를 유지한다.

### 5-3. production 활성화 시 추가 준비

현재 dev의 bundled PostgreSQL·SSH tunnel 구성에는 DB CA, Secret Manager, reverse proxy가
필요하지 않다.
나중에 main 배포를 활성화할 때 아래를 별도로 준비한다.

1. Secret Manager/에이전트가 [runtime.env.example](../deploy/runtime.env.example)을 바탕으로
   서버의 절대 경로에 runtime env 파일을 mode `0600`으로 렌더링하게 한다.
2. PostgreSQL CA 같은 공개 인증서 파일은 `<DEPLOY_PATH>/certificates/` 아래에 mode `0644`로 둔다.
3. `DASHBOARD_BIND_HOST=127.0.0.1`을 유지하고 같은 서버의 HTTPS reverse proxy로만 노출한다.
   reverse proxy가 다른 호스트/컨테이너라면 private bind 주소와 방화벽 범위를 별도로 정한다.

production runtime 파일의 필수 보안값은 다음과 같다.

- `AUTH_ENV=production`
- `AUTH_MODE=required`
- HTTPS `AUTH_PUBLIC_BASE_URL`, 정확한 `AUTH_ALLOWED_HOSTS`, 동일한 `HEALTHCHECK_HOST`
- `AUTH_COOKIE_SECURE=true`, `AUTH_REQUIRE_MFA_FOR_PRIVILEGED=true`
- 서로 다른 32자 이상 `AUTH_SESSION_PEPPER`, `AUTH_MFA_MASTER_KEY`
- `sslmode=verify-full`과 CA를 사용하는 `DATABASE_URL`
- 컨테이너에서 접근 가능한 `CHARTS_TRINO_URL`

같은 서버에서 상위 `sample/docker-compose.yml`의 Trino를 쓴다면 GitHub 변수
`DASHBOARD_DATA_NETWORK=elt_net`과 runtime 값 `CHARTS_TRINO_URL=http://trino:8080`을 사용한다.
다른 서버의 Trino를 쓴다면 `DASHBOARD_DATA_NETWORK`를 비우고 private URL을 직접 지정한다.

### 5-4. GitHub Environments

저장소 Settings → Environments에 `development`를 만들고 아래 값을 등록한다. `production`은
나중에 배포를 활성화할 때 만들며, 현재 workflow에서는 사용하지 않는다.

| 종류 | 이름 | 예시/용도 |
|---|---|---|
| Variable | `DEPLOY_HOST` | `exisnet.iptime.org` |
| Variable | `DEPLOY_PORT` | `3707` |
| Variable | `DEPLOY_USER` | `exi` |
| Variable | `DEPLOY_PATH` | `/home/exi/apps/ask-seoul-dashboard-dev` |
| Variable | `COMPOSE_PROJECT_NAME` | `ask-seoul-dashboard-dev` |
| Variable | `RUNTIME_ENV_FILE` | `/home/exi/.config/ask-seoul/dashboard-dev.env` |
| Variable | `DASHBOARD_DATA_NETWORK` | `elt_net` |
| Variable | `TRINO_DEPLOY_PATH` | `/home/exi/apps/ask-seoul-trino-dev` |
| Variable | `TRINO_COMPOSE_PROJECT_NAME` | `ask-seoul-trino-dev` |
| Secret | `SSH_PRIVATE_KEY` | 배포 전용 private key |
| Secret | `SSH_KNOWN_HOSTS` | 별도 채널에서 fingerprint를 검증한 대상 서버 host key |
| Secret | `R2_DEV_DATA_CATALOG_URI` | dev Iceberg REST catalog URI |
| Secret | `R2_DEV_DATA_CATALOG_WAREHOUSE` | dev Iceberg warehouse |
| Secret | `R2_DEV_DATA_CATALOG_TOKEN` | dev catalog OAuth2 token |
| Secret | `R2_DEV_ENDPOINT` | R2 S3 endpoint |
| Secret | `R2_DEV_ACCESS_KEY_ID` | R2 access key ID |
| Secret | `R2_DEV_SECRET_ACCESS_KEY` | R2 secret access key |

`SSH_KNOWN_HOSTS`는 `ssh-keyscan` 결과를 그대로 신뢰하지 말고 서버 콘솔의 fingerprint와
비교한 뒤 등록한다. SSH port가 `3707`이므로 known_hosts에는
`[exisnet.iptime.org]:3707` 형식으로 들어가야 한다.

GHCR push/pull은 workflow의 짧은 수명 `GITHUB_TOKEN`을 사용한다. Organization 정책에서
Actions의 package write 권한이 차단되어 있다면 이 저장소가 GHCR package를 만들고 읽을 수 있게
허용해야 한다.

R2 secret 최초 등록, 임시 파일 삭제, 회전·폐기 순서는
[R2/Trino secret 관리 계약](deployment/r2-secret-management.md)을 따른다.

### 5-5. 배포 후 최초 관리자

첫 배포와 `/health` 확인 뒤 서버의 보호된 TTY에서 실행한다. 아래 `-f` 뒤의 data network
override는 `DASHBOARD_DATA_NETWORK`를 설정한 환경에서만 붙인다.

```bash
cd <DEPLOY_PATH>

docker compose \
  --env-file <RUNTIME_ENV_FILE> \
  --env-file .deploy.env \
  --project-name <COMPOSE_PROJECT_NAME> \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec dashboard python scripts/create_admin.py

# 같은 명령 접두부로 MFA 등록
docker compose \
  --env-file <RUNTIME_ENV_FILE> \
  --env-file .deploy.env \
  --project-name <COMPOSE_PROJECT_NAME> \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec dashboard python scripts/setup_mfa.py
```

이메일·비밀번호·MFA seed·복구 코드는 GitHub Actions 로그나 채팅에 입력하지 않는다.
일상 상태 확인은 같은 Compose 접두부에 `ps`, 로그 확인은 `logs --tail 100 dashboard`를 붙인다.

서비스 DB 백업은 dashboard 이미지 배포와 분리한다.

```bash
docker compose \
  --env-file <RUNTIME_ENV_FILE> \
  --env-file .deploy.env \
  --project-name <COMPOSE_PROJECT_NAME> \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec -T postgres pg_dump -U ask_seoul -d ask_seoul -Fc > ask_seoul.dump
```
