# 07 — 인수인계 지침 (이 문서만 읽고 그대로 구현할 수 있게)

중단된 온톨로지 작업을 이어받는 사람을 위한 지침이다. **핵심 개념 → 불변식(어기면 안 되는 것) →
남은 작업(파일·함수·수용 기준까지) → 검증 절차 → 참조 링크** 순으로, 이 문서만 읽고 바로
구현할 수 있게 적었다. 배경 판단은 [01](01-ontology-verdict.md)·[02](02-gaps-and-remediation.md),
활용 목록은 [03](03-usage-patterns.md)에 있다.

---

## 0. 지금 상태 (무엇이 끝났고 무엇이 남았나)

**끝난 것 (브랜치 `ontolog-middle` 반영·테스트 통과)**

| 영역 | 산출물 |
|---|---|
| MCP/AI 도구 표면 | `app/charts/agent_tools.py` — 7개 도구(`list_sources`·`describe_source`·`plan_query`·`run_query`·`list_metrics`·`run_metric`·`ontology_manifest`), `tool_schemas()`(=MCP inputSchema), `call_tool()` 디스패치, env 설정부 `AgentToolsConfig` |
| 온톨로지 보강 | geo part-of(`GEO_PARENT`) + **실행 가능한 롤업**(`rollup_columns`), 역할 택소노미(`ROLE_CONCEPT`/`CONCEPT_PARENT`), 3치 가산성(`additivity()`), JSON-LD/SKOS 내보내기(`ontology_export`), 명명 지표(`NAMED_METRICS`) |
| 안전·정확성 하드닝 | 센티널 기반 `truncated`, 결정적 기본 ORDER BY, 실행 불가 롤업 광고 제거, `assert_select_only`, `call_tool` 미광고 인자 차단(`force` 차단), `value_labels` 상한, **표시축→코드축 승격**(`promote_dims_to_codes`) |
| 예제 | `examples/ai_analyst_example.py`(Anthropic tool-use 루프), `examples/mcp_ontology_server.py`(MCP 서버), `examples/README.md` |
| 문서 | `docs/ontology/01~07` |
| 테스트 | `tests/test_agent_tools.py` 24개 — 전체 스위트 **169 passed** |
| 부수 정정 | 카탈로그 좌측탭(Ask Chat 추가·운영 관리 개칭·Service Health 제거), 시드 5건 비가산 sum 정정, 정적 HTML 인라인 핸들러/`rel=noopener` 정정 |

**2차로 끝난 것 (P1~P4·P6·P7·P9 — 커밋 `41e94eb` 이후)**

| 항목 | 결과 |
|---|---|
| **P2 비율 지표** | `agg="ratio"`(`sum(num)/nullif(sum(den),0)`) 추가. 6개 방언 렌더 확인, 분자·분모는 가산 measure 로 제한, SELECT·HAVING 이 같은 화이트리스트 공유. `cohort_survival_rate` 를 가중식으로 교정(+`require_dims`), `early_close_ratio` 추가 |
| **P1 가산성 집행** | `additive_over` 를 온톨로지 정본으로 두고 querybuilder 가 집행 — 재고성 측정값 + 시간축 + `sum` 거부. 재고의 `preferred_agg` 를 `avg` 로 바꿔 **온톨로지가 스스로 거부할 조합을 추천하지 않게** 함(자기정합성 테스트 포함) |
| **P3 last_n 상한** | 연·월·일/시각 전 granularity 를 닫힌 구간으로 — 예보 테이블(25/112)의 미래 유입 차단 |
| **P4 소스별 라벨** | `value_labels` 를 소스별로 키잉(전역 병합은 하위호환 폴백). `gu_code` 는 MOIS 표준을 정본으로 승격해 **잘못된 구 표기 210건** 해소. `SourceDetail.value_labels` 추가(목록 응답 제외) |
| **P6 비용 게이트** | 실측 통계로 그룹 카디널리티 추정 후 실행 전 거부(`cost_rejected` + 롤업/필터 힌트). 통계가 없으면 통과, 상한에서 곱셈 조기 종료 |
| **P7 검색·역해결** | `search_ontology`(한글 2-gram 포함)·`resolve_label`(한글→코드, 동명이지역은 후보 전부 + `ambiguous`). 외부 의존성 없음 |
| **P9 출처·계약버전** | `provenance`(refresh_mode·observed_at·lineage_captured·contract_enforced), `run_query.cached_at` 전파, 매니페스트 `contract_version`/`contract_hash`(런타임 설정과 무관하게 안정) |

