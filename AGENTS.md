# AGENTS.md — ASK SEOUL Dashboard 작업 규약

이 파일은 `dashboard/` 저장소에서 작업하는 사람과 AI 에이전트의 최상위 진입점이다.
이 저장소는 상위 `sample/` 프로젝트의 Git 서브모듈이며, 데이터 카탈로그와 Charts Studio를
FastAPI로 서빙한다.

코드와 문서가 어긋나면 **실행되는 코드를 우선 확인**하고, 같은 변경에서 관련 문서를 갱신한다.
프로젝트의 역사와 Charts Studio 설계 의도는 [docs/HERITAGE.md](docs/HERITAGE.md)가 정본이다.

---

## 0. 작업 시작 순서

코드를 설계하거나 수정하기 전에 아래 순서로 읽는다.

1. 이 파일 `AGENTS.md`
2. [docs/HERITAGE.md](docs/HERITAGE.md)
3. [docs/README.md](docs/README.md)
4. 작업 종류에 따라:
   - Charts Studio 코드/UX: [docs/charts-design-intents.md](docs/charts-design-intents.md)
   - 실행·장애·환경: [docs/operations.md](docs/operations.md)
   - 서버 배포 전체 순서(사람): [docs/deployment/end-to-end-human-runbook.md](docs/deployment/end-to-end-human-runbook.md)
   - 서버 배포(사람): [docs/deployment/server-setup-human.md](docs/deployment/server-setup-human.md)
   - 서버 배포(AI): [docs/deployment/server-agent-runbook.md](docs/deployment/server-agent-runbook.md)
   - R2/Trino secret: [docs/deployment/r2-secret-management.md](docs/deployment/r2-secret-management.md)
   - 사용자 흐름: [docs/charts-user-guide.md](docs/charts-user-guide.md)
   - 카탈로그/API 전반: [README.md](README.md)
5. 실제 구현 파일과 테스트/검증 경로

상류 데이터 계약이 필요한 경우에만 다음을 읽는다.

- 수집·적재·분류 정책: `../dags/domains/commerce/CLAUDE.md` →
  `Share.md` → `docs/PROJECT.md`
- silver/gold 모델과 컬럼: `../dbt/domains/commerce/README.md` →
  `docs/README.md` → `models/`

현재 `dbt/domains/`에는 별도 `CLAUDE.md`가 없으므로, dbt 쪽은 README·문서 인덱스·
`dbt_project.yml`·모델 정의를 실행 계약으로 사용한다.

---

## 1. 실행 모드 게이트

모든 작업은 먼저 아래 중 하나로 분류한다.

### Mode A — MVP First

아이디어 검증, 일회성 데모, 빠른 UI 실험처럼 장기 계약이 아직 확정되지 않은 작업에 사용한다.

우선순위:

1. 작동하는 최소 결과
2. 기존 경계 안의 작은 변경
3. 이해 가능한 데이터 흐름
4. 기본 오류 처리와 검증
5. 후속 보강 항목의 명시

### Mode B — Maintainability First

반복 운영, 데이터 계약, 캐시·레이아웃 영속성, API 변경, 상류 스키마 드리프트,
팀 인수인계에 영향을 주는 작업에 사용한다.

우선순위:

1. 기존 API·온톨로지·레이아웃 계약 보존
2. 결정적이고 재현 가능한 질의
3. 실패 시 안전한 폴백
4. 런타임 상태와 커밋 대상의 분리
5. 관측 가능성, 문서 정합성, 회귀 검증

### Mode C — Review / Architecture

코드·설계·PR 검토에는 Critical → Major → Minor 순으로 판단한다.

- Critical: 데이터 왜곡, SQL/XSS/시크릿 노출, 잘못된 지역 매칭, 영속 데이터 손실, 서비스 불능
- Major: 캐시·폴백·동시성·스키마 드리프트 대응 실패, 경계 침범, 운영상 높은 장애 가능성
- Minor: 명명, 문서 드리프트, 작은 사용성·유지보수성 문제

이 저장소는 이미 운영 가능한 데모와 누적 설계 계약이 있으므로, 불명확하면
**Mode B — Maintainability First**를 기본으로 한다.

---

## 2. 작업 경계

- 대시보드 작업은 원칙적으로 **`dashboard/` 내부에서만** 수행한다.
- 상위 `sample/`, `dags/`, `dbt/`, Compose, Airflow 이미지, Trino 설정을 바꿔야 한다면
  대시보드 변경과 분리해 이유·영향·대안을 먼저 설명하고 사용자 동의를 받는다.
