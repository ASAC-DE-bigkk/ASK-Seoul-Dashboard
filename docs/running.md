# 실행 매뉴얼 — 환경별 대시보드 기동

대시보드(카탈로그 + Charts Studio)를 **환경별로 기동하는 방법의 정본**이다. 로컬은 로그인
없이(운영자 자동), 개발은 실제 로그인(required)으로 뜬다. 운영(prod) 서버 배포는 범위 밖이며
[deployment/](deployment/README.md) 문서를 따른다.

문제 해결은 [local-analysis-human-guide.md §7](local-analysis-human-guide.md#7-자주-발생하는-문제)를,
기동/중지/초기화 전반은 [operations.md](operations.md)를 함께 본다.

---

## 0. 어떤 방법을 쓸까 (결정표)

| 환경·방법 | 목적 | 실행 | 로그인 | Trino | 인증 DB |
|---|---|---|---|---|---|
| **로컬 · 호스트** | 코드 수정·핫리로드 | `scripts/run_local.ps1` (호스트 uvicorn) | 없음 — 운영자 자동 | 상위 `sample/` (127.0.0.1:30586) | SQLite(로컬 파일) |
| **로컬 · Docker** | 격리·한 방 기동 | `deploy/compose.local.yaml` | 없음 — 운영자 자동 | 상위 `sample/` (host.docker.internal:30586) | SQLite(컨테이너 볼륨) |
| **개발 · Docker** | 실제 로그인·회원 흐름 검증 | `deploy/compose.dev.yaml` (+ `deploy/trino`) | 필요 — 부트스트랩 관리자 | 자체 compose(`deploy/trino`) | PostgreSQL(컨테이너) |
| 운영(prod) | 서버 배포 | `deploy/compose.yaml`(이미지 기반) — [deployment/](deployment/README.md) | 필요 | 서버 companion | PostgreSQL |

- **로컬 계정**: `local-analyst@localhost.invalid` 예약 계정이 **운영자(operator)** 로 자동 생성·로그인된다.
  관리 콘솔과 '권한별 화면 관리'(게스트/멤버 화면 미리보기)를 로컬에서 그대로 쓸 수 있고,
  최고관리자 전용 기능만 제한된다.
- **카탈로그는 Trino 없이도** 동작한다(커밋된 스냅샷). 실 Charts 데이터에만 Trino가 필요하다.

---

## 1. 사전 요건

| 대상 | 요건 |
|---|---|
| 호스트 실행 | Python **안정 버전(3.13 권장)** — 3.14 등 최신은 pydantic_core 휠 부재 가능 |
| Docker 실행 | Docker Desktop / Docker Engine |
| 실 Charts 데이터 | Trino 기동 + `sample/.env`(또는 `deploy/trino` env)에 R2 dev 값 |
| 서브모듈 | 상위 `sample/` 클론 시 dashboard가 최신 `dev`인지 확인(`git -C dashboard switch dev`) |

R2 dev 값(레포 미포함, 팀에서 받아 채운다): `R2_DEV_ENDPOINT / _ACCESS_KEY_ID / _SECRET_ACCESS_KEY /
_DATA_CATALOG_URI / _WAREHOUSE / _TOKEN`. 상세는 상위 `sample/setup-values-guide.md`.

---

## 2. 로컬 · 호스트 (로그인 없이, 핫리로드)

Trino(실데이터용, 선택)를 먼저 상위 `sample/`에서 띄운다.

```bash
cd <프로젝트 루트>/sample && docker compose up -d trino
```

**Windows PowerShell**

```powershell
cd <프로젝트 루트>\sample\dashboard
py -3.13 -m venv .venv                          # 최초 1회 (안정 버전)
.venv\Scripts\pip install -r requirements.txt   # 최초 1회
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
```

**macOS/Linux · Git Bash**

```bash
cd <프로젝트 루트>/sample/dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env.local || cp .env.local.example .env.local
set -a; source .env.local; set +a
.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

→ http://127.0.0.1:8765/charts (운영자로 자동 진입). 셸별 대응·주의는 [SHARE.md §0.1](../SHARE.md).

---

## 3. 로컬 · Docker (로그인 없이, 격리·한 방)

Trino(실데이터용, 선택)를 상위 `sample/`에서 먼저 띄운다.

```bash
cd <프로젝트 루트>/sample && docker compose up -d trino    # ①
cd dashboard
docker compose -f deploy/compose.local.yaml up --build      # ②
# → http://127.0.0.1:8765/charts (운영자 자동 진입)
```

- 비밀값 불필요(세션 pepper는 컨테이너 `data` 볼륨에 자동 생성). 선택 오버라이드는
  `deploy/env/local.env.example`(포트·Trino URL 등).
- 컨테이너 안 앱은 클라이언트 IP가 Docker 게이트웨이(사설)로 보이므로 자동 로그인을 위해
  `AUTH_LOCAL_AUTO_CLIENT_CIDRS`(사설 대역)를 신뢰한다. **반드시 127.0.0.1 에만 publish**하며
  이 전제에서만 안전하다(§6 보안 주의).
- 중지: `docker compose -f deploy/compose.local.yaml down` (볼륨까지 지우려면 `-v`).

---

## 4. 개발 · Docker (실제 로그인, PostgreSQL)

Trino를 **별도 compose**로 배포하고(elt_net 생성), 앱+DB를 올린다.

```bash
cd <프로젝트 루트>/sample/dashboard
cp deploy/env/dev.env.example  deploy/env/dev.env     # 관리자·postgres·pepper 채우기
cp deploy/trino/runtime.env.example deploy/env/trino.env   # R2 dev 값 채우기

docker compose -f deploy/trino/compose.yaml   --env-file deploy/env/trino.env up -d   # ① Trino
docker compose -f deploy/compose.dev.yaml     --env-file deploy/env/dev.env   up --build # ② 앱+DB
# → http://127.0.0.1:8765  (dev.env의 부트스트랩 관리자로 로그인)
```

- 앱은 `elt_net`에 external로 참가해 `http://trino:8080`으로 질의한다.
- `deploy/env/*.env`(실제 값)는 `.gitignore` 대상 — `*.example`만 커밋한다.
- 실제 회원가입·승인·MFA 흐름 개발은 [maintanance/README.md](maintanance/README.md)를 함께 본다.

---

## 5. 접속·정상 동작 확인

| 화면 | 주소 | 로컬(운영자) | 개발(로그인 후) |
|---|---|---|---|
| 카탈로그 | `/catalog` | 로그인 없이 접근 | 권한대로 |
| Charts Studio | `/charts` | 로그인 없이 접근·편집 | 권한대로 |
| 운영 콘솔 | `/admin` | 접근 가능(운영자) | 운영자+만 |
| 상태 | `/health` | 200 · `database=ok` | 200 |

```bash
curl -fsS http://127.0.0.1:8765/health
# 세션 역할 확인(로컬은 operator):
curl -fsS http://127.0.0.1:8765/api/v1/auth/session
```

`localhost` 와 `127.0.0.1` 어느 쪽으로 접속해도 된다(로컬 모드는 두 loopback host를 모두 허용).

---

## 6. 보안 주의 (로컬 Docker)

- 로컬 Docker 앱은 반드시 **`127.0.0.1` 에만 publish**한다. `0.0.0.0` 으로 바꾸면
  `AUTH_LOCAL_AUTO_CLIENT_CIDRS` 신뢰가 원격 자동 로그인으로 악용될 수 있다.
- `AUTH_LOCAL_AUTO_CLIENT_CIDRS`는 사설/loopback 대역만 허용된다(공인 IP는 기동 거부).
- Trino 이중 기동 금지: 상위 `sample/`의 Trino와 `deploy/trino`의 Trino를 **동시에** 띄우면
  30586 충돌·메모리 중복이다. 한쪽만 쓴다.
- `.env.local`·`data/`·세션 쿠키·실제 자격증명은 커밋·공유하지 않는다.

---

## 7. 중지·정리

| 방법 | 중지 |
|---|---|
| 로컬 호스트 | 실행 터미널에서 `Ctrl+C`. Trino는 상위 `sample/`에서 `docker compose stop trino` |
| 로컬 Docker | `docker compose -f deploy/compose.local.yaml down` (`-v` 볼륨 삭제) |
| 개발 Docker | `docker compose -f deploy/compose.dev.yaml down` + `-f deploy/trino/compose.yaml down` |

`docker compose down -v`, `docker volume prune`은 데이터 손실이 있으니 로컬 종료 관용 명령으로 쓰지 않는다.
