# HERITAGE — 계승 문서 (다음 작업자/AI 는 이것부터)

이 문서 하나로 **이 리포의 규정**과 **Charts Studio 의 설계 의도**를 이어받아,
지금까지의 맥락 그대로 작업을 계속할 수 있어야 한다. 세부는 링크된 문서가 정본이며,
코드와 문서가 어긋나면 코드를 따르되 문서를 같은 커밋에서 갱신한다.

---

## 1. 이 프로젝트가 무엇인가

**ASK SEOUL** — 서울 오픈데이터 레이크하우스(dbt·Trino·Iceberg·Airflow·R2) 위의 데모 대시보드.
상위 저장소 `sample/` 의 서브모듈이며(github.com/ASAC-DE-bigkk/ASK-Seoul-Dashboard), 아래 화면을 서빙한다:

| 화면 | 경로 | 사상 |
|---|---|---|
| 데이터 마켓플레이스(카탈로그) | `/catalog` | **"계산은 파이프라인이 미리, API 는 얇게"** — extract.py 가 박제한 스냅샷 JSON 만 서빙 |
| **Charts Studio** | `/charts` | 예외적으로 **gold 라이브 집계** — 단, 화이트리스트 SQL + 디스크 캐시 + stale 폴백으로 얇음을 유지 |
| 인증·프로필 | `/auth/*`, `/profile` | 승인형 가입, DB 세션, 개인 온톨로지·이용권 |
| 운영 콘솔 | `/admin` | 회원·페이지 권한·정책·IP·모의결제 승인 |

Charts Studio 가 라이브 질의를 갖는 이유: 사용자가 소스·차원·집계를 조합해 만드는 질의는
미리 계산해둘 수 없기 때문. 대신 캐시(TTL 600s)와 stale 폴백으로 "요청마다 Trino 를 두드리는
무거운 API"가 되는 것을 막았다. 이 긴장 관계를 이해해야 캐시 관련 코드를 올바르게 고칠 수 있다.

## 2. 저장소 규정 (건드리기 전에)

- **커밋**: 한국어 conventional — `feat: 요약 — 상세`, `fix: …`, `demo: …`. 논리 단위로 상시 커밋.
- **브랜치/이슈/PR**: org 컨벤션은 `feat|fix/<이슈번호>-<슬러그>` 브랜치, 이슈 제목 `[Feat]/[Bug] 한국어 요약`.
  push·PR 은 사용자 승인 후에만. **`dev` 직병합 금지 방침의 작업은 `test/…` 브랜치에 격리**
  (Charts Studio 는 `test/commerce-charts-studio` 에서 작업됨 — 병합 시점은 별도 결정).
- **격리 원칙(최상위 불변식)**: 차트 관련 코드는 `app/charts/`(백엔드)와 `app/static/charts/`(프론트)
  밖으로 나가지 않는다. 본체 접점은 `main.py` 의 include 한 곳 — **접점을 늘리는 변경은 하지 않는다.**
- **데이터 규정(상위 커머스 번들 계승)**: 원본 값을 파괴하지 않는다 — 코드값은 표시 라벨로만 번역하고,
  집계·필터는 재현 가능해야 한다(응답에 SQL 포함). 시크릿을 코드·로그·커밋에 넣지 않는다.
- **문서 체인**: [docs/README.md](README.md)(인덱스) → [operations.md](operations.md)(기동/중지) ·
  [maintanance/README.md](maintanance/README.md)(개발 실행·최초 접속·운영 원칙) ·
  [charts-user-guide.md](charts-user-guide.md)(사용법) · [charts-design-intents.md](charts-design-intents.md)(의도 A~F) · 본 문서(계승).

## 3. 환경 사실 (하드코딩된 지식 — 모르면 사고 나는 것들)

