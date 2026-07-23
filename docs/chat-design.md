# Ask Chat 설계 — 온톨로지·D1(SQLite) 근거 조회 기반 LLM 채팅

`/chat` 화면과 `app/chat/` 백엔드 번들의 설계 정본이다. 목표는 **축적된 gold 데이터(D1/SQLite
서빙 스냅샷)를 근거로만 답하는** ChatGPT/Claude 스타일 채팅이다. LLM에게 자유 SQL을 주지 않고,
**한정된 조회 스펙**(테이블·컬럼 화이트리스트 + 필터 + 제약 집계)만 실행을 허용하며, 응답에는
실행된 조회 근거를 함께 노출한다.

```text
사용자 질문
   │  POST /api/v1/chat/messages (SSE 스트리밍)
   ▼
app/chat/service.py  ── 시스템 프롬프트(온톨로지 카드, prompt cache) + 도구 루프
   │        ▲
   │ tool: query_gold(제약 스펙)              Claude API (스트리밍, urllib)
   ▼        │
queryspec.py  스펙 검증 → SQL+바인딩 조립 (화이트리스트·LIMIT 캡)
   ▼
datasource.py  D1 REST(기본) 또는 로컬 SQLite(mode=ro) — 읽기 전용
   ▼
_catalog / gold_* 테이블  ←— serving/export_*_to_d1.py 가 적재 (상류 계약)
```

---

## 1. 결정 사항 (결정 근거 포함)

| # | 결정 | 선택 | 근거 · 기각한 대안 |
|---|---|---|---|
| D1 | LLM | **Claude API**, 기본 모델 `claude-opus-4-8` (`CHAT_LLM_MODEL`로 변경) | 스트리밍+도구 호출+프롬프트 캐시 요구. 모델은 env로 교체 가능(비용 절감 시 `claude-sonnet-5` 등) |
| D2 | LLM 클라이언트 | **표준 라이브러리 urllib 직접 구현** (`app/chat/llm.py`) | ① 이 저장소 실행기 관례(trino.py — "표준 라이브러리만으로 REST") ② 새 패키지 승인 절차 불필요(SHARE §2) ③ **실측 제약**: 개발 PC의 Application Control 정책이 anthropic SDK 의존 `jiter` 네이티브 DLL 로드를 차단해 SDK가 import조차 실패. SDK 전환이 필요해지면 `llm.py` 한 파일만 교체하면 된다 |
| D3 | 데이터 접근 | **Cloudflare D1 REST API 직접**(기본) + **로컬 SQLite 파일**(대안, `CHAT_SQLITE_PATH`) | 상위 `sample/.env`의 `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`/`D1_UUID`로 즉시 접속 가능. 기각: Worker API 경유(배포 의존·인증 없음), `CHARTS_DATASOURCES` 등록(charts 온톨로지에 소스가 섞이고 로컬 파일 필요) |
| D4 | RAG 형태 | **벡터 임베딩 없이** "온톨로지 카드(시스템 프롬프트) + 제약 조회 도구" | 정형 데이터 + 소규모 테이블 카탈로그(현재 수십 종)라 임베딩 검색이 불필요. 카드 전체가 프롬프트 캐시에 실려 반복 비용이 낮다. 테이블이 수백 종으로 늘면 검색 단계 도입을 재검토 |
| D5 | 요청 포맷 한정 | LLM에 **자유 SQL 금지** — `query_gold` 도구의 스펙만 허용 | 스펙 = 테이블·컬럼 화이트리스트(`_catalog` 실측) + 필터 연산 화이트리스트 + 제약 집계(count/sum/avg/min/max) + LIMIT 캡, 값은 전부 파라미터 바인딩. serving Worker(`/data/{table}`) 계약과 동형이며 집계를 추가한 형태. 프롬프트 주입이 있어도 임의 SQL로 승격되는 경로가 없다 |
| D6 | 온톨로지 정본 | **D1 `_catalog`**(dbt manifest 파생) 1순위 → 없으면 `sqlite_master`+`pragma_table_info` 폴백 → **dashboard 스냅샷으로 보강**(컬럼 설명·코드→한글 라벨) | "SQLite가 아직 완성되지 않은" 현재 상태에서도 동작. 번들 간 공유는 SHARE §5 규약대로 `snapshot/catalog_snapshot.json` **파일**로만 하고 charts 코드는 import하지 않는다 |
| D7 | 대화 저장 | **서버 무저장** — 무상태 API(클라이언트가 전체 이력 전송), 브라우저 localStorage 보관 | 1차 범위 최소화. RDB 저장·대화 공유는 후속(§6) |
| D8 | 인증·권한 | 기존 체계 그대로 — 페이지 키 `chat` 신설, **member 이상** 허용 | `page_key_for_path`에 `/chat`·`/api/v1/chat` 등록 → 로그인 게이트·로컬 자동 로그인·no-store 캐시 헤더가 기존과 동일하게 적용. CSRF/레이트리밋/보안 헤더도 미들웨어가 처리 |
| D9 | 비용·부하 상한 | `CHAT_LLM_MAX_TOKENS`(16000) · 도구 호출 상한 `CHAT_MAX_TOOL_CALLS`(6) · 조회 행 상한 `CHAT_QUERY_ROW_LIMIT`(200) · 동시 스트림 `CHAT_MAX_CONCURRENT`(2) | LLM 종량 과금·DB 부하·컨텍스트 크기를 env로 통제. 초과 시 429/스펙 오류로 정직하게 반환 |
| D10 | 격리 | `app/chat/` + `app/static/chat/` 번들 — 삭제하면 기능 전체가 제거되는 구조 | Charts Studio 격리 불변식(SHARE §5)과 동일. 본체 접점은 §4의 4곳뿐 |