검증: pytest **188 passed**(신규 19), 전수 build **968/968**, `node --check`, `compileall`,
`git diff --check`. 도구 fuzz(잘못된 타입·거대값·유니코드) 결과 **예외 0건**(전부 error dict).

**아직 남은 것** — §3 참조. 요약하면 `k-익명성(P5)` · `시간 그레인 롤업(P8)` · `P10 잡항목`,
그리고 아래 두 가지 후속:

- **프론트 라벨 스코프**: 백엔드는 소스별 라벨을 서빙하지만 프론트(`render.js` `vlabel`,
  `app.js`)는 여전히 전역 `meta.value_labels` 를 읽는다. 잔여 불일치는 **표기 변형**
  (`묵제1동`↔`묵1동`) 1,345건으로 *잘못된 장소가 아니라 철자 차이*라 Minor 다. 전환하려면
  소스 상세의 `value_labels` 를 우선 쓰도록 바꾸고 **반드시 `/charts?selftest=1`
  (`SELFTEST_ALL_PASS`)까지 확인**한다(SHARE §11).
- **recommend.js**: 서버 `preferred_agg` 가 재고에서 `avg` 로 바뀌었으므로 프론트 추천의
  이름 패턴 규칙(`active…`→합계)도 같은 방향으로 맞추면 UX 가 일관된다.

---

## 1. 핵심 개념 (이것만 이해하면 나머지는 따라온다)

### C1. 이건 "온톨로지"가 아니라 **시맨틱 레이어 + role 레지스트리**다
형식(OWL/DL) 요건 — 클래스 택소노미·객체속성·공리·개체(A-Box)·추론기·URI 직렬화 — 대부분이 없다.
있는 것은 **컬럼에 의미역(role)을 붙인 통제 어휘 + 집계 의미 + 도표 슬롯 계약**이다.
→ **함의:** 형식 온톨로지로 승격하려 들지 말 것(가치 0, [02 §A](02-gaps-and-remediation.md)).
대신 **선언을 소비자(LLM/MCP)에게 정확히 노출**하고 **관계를 실행 가능하게** 만드는 데 투자한다.

### C2. 식별은 **코드**, 표기는 **한글**
`gu_code`↔`gu`, `admin_dong_code`↔`admin_dong`, `X`↔`X_ko` 처럼 코드/표시 쌍이 있으면
**GROUP BY 는 반드시 코드**다. 이름으로 묶으면 동명이지역(신사동: 강남구·관악구)이 한 행으로
합쳐져 **조용히 틀린다**. 표시는 `value_labels`(코드→한글, 중복명은 `신사동·강남구`로 유일화).
→ 코드에서: `ontology.companion_pairs()`, 필드의 `id_field`/`label_field`,
`agent_tools.promote_dims_to_codes()`(에이전트 경로), 프론트 `effectiveBindings`(화면 경로).

### C3. 도표 타입 = **슬롯 계약**, 지원 여부 = **이분 매칭**
`CHART_TYPES[t].slots[i].accepts` 가 받는 role 을 선언하고, 소스가 그 타입을 지원하는지는
"필수 슬롯마다 **서로 다른 실재 필드**를 배정할 수 있는가"로 판정한다(`compatible_bindings`).
→ 컬럼명 하드코딩이 없으므로 새 gold 가 들어와도 자동으로 표현 가능성이 계산된다.

### C4. 집계 의미(가산성)가 **안전 계약**이다
필드마다 `preferred_agg`·`additive`·`allowed_aggs`·`cumulative_safe` 를 부여하고,
`querybuilder._measure_expr` 가 `allowed_aggs` 밖 집계를 **SQL 조립 전에 거부**한다.
비율·평균·순위·LQ·온도 같은 비가산 필드는 `sum` 이 아예 목록에서 빠진다.
→ 이것이 LLM 환각을 막는 **구조적** 장치다(프롬프트가 아니라 코드가 막는다).

### C5. 스펙은 **백엔드 중립**, 방언만 갈아끼운다
온톨로지·스펙(dims/measures/filters/having/order_by/limit)은 DB 를 모른다. 소스의
`backend`(방언 키)·`datasource`(연결 이름)가 `querybuilder.DIALECTS` 프로파일과
`backends.execute` 실행기를 고른다. Trino·Postgres·SQLite(=**Cloudflare D1**)·MySQL·Oracle·
MSSQL·DuckDB 지원. 같은 스펙 → 방언별 SQL → **동일 결과**([05 실증](05-backend-portability.md)).

