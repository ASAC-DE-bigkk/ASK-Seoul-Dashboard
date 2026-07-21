# Data Catalog — Marketplace 데모

Snowflake Marketplace 스타일의 **데이터 카탈로그 API + 웹화면** 데모.
W3 "품질·카탈로그 API화"의 축소판 — **전 도메인 gold** 를 싣는다 (7/15 확장).

도메인 2계층:

| 계층 | 도메인 | 메타 수준 |
|---|---|---|
| rich | culture | dbt manifest/catalog 보유 → 설명·contract·계보·quality_status까지 |
| basic | commerce · traffic · weather · citydata | Trino 실측만 (스키마·행수·기간·샘플) — dbt 아티팩트 미제공이라 설명/계약/계보 공란 |

basic 도메인의 빈 칸이 곧 "메타데이터 채무" 가시화다 — W3 본작업에서
각 도메인이 dbt 아티팩트를 등록하면 자동으로 rich 로 승격되는 구조.

사상: **계산은 파이프라인이 미리, API는 얇게.** 요청 시 Trino를 두드리지 않고,
추출기가 박제한 스냅샷 JSON만 서빙한다 (조회 전용 서빙 레이어).

```
extract.py ──(dbt manifest/catalog.json + Trino 실측)──▶ snapshot/catalog_snapshot.json
app/main.py(FastAPI) ──▶ /api/v1/catalog/... + / (마켓플레이스 화면) + /docs (Swagger)
```

## 실행

### 팀원 로컬 분석 — 로그인 생략

사람이 따라 하는 전체 순서와 문제 해결은
[로컬 데이터 분석 시작 가이드](docs/local-analysis-human-guide.md)를 정본으로 사용한다.
AI에게 준비·검증을 맡길 때는
[로컬 분석 AI runbook](docs/local-analysis-agent-runbook.md)을 함께 전달한다.

> 실행 환경은 **Windows(PowerShell)와 macOS/Linux(bash) 둘 다** 지원한다. 아래 두 절차 중
> 자기 환경의 것을 사용한다. 앱은 `.env` 파일을 자동 로드하지 않으므로 `.env.local` 주입은
> 실행 셸이 담당한다 — bash 예시를 PowerShell에 그대로 붙여넣으면 `AUTH_MODE`가 주입되지 않아
> 로그인 화면으로 떨어진다.

**macOS/Linux · Git Bash**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f .env.local || cp .env.local.example .env.local
set -a; source .env.local; set +a
.venv/bin/python scripts/init_auth_db.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
# → http://127.0.0.1:8765/charts
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
pwsh scripts/run_local.ps1
# run_local.ps1 이 .env.local 생성·로드 → 인증 DB 초기화 → uvicorn 기동을 일괄 수행
# → http://127.0.0.1:8765/charts
```

`AUTH_MODE=local_auto`는 loopback에서 최초 화면을 열 때 로컬 SQLite에 일반 회원을 만들고
정상 세션·CSRF 쿠키를 자동 발급한다. 관리자 권한은 없으며 레이아웃은 로컬 사용자 ID로 분리된다.
Charts 실데이터가 필요하면 상위 `sample/`에서 Trino를 먼저 기동한다.

### 실제 로그인·회원가입 흐름 개발

```bash
# 1) 설치
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # 값을 편집한 뒤 shell에 로드. 운영 시크릿은 secret manager에서 주입
set -a; source .env; set +a

# 2) 인증 DB 초기화·최초 관리자
.venv/bin/python scripts/init_auth_db.py
.venv/bin/python scripts/create_admin.py
.venv/bin/python scripts/setup_mfa.py

# 3) 스냅샷 갱신 (전제: sample/ 스택 기동 + dbt target/ 에 manifest·catalog.json)
.venv/bin/python extract.py