- 상류 DAG/dbt 파일은 대시보드가 소비하는 계약을 이해하기 위한 읽기 대상이다.
  대시보드 요구에 맞추려고 상류 모델을 임의로 바꾸지 않는다.
- 새 패키지, 외부 CDN, 새 인프라 구성요소는 기존 Python + FastAPI + 정적 JS 구조로
  해결할 수 없는 이유와 운영 비용을 설명한 뒤 승인받는다.
- 시크릿, 토큰, 쿠키, 실제 자격증명은 코드·문서·스냅샷·캐시·로그·경로에 넣지 않는다.

---

## 3. 프로젝트 정체성과 데이터 경로

ASK SEOUL Dashboard는 같은 데이터 레이크하우스를 두 방식으로 보여준다.

### 3.1 데이터 카탈로그 — `/catalog`

```text
dbt manifest/catalog + Trino 실측
                │
             extract.py
                │
 snapshot/catalog_snapshot.json
                │
 app/main.py → /api/v1/catalog/* → /catalog
```

원칙은 **“계산은 파이프라인이 미리, API는 얇게”**다.

- 요청 시 카탈로그 메타를 다시 계산하거나 Trino를 질의하지 않는다.
- `snapshot/catalog_snapshot.json`은 파생 스냅샷이며 원천 데이터가 아니다.
- 새 테이블·컬럼·설명·계보가 필요하면 `extract.py`로 스냅샷을 재생성한다.
- 응답 계약은 `app/models.py`의 Pydantic 모델과 FastAPI `response_model`로 고정한다.
- 오류는 RFC 7807 `application/problem+json` 형식을 유지한다.

### 3.2 Charts Studio — `/charts`

```text
snapshot/catalog_snapshot.json → 소스·필드·role 레지스트리
                                  │
사용자 chart spec → 화이트리스트 SQL 빌더 → Trino gold
                                  │
                         live / cached / stale 결과
```

사용자가 소스·차원·집계를 조합하므로 미리 계산할 수 없는 예외 경로다.
대신 다음 제한으로 API를 얇고 안전하게 유지한다.

- gold relation만 질의한다.
- 소스·필드 식별자는 스냅샷 레지스트리에 존재하는 값만 허용한다.
- 집계·연산자는 고정 화이트리스트를 사용한다.
- 사용자에게 임의 SQL 문자열을 받지 않는다.
- 행 상한은 5,000이며, 절단 여부를 응답과 화면에 숨기지 않는다.
- 디스크 캐시 TTL 기본값은 600초다.
- Trino 장애 시 마지막 성공 캐시를 `stale`로 제공할 수 있지만 상태를 명시한다.
- 수동/자동 새로고침의 `force`는 fresh cache만 우회하며 안전 규칙은 우회하지 않는다.
- 응답에 실행 SQL을 포함해 집계가 재현 가능하게 한다.

---

## 4. 파일 구조와 책임

```text
dashboard/
├─ AGENTS.md                       # 이 파일: 작업 규약과 재개 진입점
├─ README.md                       # 제품 개요·API 표
├─ extract.py                      # dbt/Trino → 카탈로그 스냅샷 생성
├─ requirements.txt
├─ snapshot/
│  └─ catalog_snapshot.json        # 카탈로그·Charts 소스 메타 스냅샷
├─ app/
│  ├─ main.py                      # FastAPI 본체·카탈로그 API·auth/charts include
│  ├─ models.py                    # 카탈로그 API Pydantic 계약
│  ├─ auth/                        # 회원·세션·RBAC·정책·결제·보안 미들웨어
│  ├─ notifications/               # Discord·Slack·Telegram 알림 인터페이스
│  ├─ charts/                      # Charts Studio 백엔드 번들
│  │  ├─ ontology.py               # role 추론·CHART_TYPES·VALUE_LABELS·CURATED 정본
│  │  ├─ querybuilder.py           # 화이트리스트 SQL 조립
│  │  ├─ trino.py                  # Trino REST·재시도·취소·캐시·stale 폴백
│  │  ├─ layouts.py                # 사용자별 RDB 레이아웃·시드 복사
│  │  ├─ router.py                 # /api/v1/charts/*
│  │  ├─ models.py                 # Charts API Pydantic 계약
│  │  └─ data/
│  │     ├─ layouts.seed.json      # 커밋 대상 기본 레이아웃
│  │     └─ cache/                 # 런타임, 커밋 금지
│  └─ static/
│     ├─ landing.html
│     ├─ index.html                # 데이터 마켓플레이스
│     ├─ auth/                     # 로그인·가입·프로필·운영 콘솔
│     └─ charts/                   # Charts Studio 프론트 번들
│        ├─ index.html
│        ├─ charts.css
│        ├─ js/
│        │  ├─ api.js              # API 래퍼·problem+json 처리
│        │  ├─ app.js              # 상태·편집·레이아웃·셀프테스트
│        │  ├─ recommend.js        # role 기반 자동 추천
│        │  ├─ geo.js              # 지도 등록·코드/이름 매칭
│        │  └─ render.js           # 슬롯 기반 차트 렌더링
│        └─ geo/                    # 재현 가능한 로컬 GeoJSON
└─ docs/
   ├─ HERITAGE.md                  # 역사·불변식·확장법 정본
   ├─ README.md                    # 문서 인덱스
   ├─ deployment/                  # 사람용 서버 준비 + 서버 AI용 안전 runbook
   ├─ charts-design-intents.md     # 설계 의도 A~F
   ├─ charts-user-guide.md
   ├─ additional_doc/              # 인증·RDB·알림·클라우드 WAF
   └─ operations.md
```

