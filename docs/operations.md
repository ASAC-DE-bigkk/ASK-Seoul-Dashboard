# 운영 매뉴얼 — 솔루션 기동(up) / 중지(down)

대상: 이 데모(카탈로그 + Charts Studio)를 켜고 끄고 초기화하는 사람.
구성 요소는 **① 데이터 스택(docker compose)**, **② 인증/권한 RDB**, **③ 대시보드 서버(FastAPI)**다.

```
[R2/Iceberg gold] ←질의─ Trino(:30586) ←─ ② FastAPI(:8765) ─→ 브라우저 (/charts, /catalog)
        ▲                                        │
   Airflow DAG(적재)                     snapshot/catalog_snapshot.json (카탈로그 메타)
```

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

기동 확인:

```bash
docker compose ps                                   # trino 가 healthy 인지
curl -s http://127.0.0.1:30586/v1/info | head -c 80  # {"nodeId":...,"state":"ACTIVE"...}
```

| 서비스 | 포트 | Charts Studio 와의 관계 |
|---|---|---|
| trino | 127.0.0.1:30586 | **필수** — gold 집계 질의 대상 |
| airflow-apiserver | 127.0.0.1:30585 | 선택 — gold 를 갱신하는 적재 파이프라인 |
| serving-postgres / marquez | 30587 / 3000 | 무관 (타 워크로드) |

### 1-2. 대시보드 서버

최초 1회 (가상환경):

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

# 권한 계정이 인증 앱과 복구 코드를 모두 분실한 break-glass 상황에서만 사용
.venv/bin/python scripts/setup_mfa.py --reset-existing
```

기동:

```bash
.venv/bin/uvicorn app.main:app --port 8765            # Windows: .venv\Scripts\uvicorn
# 개발 중이면 --reload 를 붙인다
```

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

기동 검증(권장): 브라우저로 `http://127.0.0.1:8765/charts?selftest=1` 접속 →
탭 제목이 `SELFTEST_ALL_PASS` 면 레이아웃 CRUD·질의·온톨로지 폴백까지 전부 정상.

### 1-3. 환경변수

| 변수 | 기본값 | 용도 |
|---|---|---|
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
# ② 대시보드 서버 — 포그라운드면 Ctrl+C, 백그라운드면:
kill $(lsof -ti :8765)

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
| 질의 캐시 비우기 | `rm -r app/charts/data/cache/` |
| 카탈로그 메타(소스 목록·스키마) 갱신 | `python extract.py` → `snapshot/catalog_snapshot.json` 재생성 (스택 기동 + dbt manifest 전제) |

> 참고: 레이아웃은 RDB 사용자 데이터다. 전체 DB 파일 삭제로 초기화하지 말고 대상 사용자 행을 좁혀 처리한다.
> `app/charts/data/cache/`는 계속 gitignore 대상 런타임 캐시이며, 커밋되는 기본 레이아웃은 시드뿐이다.

### 3-1. 정기 유지보수

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