## 2. 사용자(팀) 결정이 필요한 항목

| 항목 | 현재 상태 | 필요 결정 |
|---|---|---|
| **Anthropic API 키** | 미설정 — 화면은 뜨고 미설정 안내가 표시됨 | 키 발급 주체·예산 한도. `.env.local`(또는 배포 secret)에 `ANTHROPIC_API_KEY` 주입 |
| 모델·비용 정책 | 기본 `claude-opus-4-8`(입력 $5/출력 $25 per 1M tok) | 데모/운영별 모델 선택. 비용 민감하면 `CHAT_LLM_MODEL=claude-sonnet-5` |
| D1 적재 범위 | `_catalog`에 citydata 12종 + transit 6종 (serving/ 프로토타입) | gold 전 도메인으로 확장할지 — 상류 `serving/` export의 DAG 승격 작업(대시보드 범위 밖) |
| 접근 권한 | member 이상 (`DEFAULT_ROLE_ACCESS`) | guest 허용 여부. 운영 콘솔 '접근 관리'에서 역할별로 조정 가능 |
| 대화 서버 저장 | 저장 안 함(localStorage) | 이력 서버 보관·팀 공유가 필요해지면 후속 이슈로 |
| 레이트리밋 세분화 | 일반 authenticated 버킷 | LLM 전용 카테고리(`chat_query`)를 정책에 신설할지 |

## 3. 번들 구성 (`app/chat/`)

| 파일 | 책임 |
|---|---|
| `config.py` | env 로딩(지연 평가 — import 시 네트워크·필수값 강제 없음). `CHAT_D1_*`가 없으면 상위 `sample/.env` 이름(`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`/`D1_UUID`)을 폴백으로 읽는다 |
| `datasource.py` | 읽기 전용 실행기. D1 REST(urllib, Bearer, 파라미터 바인딩) / 로컬 SQLite(`mode=ro`+`PRAGMA query_only`, `app/` 하위 경로 거부 — charts 규약과 동일). `_catalog` 조회·`pragma_table_info` 폴백 포함 |
| `ontology.py` | 온톨로지 카드 빌드(+TTL 캐시): `_catalog` ∪ 스냅샷 보강(컬럼 설명·code_labels) → 시스템 프롬프트(한글) 생성 |
| `queryspec.py` | `query_gold` 스펙 검증 → `(SQL, params)` 조립. 식별자 정규식+화이트리스트, 연산·집계 화이트리스트, LIMIT 캡, LIKE 이스케이프(`ESCAPE '\'`) |
| `llm.py` | Claude Messages API 스트리밍 클라이언트(urllib·SSE 파서·콘텐츠 블록 누적기). adaptive thinking, 시스템 프롬프트 prompt cache(`cache_control`) |
| `service.py` | 턴 오케스트레이션: 도구 루프(≤N회), SSE 이벤트 생성(`delta`/`thinking`/`tool`/`tool_result`/`done`/`error`), 조회 근거 수집 |
| `models.py` | `ChatRequest`/`ChatMessage` Pydantic 계약(제어문자·크기 제한), meta 응답 |
| `router.py` | `GET /api/v1/chat/meta` · `POST /api/v1/chat/messages`(StreamingResponse, `text/event-stream`). `Depends(require_page("chat"))`, 동시성 세마포어(초과 시 429 problem+json) |