---

## 5. Charts Studio 격리 불변식

- 차트 백엔드 로직은 `app/charts/` 안에 둔다.
- 차트 프론트 로직·스타일·지도 자산은 `app/static/charts/` 안에 둔다.
- 본체 접점은 `app/main.py`의 router include와 `/charts` 화면 라우트로 제한한다.
- 카탈로그 본체와 공유해야 하는 데이터는 `snapshot/catalog_snapshot.json`을 통해 공유한다.
- 편의를 이유로 차트 전용 상태·상수·DOM 로직을 `main.py`, `app/models.py`,
  `app/static/index.html`로 확산시키지 않는다.
- Charts 번들을 삭제하면 기능 전체가 제거될 수 있는 구조를 유지한다.

기존 본체 진입 링크처럼 최소한의 연결이 필요할 수는 있지만, 접점을 추가할 때는
왜 기존 접점으로 해결할 수 없는지 먼저 확인한다.

---

## 6. 온톨로지와 스키마 드리프트 규칙

- 도표는 컬럼명이 아니라 `role`에 연결한다.
- role 추론, `CHART_TYPES` 슬롯 계약, `VALUE_LABELS`, `CURATED`의 서버 정본은
  `app/charts/ontology.py`다.
- 프론트는 `/api/v1/charts/meta`와 source API가 주는 계약을 소비한다.
  서버 계약 사본을 프론트에 다시 하드코딩하지 않는다.
- 저장되는 차트 설정은 `{type, source, bindings, agg, filters, options}`처럼 선언적으로 유지한다.
- 저장된 필드가 사라진 경우 같은 role의 필드로 재바인딩할 수 있지만, 화면에 폴백 사실을 표시한다.
- 추천 규칙은 role·슬롯·값 사전·일반적인 이름 패턴의 함수여야 한다.
  특정 테이블명이나 특정 gold 컬럼만을 위한 분기를 만들지 않는다.
- 문자열 비교는 `cast(... as varchar)`, 수치 비교는 `try_cast(... as double)` 계열의
  기존 드리프트 흡수 규칙을 보존한다.
- 코드값은 표시 라벨로만 번역하고 원본 값, SQL 필터 값, 응답 의미를 파괴하지 않는다.

---

## 7. 데이터 정확성과 시각화 규칙

### 7.1 집계

- 결측은 0이 아니다. 빈 피벗 셀과 우측 검열 구간은 `null`로 유지한다.
- `sum`/`count`처럼 가산 가능한 집계만 “기타”로 합칠 수 있다.
- `avg`·비율처럼 비가산인 값은 합쳐 왜곡하지 않는다.
- 비율 재집계가 필요한 경우 단순평균을 정확한 값처럼 표현하지 않는다.
- 산점도에서 `count`로 X·Y가 같은 값으로 붕괴하는 조합을 허용하지 않는다.
- 자연 순서가 있는 월·연차·밴드는 사전식/값 크기 정렬로 깨뜨리지 않는다.

### 7.2 지역 코드

