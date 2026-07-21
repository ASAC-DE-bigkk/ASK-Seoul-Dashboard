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

## 2. 준비 사항

- Python 3.9 이상
- Python 패키지를 최초 한 번 설치할 수 있는 환경
- Charts 실데이터가 필요하면 Docker와 상위 `sample/` 데이터 스택 설정
- Dashboard는 반드시 자기 PC의 `127.0.0.1`에만 실행

R2/Iceberg 자격증명은 상위 `sample/.env`를 통해 Trino에만 주입한다.
`dashboard/.env.local`에 R2 token, access key, cookie 또는 실제 계정 비밀번호를 넣지 않는다.

## 3. 처음 실행하기

### 3-1. Trino 기동 — Charts 실데이터를 볼 때만

작업 위치: `sample/` 루트

```bash
cd <프로젝트 루트>/sample
docker compose up -d trino
docker compose ps trino
curl -fsS http://127.0.0.1:30586/v1/info
```

`trino`가 healthy이고 마지막 명령이 JSON을 반환하면 된다. Catalog 화면과 로그인 관련 화면만
확인한다면 Trino 없이 Dashboard를 먼저 기동할 수 있다.

### 3-2. Dashboard 준비와 실행

작업 위치: `sample/dashboard/`

```bash
cd <프로젝트 루트>/sample/dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

test -f .env.local || cp .env.local.example .env.local
set -a
source .env.local
set +a

.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

이미 `.venv`와 `.env.local`이 있으면 생성·복사 명령은 다시 실행하지 않아도 된다.
서버를 다시 여는 새 터미널에서는 `source .env.local` 단계부터 다시 실행한다.

## 4. 접속과 정상 동작 확인

브라우저 주소는 `localhost`와 섞지 말고 아래처럼 `127.0.0.1`로 통일한다.

| 화면 | 주소 | 로컬 분석 모드의 결과 |
|---|---|---|
| 랜딩 | `http://127.0.0.1:8765/` | 로컬 일반 회원으로 자동 인식 |
| Catalog | `http://127.0.0.1:8765/catalog` | 로그인 화면 없이 접근 |
| Charts Studio | `http://127.0.0.1:8765/charts` | 로그인 화면 없이 접근, 레이아웃 저장 가능 |
| 프로필 | `http://127.0.0.1:8765/profile` | 로컬 계정 상태 확인 가능 |
| 관리자 | `http://127.0.0.1:8765/admin` | 접근 거부가 정상 |

서버 상태도 확인한다.

```bash
curl -fsS http://127.0.0.1:8765/health
```

정상 기준:

1. `/health`가 HTTP 200이고 `database`가 `ok`다.
2. `/charts` 접근 때 로그인·회원가입 화면으로 이동하지 않는다.
3. 화면의 사용자는 `local-analyst`, 역할은 `일반회원`이다.
4. 차트 페이지 편집·저장 후 새로고침해도 레이아웃이 유지된다.
5. `/admin`은 열리지 않는다.

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

## 8. 안전 수칙

- Uvicorn을 `0.0.0.0`에 bind하지 않는다.
- 로컬 모드를 reverse proxy, SSH 공유 서버, dev/main 서버에 적용하지 않는다.
- `.env.local`, `data/`, 세션 cookie, 실제 자격증명을 커밋하거나 공유하지 않는다.
- dev/main runtime 파일에 `AUTH_MODE=local_auto`를 넣지 않는다. 배포 스크립트도 이를 거부한다.
- 로컬 DB 전체 삭제로 레이아웃 하나를 초기화하지 않는다. 먼저 Charts 화면의 대상 페이지 기능을 사용한다.
- 전체 로컬 DB를 새로 시작해야 한다면 서버를 중지하고 정확한 DB 파일을 별도 백업 경로로 이동한 뒤 진행한다.

더 자세한 인증 보안 계약은
[인증 보안 아키텍처](additional_doc/auth/security-architecture.md), 전체 기동·중지는
[운영 매뉴얼](operations.md)을 따른다.