### C6. 온톨로지가 곧 **MCP tool 계약**이다
`tool_schemas()` 는 Anthropic `tools` 이자 MCP `inputSchema` 다(동형). 그래서 MCP 가 요구하는
"기계 판독형 JSON-Schema"를 온톨로지가 그대로 공급한다 — **형식 온톨로지(OWL)의 부재는 MCP
가치와 무관**하다. 레거시(`app/chat/`)는 이걸 손으로 중복 구현해 드리프트한다([04](04-mcp-vs-legacy-and-empirical.md)).

---

## 2. 불변식 (어기면 회귀가 난다)

| # | 불변식 | 이유 / 확인법 |
|---|---|---|
| I1 | **trino 방언 SQL 은 byte-동일** | 캐시 키가 SQL 원문 해시다. 1바이트만 바뀌어도 전체 캐시가 콜드스타트. `querybuilder` 렌더 경로를 고칠 때는 trino 출력 diff 를 반드시 확인 |
| I2 | **단일 relation** | `build()` 는 `select … from <relation>` 만 만든다. JOIN·서브쿼리·윈도우 없음. 소스 간 조인이 필요하면 gold 에서 미리 조인(이미 x-테이블 다수 존재) |
| I3 | **식별자는 화이트리스트, 값은 이스케이프** | 사용자/AI 입력이 식별자로 들어갈 경로가 없어야 한다. 새 기능이 컬럼명을 문자열로 받으면 반드시 레지스트리 대조 |
| I4 | **실행은 읽기 전용** | 방언별 세션 강제(`read_only`/`query_only`/`READ ONLY`) + `assert_select_only`. 새 백엔드 추가 시 둘 다 |
| I5 | **에러는 예외가 아니라 결과** | 도구는 `{"error": …}` 를 돌려준다. 에이전트 루프가 죽으면 자가수정이 불가능하다 |
| I6 | **자격증명은 env, 코드엔 절대 금지** | `CHARTS_DATASOURCES` 의 `dsn_env` 는 값이 아니라 **환경변수 이름**(간접 참조) |
| I7 | **작업 경계는 `dashboard/`** | 상위 `sample/`·`dags/`·`dbt/`·Compose 변경은 분리해 사유·영향·대안을 먼저 합의(SHARE.md §2) |
| I8 | **산출물은 한국어** | 문서·PR·응답·주석. 코드 식별자는 영문 |
| I9 | **원격 push·PR 은 승인 후** | 단, 현 작업은 사용자가 `ontolog-middle` 브랜치 push 를 명시 허용(이슈/PR 생성은 하지 않음) |

**변경 위험도 판정법**: `app/charts/ontology.py`·`querybuilder.py` 는 **코어**다(계약·캐시 영향).
`agent_tools.py` 는 **얇은 상층**이라 자유도가 높다. 가능하면 상층에서 해결하고, 코어를 고칠 땐
I1 을 먼저 검토한다.

---

## 3. 남은 작업 (우선순위 · 구현법 · 수용 기준)

> 표기: **[코어]** = ontology/querybuilder 수정(캐시·계약 영향 검토 필요), **[상층]** = agent_tools 만 수정.

### P1. 가산성 **집행**을 차원 인지형으로 — 3등급, [코어]
**문제**: `additive` 가 2치라 "구별로는 합산 가능하지만 시간축으로 합산하면 이중계산"인
**semi-additive**(재고·정원·활성수: `seat_count`, `active_cnt`, `occupancy`)를 표현하지 못한다.
지금은 `additivity()`(상층)가 3치로 *알려주기만* 하고 `querybuilder` 는 막지 않는다.

**구현**
1. `ontology._measure_semantics` 에 `additive_over: list[str]` 추가(예 `["geo","category"]`,
   시간 제외). 판정 근거: 이름 패턴(`_count`/`cnt`/`total` + 스냅샷성 소스) + `cumulative_safe`.
2. `querybuilder._measure_expr` 시그니처에 그룹 차원의 role 집합을 전달하고,
   `agg=="sum"` 이면서 semi-additive 필드 + 그룹에 `time`/`sequence` role 이 있으면 `SpecError`.
3. 메시지는 대안을 제시: "…는 시간축과 함께 합산할 수 없습니다. avg/max 를 쓰거나 시간축을 빼세요."