- gold 지역 코드는 **MOIS(행안부)** 체계다.
- `seoul_gu.json`, `seoul_dong.json`의 code는 **KOSTAT(통계청)** 체계일 수 있다.
- 서로 다른 코드 체계를 직접 조인하지 않는다.
- 코드 체계를 검증할 수 없는 지도는 이름 매칭만 사용한다.
- 동명이동은 중복 칠하기보다 모호한 행을 제외하고 제외 개수를 표시한다.
- 모든 지도는 `매칭 N/M` 커버리지를 표시한다.

### 7.3 표현

- 기존 검증 팔레트, `VALUE_COLORS`, null 처리, “기타” 규칙을 임의로 바꾸지 않는다.
- 데이터 유래 문자열을 HTML에 넣을 때 반드시 escape한다.
- 행 상한, stale 상태, 재바인딩, 지도 미매칭을 숨기지 않는다.
- 차트 제목·노트·SQL은 사용자가 집계 의미를 오해하지 않게 작성한다.

---

## 8. 레이아웃과 런타임 상태

- `app/charts/data/layouts.seed.json`만 기본 레이아웃으로 커밋한다.
- 사용자 레이아웃은 `auth_dashboard_layouts`에 저장하고 모든 조회/수정에 `user_id` 조건을 둔다.
- `cache/`는 런타임 산출물이므로 커밋하지 않는다.
- 특정 사용자 초기화는 그 사용자의 레이아웃 행만 대상으로 하며 전체 DB를 지우지 않는다.
- 일반 모드에서는 타일을 고정하고, 편집 모드에서만 이동·리사이즈한다.
- 변경은 명시적 저장 전까지 영속되지 않아야 한다.
- 페이지 전환 경합으로 늦은 응답이 다른 페이지를 덮어쓰지 않게 세대/요청 식별 규칙을 보존한다.
- 캐시 키는 SQL과 실행 계약에서 결정적으로 만들어야 하며 시크릿이나 사용자 개인정보를 포함하지 않는다.

---

## 9. API·네트워크·보안 규칙

- 새 API는 Pydantic 요청/응답 모델과 명시적 오류 계약을 갖는다.
- 오류 응답은 기존 RFC 7807 `application/problem+json` 형식을 따른다.
- 외부/사용자 입력을 SQL 식별자, 파일 경로, 리다이렉트 URL로 직접 사용하지 않는다.
- Trino 요청은 timeout, 503 재시도, 전체 deadline, 실패 시 취소 규칙을 유지한다.
- 사용자 제공 URL을 서버가 대신 요청하는 기능은 만들지 않는다.
- 동적 SQL은 값 이스케이프만 믿지 말고 식별자·집계·연산자 화이트리스트를 모두 통과시킨다.
- 로그·예외·캐시·레이아웃·스냅샷에 API 키, 인증 헤더, DSN 비밀번호를 남기지 않는다.
- 새 시크릿은 환경변수로 주입하고 실제 값은 커밋하지 않는다.
- 인증/권한은 UI 숨김이 아니라 전역 미들웨어와 API 의존성에서 매 요청마다 강제한다.
- 역할 기본 권한과 사용자별 allow/deny override를 분리하고, 운영자는 자신보다 낮은 역할만 관리한다.
- 세션 원문·CSRF 원문·비밀번호 원문은 DB/로그에 저장하지 않는다.
- 인증·정책·RDB 변경은 `docs/additional_doc/`의 해당 운영 문서를 같은 변경에서 갱신한다.
- 외부 CDN이나 새 원격 자산은 재현성과 장애 격리를 해칠 수 있으므로, 기존 동봉 자산으로
  해결할 수 없는 경우에만 승인 후 추가한다.

상류 commerce 번들의 `include/security/`는 참고 가능한 이식형 보안 구현이지만,
대시보드에 복사·도입하는 것은 별도 구조 변경이다. 단순 문서나 UI 변경에 임의로 이식하지 말고,
서버 보안 체계를 확장하는 작업에서 필요성과 검증 범위를 먼저 합의한다.

---

## 10. 변경 유형별 구현 순서

### 10.1 새 chart type

1. `ontology.CHART_TYPES` 슬롯 계약
2. `render.js` 렌더러와 분기
3. `app.js` spec 매핑
4. `recommend.js` role 기반 추천
5. meta/source API와 브라우저 셀프테스트
6. 설계·사용 문서 갱신

### 10.2 새 지도

1. 코드 체계·라이선스·출처 확인
2. GeoJSON을 `app/static/charts/geo/`에 동봉
3. `geo.js` 등록과 안전한 매칭
4. `CHART_TYPES` 지도 슬롯 계약
5. 커버리지·모호 제외 표시 검증
6. 스크린샷 육안 확인

