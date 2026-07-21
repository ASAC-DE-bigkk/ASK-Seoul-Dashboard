# 로컬 데이터 분석 시작 가이드 — 사람용

대상: ASK SEOUL 데이터를 자기 PC에서 살펴보고 Catalog와 Charts Studio를 사용하려는 팀원.

이 가이드는 로그인·회원가입·관리자 승인을 거치지 않고 로컬 분석을 시작하는 가장 짧은 경로다.
배포된 `dev`와 향후 `main` 운영 서비스에는 적용되지 않으며, 두 환경은 계속 실제 인증을 요구한다.

## 1. 먼저 구분할 것

| 사용하려는 곳 | 인증 모드 | 로그인·회원가입 | 저장소 |
|---|---|---|---|
| 팀원 PC의 로컬 Dashboard | `AUTH_MODE=local_auto` | 생략 | 로컬 SQLite |
| `dev` 서버 | `AUTH_MODE=required` | 필수 | 서버 PostgreSQL |
| 향후 `main` 운영 서버 | `AUTH_MODE=required` | 필수 | 운영 RDB |

로컬 모드는 인증 코드를 제거하지 않는다. 최초 화면 접근 때 비밀번호가 없는 로컬 전용 일반 회원을
만들고, 기존과 동일한 DB 세션과 CSRF 쿠키를 자동 발급한다. 따라서 Charts 레이아웃도 정상적으로
사용자 ID에 귀속된다. 관리자 권한은 제공하지 않는다.

### 1-1. 로컬에서 사용하는 구조

```text
sample/docker-compose.yml의 Trino
        ↓ http://127.0.0.1:30586
dashboard/FastAPI
        ↓ http://127.0.0.1:8765
브라우저 Catalog·Charts
```

로컬에서는 상위 `sample/`이 Trino 설정과 실행을 소유하고 Dashboard는 그 Trino에 접속한다.
`dashboard/deploy/trino/`의 배포용 companion을 로컬에 또 띄우지 않는다. 두 번째 Trino를 만들면
30586 포트 충돌, 메모리 중복 사용, catalog·secret 설정 드리프트가 생길 수 있다.

로컬과 dev가 동일해야 하는 것은 컨테이너 배치가 아니라 데이터 계약이다. 두 환경 모두 같은 Trino
버전·catalog·`iceberg_dev.<schema>.<relation>`·질의 제한을 사용하되 접속 주소만 다음처럼 다르다.

| 환경 | Trino 소유·접속 | Dashboard 인증·DB |
|---|---|---|
| 로컬 | 상위 `sample/`, `http://127.0.0.1:30586` | `local_auto`, SQLite |
| dev | 서버 companion/shared Trino, `http://trino:8080` | `required`, PostgreSQL |
| 향후 main | 운영 private Trino endpoint | `required`, 운영 RDB |

## 2. 준비 사항

- Python 3.9 이상
- Python 패키지를 최초 한 번 설치할 수 있는 환경
- Charts 실데이터가 필요하면 Docker와 상위 `sample/` 데이터 스택 설정
- Dashboard는 반드시 자기 PC의 `127.0.0.1`에만 실행
- 상위 `sample/.env`에 팀에서 허가받은 R2 dev catalog·object storage 설정

R2/Iceberg 자격증명은 상위 `sample/.env`를 통해 Trino에만 주입한다.
`dashboard/.env.local`에 R2 token, access key, cookie 또는 실제 계정 비밀번호를 넣지 않는다.

### 2-1. Dashboard 코드 버전 확인

먼저 무엇을 시험할지 구분한다.

| 목적 | 사용할 코드 | 소스 갱신 |
|---|---|---|
| Dashboard에서 아직 작업 중인 변경 확인 | 현재 feature branch/working tree | 갱신 명령을 실행하지 않고 현재 checkout을 그대로 실행 |
| 팀에 병합된 공용 버전 확인 | Dashboard `main` | 아래 Dashboard 단독 갱신 실행 |

`sample/` 루트에서 현재 Dashboard 상태를 확인한다.

```bash
cd <프로젝트 루트>/sample
git -C dashboard status --short --branch
git submodule status dashboard
grep -q '^AUTH_MODE=local_auto$' dashboard/.env.local.example
```

Dashboard가 feature branch이거나 수정 파일이 있다면 현재 작업본을 실행한다. 이 상태에서 소스 갱신
명령으로 `main`을 checkout하지 않는다. 위 `grep`이 실패한다면 현재 작업본 자체에 로컬 분석 기능이
없는 것이므로 작업 소유자와 기대 revision을 먼저 확인한다.

팀에 이미 병합된 Dashboard `main`을 확인하려는 경우에만 작업 트리가 깨끗한지 확인한 뒤 상위
`sample/`의 선택 갱신 명령을 사용한다.

```bash
./scripts/update-nested-git.sh dashboard
git -C dashboard status --short --branch
```