| 사실 | 내용 |
|---|---|
| Trino | `http://127.0.0.1:30586` (compose 서비스, env `CHARTS_TRINO_URL` 로 오버라이드). dev 카탈로그 `iceberg_dev`, 스키마 `commerce` |
| 소스 정본 | `snapshot/catalog_snapshot.json` — **낡을 수 있다**. 실물 스키마와 다르면 갱신은 `extract.py`. 질의는 cast 기반이라 낡아도 안전(실사례: `cohort_y` varchar→integer 드리프트를 흡수) |
| 지역 코드 | gold 데이터는 전부 **MOIS(행안부)** 체계(종로=11110). GeoJSON 자산 중 seoul_gu/seoul_dong 의 code 는 **KOSTAT(통계청)** 체계(종로=11010) — **혼용 금지**. 매칭 규칙은 `geo.js` 상단 주석과 [design-intents D-3](charts-design-intents.md) |
| 코드값 | `major`: health/culture/industry/environment · `event_type`: opened/closed · `age_band`: `0_lt1y`~`5_ge20y` (라벨 사전은 `ontology.VALUE_LABELS`) |
| 결측 표기 | 지역 코드 결측은 문자열 `'UNK'` — 지도 매칭에서 제외된다 |
| 파이썬/실행 | Python 3.9+ 호환. FastAPI·SQLAlchemy·Argon2. 포트 관례 8765(문서)·8799(개발) |
| 인증 DB | `DATABASE_URL`, 기본 `sqlite:///./data/ask_seoul.db`. PostgreSQL/MySQL dialect DDL도 테스트 |
| 세션 | DB에는 HMAC 해시만 저장. 절대+idle 만료와 사용자별 상한 적용. 운영은 `AUTH_SESSION_PEPPER`, HTTPS Secure cookie 필수 |
| MFA | RFC 6238 TOTP+일회용 복구 코드. 운영자 이상 기본 강제. 하위 역할은 감사 사유 기반 관리자 초기화, 권한 계정은 CLI break-glass만 허용. `AUTH_MFA_MASTER_KEY` 장기 보관 필수 |

## 4. 파일 지도 — 무엇을 고치려면 어디를 보나

```
app/charts/                    ← 백엔드 번들 (격리)
  ontology.py    role 추론·CHART_TYPES 슬롯 계약·VALUE_LABELS·CURATED  ← 온톨로지의 정본
  querybuilder.py 화이트리스트 SQL 조립 (식별자=레지스트리 실재 필드만, cast/try_cast 필터)
  trino.py       REST 실행기 (503 재시도·취소·120s 데드라인) + 디스크 캐시(TTL·stale·force)
  layouts.py     사용자별 RDB 레이아웃 영속 (첫 접근 시 시드 복제)
  router.py      /api/v1/charts/* (meta·sources·query·layouts CRUD) — RFC7807 에러
  models.py      요청/응답 Pydantic 계약
  data/layouts.seed.json  기본 5페이지(상권 4 + 전 도메인 1, 커밋 대상. cache/ 는 런타임)
app/auth/                      ← 인증·회원·RBAC·정책·결제 모델/서비스/API/미들웨어
app/notifications/             ← Discord·Slack·Telegram 운영 알림 인터페이스
app/static/charts/             ← 프론트 번들 (격리)
  index.html     스튜디오 셸 (버전 고정+SRI CDN: Pretendard·echarts@5.5·gridstack@10.3)
  charts.css     디자인 토큰 = 마켓플레이스 index.html 과 동일 헤리티지
  js/api.js      fetch 래퍼 (problem+json → Error)
  js/recommend.js 자동 추천 — role·이름패턴 기반 도표/조합 제안 (테이블 하드코딩 금지)
  js/geo.js      지도 자산 로딩·등록·지역 매칭 (MOIS_GU 사전, 동명 유일화·모호 제외)
  js/render.js   도표 렌더러 — 슬롯만 보고 그린다. 팔레트·피벗·정렬·null 규칙 여기
  js/app.js      상태·그리드(gridstack)·편집모드·드로어·사이드탭·자동갱신·셀프테스트
app/static/auth/               ← 로그인·가입·재설정·프로필·운영 콘솔
app/main.py      본체 접점 (카탈로그 + auth/charts router + 정적 화면)
docs/additional_doc/           ← 인증/RDB/알림/클라우드 WAF 운영 문서
```

## 5. 설계 의도 요약 (정본: [charts-design-intents.md](charts-design-intents.md))