### 10.3 새 gold source

1. 상류 dbt gold 모델과 실제 Trino relation 확인
2. `extract.py`로 `catalog_snapshot.json` 갱신
3. source/role 자동 추론 확인
4. 꼭 필요한 경우에만 `CURATED` 라벨·기본 추천 추가
5. 특정 테이블 하드코딩 없이 동작하는지 확인

### 10.4 카탈로그 API 변경

1. 스냅샷 필드의 생성 근거 확인
2. `app/models.py` 계약 변경
3. `app/main.py`의 얇은 조회 로직 변경
4. 기존 클라이언트/화면 호환성 검증
5. README API 표와 스냅샷 생성 절차 갱신

---

## 11. 검증 게이트

변경 범위에 맞는 검증을 실행하고, 실패를 남긴 채 완료로 보고하지 않는다.

### 정적·문법

```bash
# Python
.venv/bin/python -m compileall -q app extract.py
.venv/bin/python -m pytest -q

# JavaScript
node --check app/static/charts/js/*.js
node --check app/static/auth/*.js
```

### 서버·API

```bash
.venv/bin/uvicorn app.main:app --port 8765
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8765/api/v1/public/summary
```

### 브라우저

```text
http://127.0.0.1:8765/charts?selftest=1
```

- 탭 제목 `SELFTEST_ALL_PASS` 확인
- UI/시각화 변경은 1600×1100 기준 스크린샷으로 육안 검증
- 지도 변경은 매칭 커버리지와 코드 체계 확인
- 자동 갱신 변경은 `?refresh=<초>`로 가속 검증
- 레이아웃 변경은 생성·편집·저장·취소·복제·삭제·순서변경 회귀 확인

### 문서·커밋 위생

```bash
git status --short
git diff --check
```

- `.venv/`, `__pycache__/`, `data/`, `app/charts/data/cache/`가 포함되지 않았는지 확인한다.
- 문서의 페이지 수·도표 수·경로·포트가 코드와 맞는지 확인한다.

---

## 12. 이슈·브랜치·커밋·PR

작업 추적 순서는 **GitHub 이슈 → 이슈 번호 브랜치 → 커밋 → PR**이다.

1. 이슈 제목: `[Feat]`, `[Bug]`, `[Docs]`, `[Chore]` + 한국어 요약
2. 브랜치: `{type}/{issue-number}-{english-kebab-slug}`
   - 기능: `feat/123-commerce-charts-studio`
   - 수정: `fix/124-stale-cache-fallback`
   - 문서: `docs/125-dashboard-agent-guide`
3. 기준 브랜치는 원칙적으로 `dev`
4. 커밋은 한국어 conventional 형식:
   - `feat: 요약 — 상세`
   - `fix: 요약 — 상세`
   - `docs: 요약 — 상세`
   - `demo: 요약 — 상세`
5. PR 제목 권장: `type(scope): 한국어 요약 (#issue)`
6. PR 본문에 `Closes #<issue-number>`를 넣어 머지 시 이슈를 자동 종료한다.

`test/...` 브랜치는 dev에 바로 합치지 않기로 명시적으로 격리한 실험에만 사용한다.
정식 PR을 올릴 때는 추적 가능한 이슈 번호 브랜치로 정리한다.

커밋은 논리 단위로 만들 수 있지만, 원격 push·PR 생성은 사용자 승인 후에만 수행한다.

---

## 13. 최종 품질 체크

- [ ] 실행 모드를 선택했는가
- [ ] `dashboard/` 작업 경계를 지켰는가
- [ ] `/catalog` 스냅샷 경로와 `/charts` live 경로를 혼동하지 않았는가
- [ ] Charts Studio 격리를 유지했는가
- [ ] source/field/agg/operator 화이트리스트를 우회하지 않았는가
- [ ] 결측·비가산 집계·지역 코드 체계를 왜곡하지 않았는가
- [ ] 캐시와 stale 상태를 정직하게 표시하는가
- [ ] 런타임 파일과 시크릿이 커밋에 포함되지 않았는가
- [ ] Pydantic/API/문서 계약이 함께 맞는가
- [ ] 변경 범위에 맞는 문법·API·셀프테스트·스크린샷 검증을 했는가
- [ ] 이슈 번호 브랜치와 `Closes #...` PR 규칙을 따랐는가
