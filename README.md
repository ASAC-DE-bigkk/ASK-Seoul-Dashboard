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

```bash
# 1) 스냅샷 갱신 (전제: sample/ 스택 기동 + dbt target/ 에 manifest·catalog.json)
.venv/Scripts/python extract.py

# 2) 설치·인증 DB 초기화·최초 관리자
python3 -m pip install -r requirements.txt
cp .env.example .env  # 값을 편집한 뒤 shell에 로드. 운영 시크릿은 secret manager에서 주입
set -a; source .env; set +a
python3 scripts/init_auth_db.py
python3 scripts/create_admin.py --email admin@example.com

# 3) 서버
python3 -m uvicorn app.main:app --port 8765
# → http://127.0.0.1:8765  (화면) · /docs (Swagger) · /health
```

## 인증·회원·권한

- 익명은 랜딩·로그인·가입·비밀번호 재설정만 접근한다.
- 게스트/일반회원/운영자/최고관리자 역할, 역할 기본 페이지 권한, 사용자 allow/deny override를 지원한다.
- 이메일 인증 또는 관리자 승인, Argon2id 비밀번호, DB 세션·CSRF, 계정 잠금, 요청 제한을 적용한다.
- 사용자별 온톨로지/Charts 레이아웃, 일·주·월·연 모의결제와 운영 승인, Discord/Slack/Telegram 알림을 지원한다.
- 운영 문서: [docs/additional_doc/README.md](docs/additional_doc/README.md)

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

## Charts Studio (`/charts`) — Commerce Gold 차트 스튜디오

카탈로그 옆의 **시각화 스튜디오**. 사이드바의 Charts Studio(막대 아이콘)로 진입한다.
격리 원칙: 백엔드는 [app/charts/](app/charts/), 프론트는 [app/static/charts/](app/static/charts/)
안에서만 관리하고, 본체는 `main.py` 의 라우터 include 한 줄로만 연결된다.

- **온톨로지 바인딩** — 도표는 컬럼명이 아니라 **필드의 의미역(role: time/geo_*/category/measure)**
  에 바인딩된다. 소스 목록·role 은 카탈로그 스냅샷에서 자동 파생되므로 gold 테이블·컬럼이
  변해도 코드 수정 없이 흡수되고, 저장된 필드가 사라지면 같은 role 로 폴백(타일에 '재바인딩' 표기).
  필터 비교는 `cast(col as varchar)`/`try_cast(col as double)` 로 조립해 물리 타입 드리프트에도 안전.
- **도표 14종** — 스탯/막대/선/원형/산점도/히트맵/테이블/**타임랩스 경주**(시간 프레임 자동 재생)
  + 지도 6종(서울 자치구·행정동·법정동, 대한민국 시도, 세계, 좌표 밀도). GeoJSON 동봉.
- **레이아웃 페이지** — 왼쪽 사이드탭에서 추가/전환, 우클릭으로 순서변경(위/아래)·이름변경·복제·삭제.
  오른쪽 위 **레이아웃 변경 → 드래그·리사이즈 → 레이아웃 저장**(저장 전에는 일반 화면에서 고정).
  저장소는 사용자별 RDB `auth_dashboard_layouts`이며, 첫 접근 때 `layouts.seed.json`의 기본 4페이지를 복제한다.
- **데이터 경로** — `/api/v1/charts/query` 가 온톨로지 스펙을 화이트리스트 검증 후 SQL 로 조립해
  Trino gold 를 직접 집계한다(식별자=레지스트리 실재 필드만, 값=이스케이프). 결과는 디스크 캐시
  (TTL 10분)로 박제되고, Trino 다운 시 stale 캐시로 응답해 화면이 죽지 않는다(mode 표기: live/cache/stale).
- **자동 갱신** — 상단 간격 선택(끔/1분/5분/10분/30분/1시간, 기본 10분)으로 캐시를 우회(force)해
  주기 재질의하고 무깜빡임으로 화면을 갱신한다(다음 갱신 카운트다운 표시). 편집 중·백그라운드 탭은
  건너뛰고 다시 보이면 밀린 갱신을 즉시 수행. 검증용 오버라이드: `/charts?refresh=<초>`.
- 검증: `/charts?selftest=1` 로 실브라우저 셀프테스트(이동·저장·CRUD·온톨로지 폴백 14항목) 실행.
- **문서**: 기동/중지 매뉴얼·사용 가이드·설계 의도(대/중/소분류)·**계승 문서(HERITAGE)** 는
  [docs/](docs/README.md) — 새 작업자(사람/AI)는 [docs/HERITAGE.md](docs/HERITAGE.md) 부터.

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/v1/charts/meta` | 도표 타입(슬롯 계약)·값 라벨 사전 |
| `GET /api/v1/charts/sources` | 차트 소스(gold) + role 요약 |
| `GET /api/v1/charts/sources/{name}` | 필드·role·기본 도표 힌트 |
| `POST /api/v1/charts/query` | 온톨로지 스펙 → gold 집계 (live/cache/stale) |
| `GET·POST·PATCH·DELETE /api/v1/charts/layouts…` | 레이아웃 페이지 CRUD·복제·순서변경 |

## W3 본작업으로 갈 때 바뀌는 것

- extract.py → Airflow 태스크(transform 후속 스텝)로 승격, 스냅샷은 마트/serving-postgres 로
- 서버 → compose 서비스 (공유 인프라 = 팀 게이트, serving-postgres 선례 패턴)
- 도메인 1개 → 6개 (manifest 경로만 도메인별로 늘리면 됨)
- SLO 엔드포인트 추가 (#257/DBT#110 마트 완성 후)