이 명령은 `.gitmodules`에 지정된 Dashboard `main`만 fetch·fast-forward하며 dirty worktree에서는
중단한다. 일반 `git submodule update --init dashboard`를 반복 실행하면 상위 저장소가 고정한 과거
gitlink로 되돌아갈 수 있으므로 공용 최신 버전 확인 명령으로 사용하지 않는다.

갱신 후 상위 `sample`의 `git status`에 `M dashboard`가 보일 수 있다. 이는 로컬 Dashboard revision과
고정 gitlink가 다르다는 뜻이며 정상이다. 로컬 실행마다 상위 gitlink PR을 만들 필요는 없다. gitlink는
통합 검증한 revision을 상위 저장소의 기본 상태로 승격할 때만 별도 이슈/PR로 갱신한다.

## 3. 처음 실행하기

### 3-1. Trino 기동 — Charts 실데이터를 볼 때만

작업 위치: `sample/` 루트

Charts 실데이터를 읽으려면 `sample/.env`에 다음 이름의 값이 설정되어 있어야 한다. 실제 값은 문서,
터미널 출력, Dashboard 환경 파일에 복사하지 않는다.

```text
R2_DEV_ENDPOINT
R2_DEV_ACCESS_KEY_ID
R2_DEV_SECRET_ACCESS_KEY
R2_DEV_DATA_CATALOG_TOKEN
R2_DEV_DATA_CATALOG_URI
R2_DEV_DATA_CATALOG_WAREHOUSE
```

```bash
cd <프로젝트 루트>/sample
docker compose up -d trino
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info
docker compose exec trino trino --execute 'SELECT 1'
docker compose exec trino trino --execute 'SHOW SCHEMAS FROM iceberg_dev'
docker compose exec trino trino --execute 'SHOW TABLES FROM iceberg_dev.commerce'
```

`trino`가 healthy이고 `/v1/info`가 JSON을 반환하며 `SELECT 1`이 성공하면 query engine은 정상이다.
`SHOW SCHEMAS/TABLES`까지 성공해야 R2 dev catalog와 commerce 데이터 연결이 확인된다. Catalog 화면과
로그인 관련 화면만 확인한다면 Trino 없이 Dashboard를 먼저 기동할 수 있다.

### 3-2. Dashboard 준비와 실행

작업 위치: `sample/dashboard/`

```bash
cd <프로젝트 루트>/sample/dashboard
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

test -f .env.local || cp .env.local.example .env.local
set -a
source .env.local
set +a

.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

이미 `.venv`와 `.env.local`이 있으면 생성·복사 명령은 다시 실행하지 않아도 된다.
서버를 다시 여는 새 터미널에서는 `source .env.local` 단계부터 다시 실행한다.

### 3-3. 두 번째 실행부터

터미널 A에서 상위 Trino를 확인하거나 다시 기동한다.

```bash
cd <프로젝트 루트>/sample
docker compose up -d trino
docker compose ps trino
```

터미널 B에서 Dashboard 환경을 다시 로드하고 앱을 실행한다. `source`로 설정한 환경변수는 새 터미널에
자동 승계되지 않는다.

```bash
cd <프로젝트 루트>/sample/dashboard
set -a
source .env.local
set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

## 4. 접속과 정상 동작 확인

브라우저 주소는 `localhost`와 섞지 말고 아래처럼 `127.0.0.1`로 통일한다.

| 화면 | 주소 | 로컬 분석 모드의 결과 |
|---|---|---|
| 랜딩 | `http://127.0.0.1:8765/` | 로컬 일반 회원으로 자동 인식 |
| Catalog | `http://127.0.0.1:8765/catalog` | 로그인 화면 없이 접근 |
| Charts Studio | `http://127.0.0.1:8765/charts` | 로그인 화면 없이 접근, 레이아웃 저장 가능 |
| Charts 셀프테스트 | `http://127.0.0.1:8765/charts?selftest=1` | 전체 통과 시 탭 제목 `SELFTEST_ALL_PASS` |
| 프로필 | `http://127.0.0.1:8765/profile` | 로컬 계정 상태 확인 가능 |
| 관리자 | `http://127.0.0.1:8765/admin` | 접근 거부가 정상 |
| 상태 확인 | `http://127.0.0.1:8765/health` | 앱·인증 DB readiness |

서버 상태도 확인한다.

```bash
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8765/api/v1/public/summary
```

정상 기준:

1. `/health`가 HTTP 200이고 `database`가 `ok`다.
2. `/charts` 접근 때 로그인·회원가입 화면으로 이동하지 않는다.
3. 화면의 사용자는 `local-analyst`, 역할은 `일반회원`이다.
4. 차트 페이지 편집·저장 후 새로고침해도 레이아웃이 유지된다.
5. `/admin`은 열리지 않는다.
6. `charts?selftest=1` 완료 후 탭 제목이 `SELFTEST_ALL_PASS`다.

## 5. 자동으로 만들어지는 것

