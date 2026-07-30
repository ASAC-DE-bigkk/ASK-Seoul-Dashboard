# 02 — 부족한 부분: 없는 것 / 조치 가능 / 조치 불가, 그리고 보강한 것

01 의 대조표를 **행동 가능한 3버킷**으로 나누고, 차트 스튜디오의 원자(role·slot·field·measure·
filter·dialect)로 **재해석**한 뒤, 조치 가능한 것 중 값싸고 값진 것을 **실제로 보강**했다.

---

## A. 없지만 **조치 불가/불필요** (∅) — 만들지 말 것
차트 스튜디오 설계(스냅샷 파생·백엔드 중립·라이브 웨어하우스 질의·단일 relation)와 정면 충돌하거나,
투자 대비 가치가 명백히 낮다. **버리지 않고 이유까지 기록**(가치 0).

| 항목 | 왜 안 하는가 | 가치 |
|---|---|---|
| A-Box(타입된 개체 그래프) | 라이브 gold 를 질의하는 빌더와 상충 — 데이터를 트리플로 이중 적재 | 0 |
| DL 추론기/OWL 엔테일먼트/일관성 검사 | 유일한 유용 추론이 dict 2개의 전이폐포 — 엔진은 사중, 차트 선택에 지연·비결정성 유입 | 0 |
| RDF 트리플스토어 기판 | 폐쇄된 단일앱·집계 gold 카탈로그엔 과설계, 연합/상호운용 요구 없음 | 0 |
| 커뮤니티 합의 공유 어휘 | URI 는 기술적으로 발행 가능하나 '독립 시스템 간 합의'는 내부 서브시스템 범위 밖 | 0 |
| 소스 간 조인/일반 관계 그래프 | 소스는 의도적으로 비정규화된 단일 relation star. `querybuilder.build`는 단일테이블 전용 | 0 |

> 결론: "형식 온톨로지로 승격"은 이 제품에서 **가치 0**. 대신 **선언형 노출 + 실행 연결**에 투자한다.

---

## B. 없지만 **조치 가능**했고 → **실제로 보강함** (⊕, 완료)
전부 `app/charts/agent_tools.py` 위에 얇게 얹어 **기존 온톨로지/빌더/실행기를 한 줄도 고치지 않고**
구현했다(캐시 byte-동일·계약 불변 유지). 라이브 Trino 로 검증한 수치를 함께 남긴다.

| 보강 | 무엇을 채웠나 | 구현 | 가치 |
|---|---|---|---|
| **명시적 geo 관계(part-of)** | 동⊂구⊂시도⊂국가 관계를 처음으로 선언 | `GEO_PARENT` + `describe_source.rollup_to` + `manifest.geo_part_of` | 2 |
| **실행 가능한 롤업** | 관계를 '설명'에서 '실행'으로 — 이 소스에 실재하는 상위 컬럼을 노출 | `geo_parent_columns()` → `describe_source.rollup_columns` | **3** |
| **역할 택소노미(2단 상위어)** | 평면 role → temporal/categorical/spatial ⊂ dimension | `ROLE_CONCEPT`/`CONCEPT_PARENT` + `manifest.role_concepts` | 1 |
| **3치 가산성(Kimball)** | additive/**semi_additive**/non_additive 로 세분(시간 누적 왜곡 표식) | `additivity()` → `describe_source.additivity` | 2 |
| **형식적 직렬화(JSON-LD/SKOS)** | 자기기술 링크드데이터 — 가장 값싼 '정직' 단계 | `ontology_export('jsonld'\|'skos')`, `jsonld_context()` | 2 |
| **명명 지표 레지스트리** | KPI 를 이름으로(dbt metrics 얇은 대응) | `NAMED_METRICS` + `list_metrics()`/`run_metric()` | 2 |

**실증(라이브 Trino):**
- 실행 가능한 롤업 — `admin_dong_code`(geo_dong_code) 의 상위 컬럼은 이 소스의 `gu_code`.
  그 컬럼으로 그룹핑 → **강남구 481,622 · 서초구 279,861 · 송파구 273,927**(개·폐업 flow 합계 상위 3구).
  즉 querybuilder 를 건드리지 않고 "동→구 롤업"을 **기존 화이트리스트 컬럼 선택**으로 실현.
- 3치 가산성 — `cnt` = `additive`(cumulative_safe), `lq`/`survival_rate` = `non_additive`(sum 차단).
- SKOS 내보내기 — 20개 `skos:Concept`, `geo_dong` → `skos:broader` `geo_gu` 등.
- 명명 지표 — `run_metric('business_opened', dims=['gu_code'])` 라이브 실행.