**주의(I1)**: 거부 로직만 추가하면 **통과하는 SQL 의 텍스트는 안 바뀐다** → 캐시 안전.
**수용 기준**: 시간축 + `seat_count` sum → `spec_error`; 같은 필드 + 구축만 → 통과.
기존 시드/레이아웃이 깨지지 않는지 `tests/test_charts_ontology.py` 재실행.

### P2. 비율(ratio) 측정값 형태 — 3등급, [코어]
**문제**: `survival_rate`·`lq`·`share_in_gu` 같은 사전계산 비율을 여러 행에 걸쳐 보려면
지금은 `avg`(비가중 평균)뿐이라 **통계적으로 틀린다**(코호트 크기 무시).
`NAMED_METRICS["cohort_survival_rate"]` 가 정확히 이 오류다.

**구현**
1. measure 스펙에 `{"agg":"ratio","num":<필드>,"den":<필드>}` 형태 추가.
2. `_measure_expr` 에서 `sum(num) / nullif(sum(den), 0)` 로 렌더(방언별 nullif 는 전부 지원).
   `num`/`den` 은 **가산 필드만** 허용(화이트리스트 재사용).
3. `NAMED_METRICS` 를 `kind:"ratio"` 로 확장하고 `cohort_survival_rate` 를
   `sum(survivors)/sum(cohort_n)` 로 교체(원자 컬럼이 gold 에 있는지 먼저 `describe_source` 로 확인,
   없으면 지표를 제거하고 "원자 컬럼 필요" 로 문서화 — **틀린 지표를 남기는 것이 최악**).
4. `tool_schemas` 의 `_SPEC_MEASURE` 에 ratio 분기를 `anyOf` 로 추가.

**수용 기준**: 가중 결과가 비가중 `avg` 와 다름을 보이는 테스트 1건 + 비가산 필드를 `den` 으로
쓰면 거부.

### P3. `last_n` 상한 클램프 — 3등급, [코어]
**문제**: `querybuilder._last_n_condition` 이 `col >= cutoff` 만 낸다. 예보/미래 일자 테이블
(112개 중 25개가 `date_range.max` 미래)에서 "최근 7일"이 **미래 예보까지 포함**한다.

**구현**: 조건을 닫힌 구간으로 — `col >= cutoff AND col <= <today>`.
**I1 경고**: 이건 **통과 SQL 텍스트를 바꾼다** → `last_n` 을 쓰는 저장물의 캐시가 1회 콜드스타트.
영향 범위가 작으므로 수용 가능하나, 커밋 메시지에 명시할 것.
**대안(캐시 무영향)**: 상층에서 `run_query` 가 `last_n` 필터를 만나면 상한 필터를 **추가로** 붙인다.
코어를 안 건드리는 대신 이중 조건이 SQL 에 남는다 — 팀 합의로 택일.
**수용 기준**: `forecast_date` 에 `last_n=7` → 렌더 SQL 에 상·하한 둘 다.

### P4. `value_labels` 를 **(소스, 필드)** 로 키잉 — 3등급, [코어]
**문제**: `ontology.Registry._build` 가 모든 테이블의 `code_labels` 를 **필드명 하나로 전역 병합**한다.
서로 다른 소스가 같은 필드명(`category`, `dataset`)에 다른 코드 체계를 쓰면 라벨이 섞인다.

**구현**: `self._code_labels[(table, field)]` 로 저장 → `meta()`/`describe_source` 는 해당 소스 것만
투영. 전역 `VALUE_LABELS`(정적 큐레이션)는 소스별 오버라이드로 강등.
**수용 기준**: 두 소스가 같은 필드명·다른 코드일 때 각자 라벨만 보이는 테스트.

### P5. k-익명성(최소 셀) + 감사 로그 — 3등급, [상층+코어]
**문제**: 세밀 지오(`admin_dong_code` × `category`) 그룹에 최소 셀 억제가 없다. 소규모 셀이
재식별 위험. 성공 질의에 대한 감사 로그도 없다.

**구현**
1. 소스별 `min_cell`(기본 5)과 필드별 `sensitivity`(`public|quasi_id|sensitive`)를 큐레이션에 추가.
2. `run_query`(상층)에서 민감 조합이면 `having` 에 `count(*) >= min_cell` 을 **자동 주입**하고
   응답에 `suppressed: true` 를 명시(조용한 억제 금지).
3. 성공 질의도 구조화 로그(주체·소스·차원·행수·elapsed)를 남긴다.

**수용 기준**: dong×category 질의에 HAVING 자동 주입 + 응답에 억제 사실 표기.