# 4) 서버
.venv/bin/uvicorn app.main:app --port 8765
# → http://127.0.0.1:8765  (화면) · /docs (Swagger) · /health
```

## 서버 배포

`dev` push 시 GitHub Actions가 테스트 후 commit SHA 기반 이미지를 GHCR에 올리고,
`development` Environment에 연결된 서버에서 `deploy/compose.yaml`을 적용한다. DB migration은
앱 기동 전 단일 job으로 실행하며, `/health` 실패 시 직전 이미지로 자동 복구한다.
`main`은 테스트와 이미지 발행까지만 수행하고 서버 배포는 의도적으로 비활성화되어 있다.

배포된 서비스 DB는 외부에 port를 열지 않는 전용 PostgreSQL과 영속 volume을 사용한다.
Charts 데이터는 dashboard가 R2 자격증명을 갖지 않고 `elt_net`의 dev Trino companion이
`iceberg_dev` R2/Iceberg catalog를 조회하는 구조를 유지한다.
현재 작업 브랜치가 PR을 통해 `dev`에 merge되기 전에는 서버에 PostgreSQL이나 dashboard
컨테이너를 생성하지 않는다.

실제 배포는 접속 PC·GitHub 웹·대상 서버의 위치를 분리한
[사람용 종단간 순차 실행서](docs/deployment/end-to-end-human-runbook.md)를 위에서 아래로
따른다. 각 전제의 상세 설명은 [사람용 서버 준비 안내](docs/deployment/server-setup-human.md),
서버 안의 AI에게 점검을 맡길 때는
[서버 AI runbook](docs/deployment/server-agent-runbook.md)을 사용한다.
R2 값의 GitHub Environment 정본·일시 주입·회전은
[R2/Trino secret 관리 계약](docs/deployment/r2-secret-management.md)을 따른다.
배포 동작의 상세 계약은
[운영 매뉴얼의 dev/main 서버 자동 배포](docs/operations.md#5-devmain-서버-자동-배포)에 있다.

## 인증·회원·권한

- 익명은 랜딩·로그인·가입·비밀번호 재설정만 접근한다.
- 팀원 로컬 분석에만 `AUTH_MODE=local_auto`를 사용할 수 있고, 배포 dev/main은
  `AUTH_MODE=required`로 로그인·회원가입을 강제한다.
- 게스트의 기본 데이터 화면은 Catalog까지이며 Charts 레이아웃을 변경할 수 없다. 일반회원은
  Charts에 진입해 자신의 레이아웃만 조회·추가·수정·삭제할 수 있고 로컬 예약 member도 같은 계약을 사용한다.
- 게스트/일반회원/운영자/최고관리자 역할, 역할 기본 페이지 권한, 사용자 allow/deny override를 지원한다.
- 이메일 인증 또는 관리자 승인, Argon2id 비밀번호, DB 세션·CSRF, 계정 잠금, 요청 제한을 적용한다.
- 사용자별 온톨로지/Charts 레이아웃, 일·주·월·연 모의결제와 운영 승인, Discord/Slack/Telegram 알림을 지원한다.
- 개발 실행·최초 접속: [docs/maintanance/README.md](docs/maintanance/README.md) ·
  기동·중지: [docs/operations.md](docs/operations.md) ·
  보안·인프라 보충: [docs/additional_doc/README.md](docs/additional_doc/README.md)

## 카탈로그 API (인증 후 조회 전용)

| 엔드포인트 | 내용 |
|---|---|
| `/api/v1/catalog/tables` | published gold 6종 요약 (카드용) |
| `/api/v1/catalog/tables/{name}` | 상세: 스키마·품질·계보·샘플 |
| `/api/v1/catalog/tables/{name}/schema` | 컬럼·물리 타입·설명 |
| `/api/v1/catalog/tables/{name}/quality` | 상류 silver quality_status 분포 |
| `/api/v1/catalog/tables/{name}/sample` | 샘플 5행 |

에러는 RFC 7807(problem+json). 응답 스키마는 Pydantic `response_model` 로 고정
(= API 의 contract enforced).

## Charts Studio (`/charts`) — 전 도메인 Gold 차트 스튜디오

카탈로그 옆의 **시각화 스튜디오**. 사이드바의 Charts Studio(막대 아이콘)로 진입한다.
격리 원칙: 백엔드는 [app/charts/](app/charts/), 프론트는 [app/static/charts/](app/static/charts/)
안에서만 관리하고, 본체는 `main.py` 의 라우터 include 한 줄로만 연결된다.

- **온톨로지 바인딩** — 도표는 컬럼명이 아니라 **필드의 의미역(role: time/geo_*/category/measure)**
  에 바인딩된다. 소스 목록·role 은 카탈로그 스냅샷에서 자동 파생되므로 gold 테이블·컬럼이
  변해도 코드 수정 없이 흡수되고, 저장된 필드가 사라지면 같은 role 로 폴백(타일에 '재바인딩' 표기).
  필수 슬롯은 서로 다른 실제 필드를 써야 하고, 비가산 측정값에서는 합계를,
  원형에서는 평균/최소/최대를, 시간 누적 불가 지표에서는 누적 경주를 제거한다.
  role별 필터 연산자도 서버와 UI가 공유하며 숫자는 `try_cast(col as double)`로만 비교한다.
- **도표 14종** — 스탯/막대/선/원형/산점도/히트맵/테이블/**타임랩스 경주**(재생·정지·프레임·속도 제어)
  + 지도 6종(서울 자치구·행정동·법정동, 대한민국 시도, 세계, 좌표 밀도). GeoJSON 동봉.
- **레이아웃 페이지** — 왼쪽 사이드탭에서 추가/전환, 우클릭으로 순서변경(위/아래)·이름변경·복제·삭제.
  오른쪽 위 **레이아웃 변경 → 드래그·리사이즈 → 레이아웃 저장**(저장 전에는 일반 화면에서 고정).
  저장소는 사용자별 RDB `auth_dashboard_layouts`이며, 첫 접근 때 `layouts.seed.json`의 기본
  5페이지(상권 4 + 문화·교통·날씨·도시데이터·대중교통 대표 페이지)를 복제한다.
- **데이터 경로** — `/api/v1/charts/query` 가 온톨로지 스펙을 화이트리스트 검증 후 SQL 로 조립해
  Trino gold 를 직접 집계한다(식별자=레지스트리 실재 필드만, 값=이스케이프). 결과는 디스크 캐시
  (TTL 10분)로 박제되고, 같은 cold-cache SQL은 singleflight로 한 번만 실행한다. Trino 다운 시
  stale 캐시로 응답해 화면이 죽지 않는다(mode 표기: live/cache/stale).
- **자동 갱신** — 상단 간격 선택(끔/1분/5분/10분/30분/1시간, 기본 10분)으로 캐시를 우회(force)해
  주기 재질의하고 무깜빡임으로 화면을 갱신한다(다음 갱신 카운트다운 표시). 편집 중·백그라운드 탭은
  건너뛰고 다시 보이면 밀린 갱신을 즉시 수행. 검증용 오버라이드: `/charts?refresh=<초>`.
- 검증: `python3 -m pytest -q`로 전체 온톨로지 계약을, `/charts?selftest=1`로
  임시 페이지 기반 3페르소나 UX·6개 도메인 드로어/실질의·레이스 컨트롤·결측 보존을 검증한다.
- **문서**: 기동/중지 매뉴얼·사용 가이드·설계 의도(대/중/소분류)·**계승 문서(HERITAGE)** 는
  [docs/](docs/README.md) — 새 작업자(사람/AI)는 [docs/HERITAGE.md](docs/HERITAGE.md) 부터.

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/v1/charts/meta` | 도표 타입(슬롯 계약)·값 라벨 사전 |
| `GET /api/v1/charts/sources` | 차트 소스(gold) + role 요약 |
| `GET /api/v1/charts/sources/{name}` | 필드·role·기본 도표 힌트 |
| `GET /api/v1/charts/sources/{name}/availability` | 실데이터 기준 필드별 null 아닌 값 수 |
| `POST /api/v1/charts/query` | 온톨로지 스펙 → gold 집계 (live/cache/stale) |
| `GET·POST·PATCH·DELETE /api/v1/charts/layouts…` | 레이아웃 페이지 CRUD·복제·순서변경 |

## W3 본작업으로 갈 때 바뀌는 것

- extract.py → Airflow 태스크(transform 후속 스텝)로 승격, 스냅샷은 마트/serving-postgres 로
- 독립 dashboard Compose → 상위 `sample/` 공유 배포와 network·secret 계약 통합
- 도메인 1개 → 6개 (manifest 경로만 도메인별로 늘리면 됨)
- SLO 엔드포인트 추가 (#257/DBT#110 마트 완성 후)
