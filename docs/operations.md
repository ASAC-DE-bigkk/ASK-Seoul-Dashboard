# 운영 매뉴얼 — 솔루션 기동(up) / 중지(down)

대상: 이 데모(카탈로그 + Charts Studio)를 켜고 끄고 초기화하는 사람.
구성 요소는 둘뿐이다 — **① 데이터 스택(docker compose, sample/ 루트)** 과 **② 대시보드 서버(FastAPI, 이 리포)**.

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
python3.12 -m venv .venv                      # macOS/Linux (Windows: py -3.12 -m venv .venv)
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
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
| http://127.0.0.1:8765/catalog | 데이터 마켓플레이스(카탈로그) |
| http://127.0.0.1:8765/charts | **Charts Studio** |
| http://127.0.0.1:8765/docs | Swagger (전체 API) |
| http://127.0.0.1:8765/health | 헬스 체크 |

기동 검증(권장): 브라우저로 `http://127.0.0.1:8765/charts?selftest=1` 접속 →
탭 제목이 `SELFTEST_ALL_PASS` 면 레이아웃 CRUD·질의·온톨로지 폴백까지 전부 정상.

### 1-3. 환경변수 (선택 — 기본값으로 충분)

| 변수 | 기본값 | 용도 |
|---|---|---|
| `CHARTS_TRINO_URL` | `http://127.0.0.1:30586` | Trino 주소 |
| `CHARTS_TRINO_USER` | `charts-studio` | X-Trino-User 헤더 |
| `CHARTS_CACHE_TTL` | `600` (초) | 질의 결과 디스크 캐시 신선 기간 |

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
| 레이아웃 페이지를 기본 시드 3페이지로 되돌리기 | `rm app/charts/data/layouts.json` 후 서버 재시작(시드 `layouts.seed.json` 이 복사됨) |
| 질의 캐시 비우기 | `rm -r app/charts/data/cache/` |
| 카탈로그 메타(소스 목록·스키마) 갱신 | `python extract.py` → `snapshot/catalog_snapshot.json` 재생성 (스택 기동 + dbt manifest 전제) |

> 참고: `layouts.json`·`cache/` 는 gitignore 대상 런타임 파일이다. 커밋되는 것은 시드뿐이다.

---

## 4. 장애 시 확인 순서

1. `/health` 200 인가 → 아니면 서버부터 (uvicorn 로그 확인)
2. 타일 배지가 `stale` 인가 → Trino 다운. `docker compose ps` / `curl :30586/v1/info`
3. 503 "Trino 접속 불가이고 캐시도 없습니다" → 스택 기동 후 타일의 ↻(다시 조회)
4. "데이터가 없습니다" → 차트 필터 값 확인 (연월 형식 `YYYY-MM` 등)
5. 새 gold 테이블/컬럼이 안 보임 → `extract.py` 로 스냅샷 갱신 (질의 자체는 cast 기반이라 낡은 스냅샷에도 안전)