### P6. 사전 비용·카디널리티 게이트 — 3등급, [상층]
**문제**: 실행 전 비용 추정이 없어 대형 스캔이 그대로 나간다.
**구현**: `describe_source` 가 이미 주는 통계로 상층에서 추정 —
`예상 그룹수 = ∏ distinct_count(각 dim)`, 구간축은 `ceil((max-min)/bin_width)`.
임계 초과 시 실행 대신 `{"error":"cost_rejected", "hint": …}` + 대안(상위 축 롤업·필터 추가) 제시.
또한 큰 소스에 시간 필터가 없으면 경고(파티션 프루닝 유도).
**수용 기준**: 고카디널리티 조합이 실행 전에 거부되고 대안이 제시됨.

### P7. 검색·역해결 도구 — 3등급, [상층]
**문제**: 112개 소스를 **열거만** 할 수 있어 LLM 이 소스 선택에서 헤맨다. 또 "강남구"라는
사람 말을 코드(`11680`)로 되돌리는 경로가 없다(`value_labels` 는 코드→한글 단방향).

**구현**
1. `search_ontology(query, k)` — 소스명/라벨/설명 + 필드 라벨을 오프라인 인덱싱(간단한 토큰 점수로
   충분, 외부 의존성 금지). 반환은 소스+근거 필드.
2. `resolve_label(text, field?|source?)` — `value_labels` 역인덱스로 한글→코드. 동명이면 후보를
   전부 돌려주고 상위 지역까지 표기(`신사동·강남구`).
3. 둘 다 `tool_schemas`/`TOOL_DISPATCH` 에 등록.

**수용 기준**: "강남구" → `{field:"gu_code", code:"11680"}`; 동명 입력 시 후보 2건 이상 반환.

### P8. 시간 그레인 롤업 — 3등급, [코어]
**문제**: geo 는 part-of 가 생겼는데 **시간은 롤업이 없다**(일→월→분기→연). 시맨틱 레이어에서
가장 많이 쓰는 축인데 매번 다른 소스를 골라야 한다.
**구현**: dim 의 제3형태 `{"field": <time>, "grain": "month"}` 를 추가하고 방언별
`date_trunc`/`substr` 로 렌더. 별칭은 필드명 유지(소비자 불변).
**수용 기준**: `date` 축에 `grain:"month"` → 월 단위 그룹, 방언 6종 렌더 확인.

### P9. 신선도·계보·계약버전 노출 — 2등급, [상층]
- 스냅샷의 `refresh.mode`·`domain_generated_at`·`contract_enforced`·`lineage`(33/112만 존재)를
  `describe_source` 에 실어 에이전트가 stale/부분 통계를 **알고** 답하게 한다.
- `run_query` 응답에 `cached_at` 전파(현재 누락) — `mode=cache/stale` 의 나이를 알 수 있어야 한다.
- `ontology_manifest` 에 `contract_version`(수동 semver) + `contract_hash`(계산값)를 넣고
  CI 에서 해시 드리프트 시 실패 → 자동 파생 표면을 버전 계약으로 만든다.

### P10. 그 외 (2등급 이하, [03](03-usage-patterns.md) 참조)
`rows`/`labeled_rows` 중복 제거 · 필드 `desc` 전문 노출 · `having`/필터그룹 스키마 정밀화 ·
단위(`unit`/`quantity_kind`) 부여 · NULL 정렬 정규화 · 물리 date 컬럼 타입 비교(캐스트 제거로
파티션 프루닝) · 접근성(`aria`/`decal`/대체 테이블) · 에이전트 전용 Trino 레인 분리.

---

## 4. 검증 절차 (매 변경 후)

```bash
# 1) 구문·계약
.venv/bin/python -m compileall -q app extract.py examples
.venv/bin/python -m pytest -q                    # 현재 기준 169 passed

# 2) 온톨로지 전수 하네스 (DB 없이도 build 전수는 돈다)
#    - 모든 소스 × 지원 도표에 대해 compatible_bindings → build 성공 여부
#    - 라이브 Trino 가 있으면 도메인×도표 표본 실행까지
#    기준선: build 968/968, live 68/68, 필터연산자 177/177, 비가산 가드 3/3

# 3) 이식성 (D1 대체 검증)
#    같은 스펙을 trino / sqlite 소스로 각각 build+execute → 행 동일 확인
```
런타임 확인이 필요한 변경(인증·화면·차트)은 서버를 띄워 `/health` 와 해당 화면,
그리고 `/charts?selftest=1` 의 탭 제목 `SELFTEST_ALL_PASS` 까지 본다(SHARE.md §11).