프론트(`app/static/chat/`): `index.html`(인라인 스크립트 0 — CSP 해시 등록 불필요),
`chat.css`(charts 디자인 토큰 재사용), `js/api.js`(meta는 `AuthUI.api`, 스트리밍은 전용 fetch
리더 + `AuthUI.csrfToken()`), `js/render.js`(이스케이프 우선 마크다운 라이트), `js/app.js`
(대화 목록 localStorage·스트리밍 상태 머신).

## 4. 본체 접점 (이 4곳 외 기존 코드 무변경)

1. `app/main.py` — `chat_router` include + `GET /chat` 화면 라우트
2. `app/auth/middleware.py` `page_key_for_path` — `/chat`, `/static/chat/index.html`, `/api/v1/chat` → `chat`
3. `app/auth/service.py` — `PAGE_DEFINITIONS`·`DEFAULT_ROLE_ACCESS`에 `chat` 키(시딩은 매 기동 멱등 — SCHEMA_VERSION 범프 불필요)
4. `app/inputguard.py` — `GET /chat`, `GET /api/v1/chat/meta`, `POST /api/v1/chat/messages`(`PYDANTIC:ChatRequest`) 등록

## 5. 안전 계약

- **읽기 전용 2중 강제**: SQLite는 `mode=ro`+`query_only`, D1은 API 토큰 권한 + 단일 SELECT만 조립.
- **내부 DB 차단**: SQLite 경로는 `app/`·런타임 `data/`·`DATABASE_URL` 인증 DB를 거부하고,
  카탈로그 폴백은 `auth_`/`_`/`sqlite_`/`d1_` 접두 테이블을 제외한다(서빙 gold 만 노출).
- **식별자는 값이 될 수 없고 값은 식별자가 될 수 없다**: 테이블/컬럼/별칭은 정규식+실측
  화이트리스트, 값은 전부 바인딩(`?`). 오류 메시지에 토큰·계정 정보 미포함(서버 로그만).
- **LLM 출력 = 신뢰 불가 입력**: 도구 입력은 서버가 재검증(스펙 위반 → 도구 오류로 모델에 반환).
- **프론트는 외부 호출 불가**(CSP `connect-src 'self'`) — LLM·D1 호출은 전부 백엔드 경유.
- 조회 근거(스펙·SQL·행 수)를 응답 이벤트에 포함해 집계 재현 가능(SHARE §3.2와 같은 사상).

## 6. 후속 확장 (이번 범위 밖)

- 대화 서버 저장·공유, LLM 전용 레이트리밋 카테고리, D1 gold 전 도메인 적재(상류),
  테이블 수백 종 규모의 카드 검색(RAG 검색 단계), 차트 자동 생성 연동(Charts spec 반환).

## 7. 실행·검증

```powershell
# .env.local에 추가(§ .env.local.example 참조): ANTHROPIC_API_KEY, CHAT_D1_* (또는 CHAT_SQLITE_PATH)
powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
# → http://127.0.0.1:8765/chat (로컬 운영자 자동 로그인)
```

검증 게이트: `python -m compileall -q app extract.py` · `python -m pytest -q`(inputguard
라우트 등록 검증 포함) · `/health` · `/api/v1/chat/meta`(미설정 시 `configured:false`) ·
`/chat` 화면 수동 확인.