> 왜 GEO_PARENT 만으론 부족했나: 리서치 검증 결과 **GEO_PARENT 는 querybuilder 에 소비자가 0**이라
> '설명용'에 그쳤다. `rollup_columns`(가치 3)가 그 관계를 **실행**으로 바꾼 핵심 조치다.

---

## B-2. 2차 보강 — 통계 정확성·표기 정본 (완료, 커밋 `41e94eb`)
1차가 "관계를 실행 가능하게" 였다면, 2차는 **"숫자와 표기를 틀리지 않게"** 다.

| 보강 | 무엇을 고쳤나 | 가치 |
|---|---|---|
| **가중 비율(ratio)** | 사전계산 비율을 `avg` 로 재집계하던 왜곡 제거 — `sum(num)/nullif(sum(den),0)`. SHARE §7.1 이 직접 금지하는 항목이었다 | **3** |
| **코호트 생존율 교정** | `avg(survival_rate)`(비가중) → `sum(survivors)/sum(cohort_n)` + 경과연차 축 필수화 | **3** |
| **재고 가산성 집행** | 재고성 측정값을 시간축과 합산하면 이중계산 → 거부. 추천(`preferred_agg`)도 함께 바꿔 자기정합 | **3** |
| **소스별 값 라벨** | 전역 병합이 남의 라벨을 덮어써 **11680 이 '중구'로 표기**되던 충돌 210건 해소. `gu_code` 는 MOIS 표준을 정본으로 승격 | **3** |
| **last_n 닫힌 구간** | 예보 테이블에서 '최근 N'이 미래를 끌어오던 문제 차단 | 2 |
| **비용 게이트·검색·역해결** | 실행 전 카디널리티 거부, 자연어 소스 검색, 한글→코드 역해결(동명이지역 후보 전부) | 2 |
| **출처·계약 버전** | `provenance`·`cached_at`·`contract_version`/`contract_hash` | 2 |

불변식 준수: **통과하는 스펙의 trino SQL 은 byte-동일**(거부만 추가), 전수 build 968/968 유지,
pytest 188 passed. 유일한 의도적 SQL 변경은 `last_n`(상한 추가)이며 캐시 TTL 600초라 영향은 1회성.

---

## C. 남은 **조치 가능**하나 **후순위** (⊕, 로드맵)
값이 있으나 지금 필수는 아니거나 코어 변경이 필요해, 문서로 남기고 보류.

| 항목 | 재해석(차트 스튜디오 언어) | 조치 방법 | 가치 |
|---|---|---|---|
| 측정값 단위/수량종류(quantity-kind) | 가산성의 진짜 근거(지금은 이름 regex 근사) | 필드에 `unit`/`quantity_kind` 선언, dbt 컬럼 메타에서 소싱 | 2 |
| 집계 안전규칙의 선언화(SHACL 형) | 슬롯·비가산·enum 제약을 shapes 로 | `agg_constraints`+`allowed_*`를 shapes 문서로 방출(추론기 없이) | 1 |
| 도메인 T-Box 클래스(District/Business…) | 컬럼 메타 위에 진짜 도메인 클래스 | 클래스+속성 선언(저가치·큰 작업) | 1 |
| 네임스페이스/URI 발행 | role/geo 를 안정 IRI 로 | NS 확정 후 SKOS 내보내기에 IRI 부여(부분 완료: `NS` 상수) | 1 |
| querybuilder 내장 롤업(substr GROUP BY) | 상위 컬럼이 없는 소스도 코드 prefix 로 롤업 | `querybuilder`에 substr 롤업 차원 추가(코어 변경·캐시 영향 주의) | 1 |

> 대부분 dong-level gold 는 이미 `gu`/`gu_code` 를 materialize 하므로 substr 롤업(C-마지막)의
> 실이익은 좁다 — B 의 `rollup_columns` 로 충분한 경우가 다수.

---

## 요약
- **없애야 할 환상(가치 0):** 형식 온톨로지 승격(A-Box·추론기·트리플스토어·조인그래프).
- **한 값진 조치(가치 3):** 관계를 **실행 가능**하게 — `rollup_columns`.
- **정직·상호운용(가치 2):** 3치 가산성·JSON-LD/SKOS·명명 지표 — 완료.
- **명명 정정:** 코드 주석/문서에서 "시맨틱 레이어 / role 레지스트리"를 정본 명칭으로, "온톨로지"는
  선언형 공리가 생길 때를 위해 남겨둔다(01 판정과 일치).