**환경**: 이 저장소의 기본 셸은 Windows PowerShell 이다. macOS/Linux 에서는 위 bash 경로
(`.venv/bin/...`)를, Windows 에서는 `.venv\Scripts\...` 와 `scripts\run_local.ps1` 을 쓴다.
`AUTH_MODE=local_auto` 는 `.env.local` 이 프로세스 env 에 주입돼야 적용된다(안 그러면 로그인 화면).

---

## 5. 참조 링크

**내부 (먼저 읽을 것)**
- 규약 정본 `SHARE.md` (§0.1 두 환경 대응표 · §2 경계 · §6 온톨로지·드리프트 · §9.0 다중 백엔드 · §11 검증 · §12 이슈/브랜치/PR)
- `docs/charts-design-intents.md` — 설계 의도 B(온톨로지)·C(데이터 경로)·D(시각화 규칙)
- 코어: `app/charts/ontology.py`(role·CHART_TYPES·value_labels) · `querybuilder.py`(안전 SQL·방언) ·
  `backends.py`(실행기) · `models.py`(Pydantic 계약) · `router.py`(HTTP 경계)
- 상층: `app/charts/agent_tools.py` · `examples/` · `tests/test_agent_tools.py`
- 레거시 대비군: `app/chat/`(ontology·queryspec·llm 을 손으로 중복 구현한 사례)

**외부 — 개념 근거**
- Gruber/Studer/Guarino 정의 논쟁 — https://keet.wordpress.com/2017/01/20/on-that-shared-conceptualization-and-other-definitions-of-an-ontology/
- Guarino, *What is an Ontology* — https://iaoa.org/isc2012/docs/Guarino2009_What_is_an_Ontology.pdf
- 온톨로지 구성요소 — https://en.wikipedia.org/wiki/Ontology_components
- SKOS(개념 스킴·broader) — https://www.w3.org/TR/skos-reference/ · JSON-LD — https://www.w3.org/TR/json-ld11/ · SHACL — https://www.w3.org/TR/shacl/
- Kimball 가산성(additive/semi-additive/non-additive) — https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/additive-semi-additive-non-additive-fact/
- dbt Semantic Layer / MetricFlow — https://docs.getdbt.com/docs/build/semantic-models · https://docs.getdbt.com/docs/build/metrics-overview
- 시맨틱 레이어 vs 온톨로지 — https://www.alation.com/blog/semantic-layer-vs-ontology-vs-enterprise-context-layer/
- 시맨틱 레이어 vs text-to-SQL(LLM 그라운딩) — https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026
- geo part-of 표준 — https://www.geonames.org/export/place-hierarchy.html · https://docs.ogc.org/is/22-047r1/22-047r1.html

**외부 — 구현 API**
- MCP 아키텍처·프리미티브(tools/resources) — https://modelcontextprotocol.io/docs/learn/architecture
- MCP 스펙 스키마 — https://modelcontextprotocol.io/specification/2025-06-18/schema
- Anthropic tool use — https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
- 프롬프트 캐싱(대형 안정 프리픽스에 적용) — https://platform.claude.com/docs/en/build-with-claude/prompt-caching

**모델·루프 관련 주의**
- 예제는 `claude-opus-4-8` + `thinking={"type":"adaptive"}` 를 쓴다. `budget_tokens` 는 이 세대에서
  제거되어 400 이 난다. 인증은 `ANTHROPIC_API_KEY` 또는 `ant auth login` 프로필 자동 해석.
- 도구 결과에 오류가 있으면 `tool_result` 에 `is_error: true` 를 실어 모델이 자가수정하게 한다
  (현재 예제는 결과만 전달 — P10 개선 후보).

---

## 6. 작업 순서 제안
1. **P1·P2**(통계적 정확성) → 틀린 숫자를 먼저 막는다. 이게 최우선인 이유: 나머지는 편의지만
   이 둘은 **답이 틀린다**.
2. **P4**(라벨 키잉) → 표기 오염 제거. P7 의 역해결이 이것 위에 서므로 선행.
3. **P3**(last_n) — 팀과 캐시 정책 합의 후.
4. **P7·P6**(검색·비용) → 에이전트 실사용 품질/비용.
5. **P5·P9**(거버넌스·신선도) → 공개·운영 준비.
6. **P8·P10** → 확장.

각 단계마다 §4 검증을 돌리고, 커밋은 논리 단위로 한국어 conventional 로 남긴다.