1. **격리(A)** — 번들 밖을 만지지 않는다.
2. **온톨로지(B)** — 도표↔소스 연결은 컬럼명이 아니라 **role**. 저장물은 바인딩 선언뿐이고
   렌더 시점마다 재해석·폴백된다. 지원 판정은 필수 슬롯마다 서로 다른 실재 필드를 배정할 수
   있어야 하며, 필드별 가산성·허용 집계도 같은 계약에 포함된다.
3. **안전한 질의(C)** — 식별자 화이트리스트, 값 이스케이프, 프로토콜 준수, 캐시는 정직하게(mode 표기).
4. **읽히는 도표(D)** — 검증된 8색 고정 팔레트, 색-의미 고정, 결측≠0, 비가산 집계는 접지 않음,
   원형=sum/count, 누적 가능성과 가산성 분리, MOIS/KOSTAT 구분, 커버리지 표기.
   소스 선택 때 실데이터 `count(field)`로 전부-null 필드를 슬롯·필터에서 제거하며 1행 소스는 통계·표만 노출.
5. **모드 분리 UX(E)** — 평시 고정, 편집 모드에서만 배치, 명시적 저장.
6. **기계 검증(F)** — 고치면 6개 도메인 서버 계약 테스트, 3페르소나 브라우저 셀프테스트,
   대표 Gold 실제 질의와 스크린샷으로 다시 증명한다.

## 6. 확장 레시피 — 자주 있을 작업의 표준 절차

### 6-1. 새 도표 타입 추가
1. `ontology.CHART_TYPES` 에 슬롯 계약 추가(라벨·icon·accepts·required·options)
2. `render.js` 에 렌더러 추가 + `render()` 분기 연결, 필요 시 `TYPE_ICONS`(app.js)에 아이콘
3. `buildSpec()`(app.js)에 스펙 매핑 분기 추가
4. `recommend.js` 에 추천 규칙(types 의 put + combos 분기)을 추가 — role 조건으로만 쓸 것
5. 검증: 드로어에서 추가 → 미리보기 → 셀프테스트·스크린샷

### 6-2. 새 지도 추가
1. GeoJSON 을 `app/static/charts/geo/` 에 동봉(외부 CDN 로딩 금지 — 재현성)
2. `geo.js CONF` 에 항목 추가 — **코드 체계부터 확인**(MOIS 인가? 아니면 이름 매칭만 허용)
3. `ontology.CHART_TYPES` 에 `map_*` 타입(지역 role + measure, `geo` 키) 추가
4. 커버리지 표기(`매칭 N/M`)가 나오는지 확인

### 6-3. 새 gold 소스/도메인 노출
- 코드 수정 불필요 — `extract.py` 로 스냅샷에 실리면 자동으로 소스 목록·role 추론에 잡힌다.
- 한국어 라벨·기본 차트를 다듬고 싶을 때만 `ontology.CURATED` 에 힌트 추가.

### 6-4. role 어휘 확장
- `ontology.NAME_ROLES` 에 이름 규칙 추가 → 해당 role 을 받는 슬롯(`accepts`)에 편입.
- 프론트는 meta 를 통해 자동 반영 — 프론트에 role 이름을 하드코딩하지 않는다.

## 7. 검증 절차 (변경 후 필수)

```bash
# 1) 문법
node --check app/static/charts/js/*.js
# 2) 서버 계약(모든 Gold 지원 타입·시드·집계 제약)
python3 -m pytest -q
# 3) 서버 기동 후 브라우저 셀프테스트 — 탭 제목 SELFTEST_ALL_PASS 확인
open "http://127.0.0.1:8765/charts?selftest=1"
# 4) 화면 확인 (헤드리스 가능)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --window-size=1600,1100 --virtual-time-budget=20000 \
  --screenshot=/tmp/p1.png "http://127.0.0.1:8765/charts?page=seed-overview"
# 자동 갱신 검증: ?refresh=4 + --virtual-time-budget 로 가속
```

수정이 시각 규칙(D)에 닿으면 스크린샷을 눈으로 보고, 팔레트를 바꾸면 dataviz 검증기를 다시 돌린다.

## 8. 알려진 한계 (의도된 트레이드오프 — "버그 아님", 고칠 거면 여기부터)