| 항목 | 위치 또는 값 | 성격 |
|---|---|---|
| 로컬 인증 DB | `data/ask_seoul.local.db` | 개인 PC의 런타임 파일, Git 커밋 금지 |
| 로컬 계정 | `local-analyst@localhost.invalid` | 실제 이메일이 아닌 예약 식별자 |
| 역할 | `member` | Catalog·Charts 사용 가능, 관리자 권한 없음 |
| 세션 | DB에는 HMAC hash만 저장 | 일반 로그인과 같은 만료·CSRF 규칙 적용 |
| 레이아웃 | 로컬 DB에서 위 사용자 ID에 귀속 | 다른 팀원 PC나 dev 서버와 공유되지 않음 |
| Charts 캐시 | `app/charts/data/cache/` | 런타임 파일, Git 커밋 금지 |

로컬 레이아웃을 팀원 전체의 기본 화면으로 승격하려면 개인 DB를 공유하지 말고,
검토된 결과만 `app/charts/data/layouts.seed.json` 변경으로 별도 작업한다.

## 6. 실제 인증 흐름으로 전환하기

로그인·회원가입·승인·MFA를 개발하거나 검증할 때는 로컬 자동 모드를 사용하지 않는다.

1. 현재 Uvicorn을 `Ctrl+C`로 중지한다.
2. 새 터미널을 연다.
3. `.env.example`을 복사한 `.env`를 로드한다.
4. `AUTH_MODE=required`인지 확인한다.
5. 관리자 생성·MFA 등록 절차를 진행한다.

전체 절차는 [개발 실행·초기 최고관리자 문서](maintanance/README.md#실제-인증-흐름-개발-빠른-시작)를
따른다. `.env.local`의 SQLite와 `.env`의 인증 DB는 분리하므로 로컬 분석 레이아웃을 실제 사용자
레이아웃으로 자동 이관하지 않는다.

## 7. 자주 발생하는 문제

| 증상 | 원인과 해결 |
|---|---|
| 로그인 화면으로 이동함 | `.env.local`을 현재 셸에 로드했는지, `AUTH_MODE=local_auto`인지 확인하고 서버를 재기동한다. |
| `403 origin mismatch` | `localhost:8765` 대신 설정과 같은 `http://127.0.0.1:8765`를 사용한다. 쿠키나 계정을 먼저 변경하지 않는다. |
| `local_auto는 ... 사용할 수 없습니다`로 기동 실패 | 오류에 표시된 조건을 고친다. development, SQLite, loopback HTTP/Host, proxy header 미신뢰가 모두 필요하다. |
| `/admin`이 열리지 않음 | 정상이다. 로컬 분석 계정은 일반 회원이며 관리 권한을 갖지 않는다. |
| Charts가 503 또는 stale을 표시함 | 인증 문제가 아니라 Trino/R2 경로 문제일 수 있다. 상위 `sample/`에서 Trino health와 `SELECT 1`을 확인한다. |
| 저장한 레이아웃이 다른 PC/dev에 없음 | 정상이다. 로컬 SQLite 사용자 데이터는 PC별로 분리된다. |
| 실제 가입 흐름을 시험할 수 없음 | 서버를 중지하고 `AUTH_MODE=required`인 `.env`로 새로 기동한다. |

## 8. 안전하게 종료하기

1. Dashboard를 실행한 터미널에서 `Ctrl+C`로 Uvicorn을 먼저 중지한다.
2. `sample/` 루트에서 Dashboard가 사용한 Trino만 중지한다.

```bash
cd <프로젝트 루트>/sample
docker compose stop trino
```

컨테이너를 보존하므로 다음 `docker compose up -d trino`가 빠르다. 로컬 인증 DB와 Charts 캐시는
Dashboard 쪽 런타임 파일이므로 Trino 중지와 함께 삭제되지 않는다.

## 9. 안전 수칙

- Uvicorn을 `0.0.0.0`에 bind하지 않는다.
- 로컬 모드를 reverse proxy, SSH 공유 서버, dev/main 서버에 적용하지 않는다.
- `.env.local`, `data/`, 세션 cookie, 실제 자격증명을 커밋하거나 공유하지 않는다.
- dev/main runtime 파일에 `AUTH_MODE=local_auto`를 넣지 않는다. 배포 스크립트도 이를 거부한다.
- `docker compose down -v`, `docker volume rm`, `docker volume prune`을 로컬 종료 명령으로 사용하지 않는다.
- 로컬 DB 전체 삭제로 레이아웃 하나를 초기화하지 않는다. 먼저 Charts 화면의 대상 페이지 기능을 사용한다.
- 전체 로컬 DB를 새로 시작해야 한다면 서버를 중지하고 정확한 DB 파일을 별도 백업 경로로 이동한 뒤 진행한다.

더 자세한 인증 보안 계약은
[인증 보안 아키텍처](additional_doc/auth/security-architecture.md), 전체 기동·중지는
[운영 매뉴얼](operations.md)을 따른다.