| 한계 | 배경 |
|---|---|
| 이름 기반 동 단위 그룹핑은 동명이동을 서버에서 합칠 수 있음 | dims 가 단일 필드라서. 지도는 모호 제외로 방어하지만 테이블/막대는 합산된다. 근본 해결은 코드+이름 복합 dim 지원 |
| 행정동 지도 코드 매칭 미지원 | 자산 코드가 KOSTAT 이라 MOIS 10자리와 호환 불가 — 이름 매칭만. MOIS 경계 GeoJSON 확보 시 교체 |
| 가중 평균 미지원 | 집계가 단일 필드 함수뿐. 비율의 정확한 재집계(ratio-of-sums)가 필요하면 파생 measure 지원을 설계할 것. 그때까지 시드는 "단순평균" 명시·표본 필터로 정직하게 |
| 앱 rate limit은 프로세스 로컬 | 운영의 정본은 AWS WAF/GCP Cloud Armor/Cloudflare. Trino live 질의는 별도 동시 실행 상한 적용, 필요 시 Redis 공용 limiter |
| CSP inline style 허용 잔존 | inline script는 응답 hash, event handler는 `script-src-attr 'none'`. 기존 inline style은 공개 운영 전 CSS class로 이동 |
| MFA master key 무중단 회전 미지원 | 키 변경 시 기존 TOTP 재등록 필요. secret manager 백업과 복구 코드를 우선 운영 |
| 알림은 DB outbox+주기 worker | exactly-once 외부 전달은 보장하지 않음. 장기 장애에는 backoff·최대 시도·DLQ 추가 |
| 스냅샷 신선도 수동 | `extract.py` 수동 실행. W3 본작업에서 Airflow 태스크로 승격 예정(상위 README 참조) |

## 9. 이력 요약

- 2026-07-16: Charts Studio 신설(`test/commerce-charts-studio`) — 온톨로지·도표 13종·지도 6종·
  레이아웃 페이지·시드 3페이지. 멀티에이전트 리뷰 확정 14건(critical 1) 수정 반영.
- 2026-07-17: 자동 갱신(간격 선택·캐시 우회·무깜빡임) 추가. 본 docs/ 체계 신설.
- 2026-07-17: 자동 추천(recommend.js) — 도표 단계 추천 배지·이유 툴팁, 연결 단계 원클릭 조합.
  셀프테스트 17항목으로 확장.
- 2026-07-17: 타임랩스 경주(race) 도표 — 시간 프레임 자동 재생·클릭 일시정지·누적/구간 모드,
  시드 4페이지(타임랩스) 추가. 셀프테스트 18항목.
- 2026-07-17: 독립 인증·회원·RBAC·정책·사용자별 레이아웃·모의결제·운영 알림과 클라우드 WAF
  운영 문서를 추가. 레이아웃 저장소를 전역 JSON에서 사용자별 RDB로 전환.
- 2026-07-17: 인증 hardening — TOTP MFA·복구 코드, idle/session 상한, 안전한 token 소비,
  전역 관리자 전이 직렬화, 결제 단일 pending, 알림 outbox/복구 worker, schema v6와 개인정보 보존
  운영 문서를 추가.
- 2026-07-17: Charts Studio 전 도메인화 — 문화·교통·날씨·도시데이터·대중교통 시드/실질의,
  실제 필드 매칭·가산성 기반 온톨로지, count-only 일정, 툴팁 수명주기, 미리보기 성공 전 적용 차단,
  페이지/소스/질의 경합 방어, null 보존, 타임랩스 프레임·속도 컨트롤과 3페르소나 셀프테스트 추가.
  후속 재검토에서 원형/누적 수학 제약, 기술시각·풍향·비가산 농도 차단, 저장/취소 직렬화,
  모바일 페이지 관리, 필터 값 사전과 전부-null 미리보기 거부까지 보강. 최종 회귀 검토에서
  coverage 비가산성·누적 flow allowlist·role별 서버 필터·availability rate/singleflight와
  사용자 레이아웃을 건드리지 않는 임시 페이지 셀프테스트를 추가.
- 2026-07-19: 인증 DB 운영 검증 — SQLite/PostgreSQL/MySQL 실제 초기화·로그인·관리 API·레이아웃
  smoke test를 통과. DB-aware health, SQLite 비공개 권한, production MFA/CLI bootstrap fail-closed,
  쓰기 없는 maintenance dry-run과 최초 접속 운영 문서를 추가.
