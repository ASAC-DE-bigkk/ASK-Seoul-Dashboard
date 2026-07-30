# 01 — 이게 정말 "온톨로지"인가? (판정)

`app/charts/ontology.py` 는 스스로를 "온톨로지"라 부른다. 이 문서는 그 주장이 타당한지
**형식(학술) 기준**과 **산업(시맨틱 레이어) 기준** 양쪽에서 검증한다. 근거는 코드 실측과
학술 문헌(하단 인용)이며, 30여 개 리서치·검증 에이전트의 대조·반박을 통과했다.

## 판정
**어느 register 로 재느냐에 따라 달라진다.**

- **형식 기준(Studer "formal, explicit specification of a shared conceptualization"; Guarino "logical theory")로는 온톨로지가 아니다.**
  온톨로지 언어 직렬화(RDF/OWL/SKOS) 없음, URI/네임스페이스 없음, DL 추론기·엔테일먼트 없음,
  타입된 개체(A-Box) 없음. (`requirements.txt` 에 rdflib/owlready/pyshacl 0개 — 실측)
- **Gruber 의 가장 느슨한 정의("explicit specification of a conceptualization")로는 통과한다** — 통제 어휘도 통과하는 낮은 바.
- **산업 "시맨틱 레이어/시맨틱 모델"(LookML, dbt metrics, Cube, Palantir Foundry "Ontology") 기준으로는 온톨로지라 부르는 게 관례적이고 방어 가능하다.**

가장 정확한 이름: **가볍고 자동 추론되는 단일테이블 시맨틱/메트릭 레이어 + role 레지스트리** —
평면 role 통제 어휘 + 코드↔라벨 식별 매핑 + 측정값별 집계 의미 + 도표 슬롯 계약(이분 매칭) +
화이트리스트 읽기전용 SQL 빌더. 코드 docstring 이 이미 `온톨로지(시맨틱) 레지스트리`·`role 어휘`로
스스로 hedge 하고 있어, 이 정정은 사실 코드의 자기 인식과 일치한다.

## 온톨로지 정의 요소 대조표
각 정의 요소별로 이 프로젝트가 충족하는지, 격차가 **조치 가능(⊕)** 인지 **조치 불가/불필요(∅)** 인지.

| 요소(정의) | 형식 요구 | 이 프로젝트 | 격차 |
|---|---|---|---|
| **개념화의 명시(Gruber)** | 도메인 객체·관계를 기계 판독형으로 명시 | **충족** — role 어휘 + CHART_TYPES + manifest | 없음(단, 이것만으론 통제어휘와 구분 안 됨) |
| **형식성(Studer/Borst)** | 논리 언어(RDFS/OWL/DL/FOL) + 추론 | **없음** — 전부 명령형 파이썬(regex·if) | ∅ 조치 불가치(차트툴에 추론기는 과설계) |
| **공유·상호운용(URI/네임스페이스)** | 커뮤니티 합의 어휘, 안정 URI | **없음** — 로컬 문자열, 단일 앱 내부 | ⊕ URI 발행은 가능하나 '합의'는 범위 밖 |
| **개념/클래스(T-Box)** | 속성 있는 클래스, 개체가 인스턴스화 | **부분** — role + role_concept 는 '컬럼 메타'의 분류. 도메인 엔티티(자치구/업종/사업체) 클래스는 없음 | ⊕ 저가치 |
| **택소노미/포섭(is-a, subClassOf)** | 클래스 is-a 계층 | **부분** — CONCEPT_PARENT(temporal/categorical/spatial ⊂ dimension) 2단 상위어 추가됨(agent_tools) | ⊕ LLM 개념묶기엔 충분. subClassOf '의미'는 주장 금지(추론기 없음) |
| **관계/객체속성(part-of 등)** | domain/range 가진 명시 관계 | **부분** — GEO_PARENT(동⊂구⊂시도⊂국가) 명시 추가됨 | ⊕ '실행'까지 원하면 rollup 로 연결(→02) |
| **공리/제약(Guarino)** | 선언형 논리 공리(추론기 검증) | **부분** — 제약 '내용'은 풍부(비가산 sum 금지·allowed_aggs·슬롯 계약·읽기전용) but 명령형 | ⊕ 집계 안전규칙을 SHACL 형태로 선언화 가능(형식 원할 때) |
| **개체/인스턴스(A-Box)** | 타입된 개체의 지상 단언 | **없음** — 컬럼/필드 메타(스키마 수준)만 | ∅ 라이브 웨어하우스 질의 설계와 상충 |
| **추론/일관성 검사** | 추론기 엔테일먼트 | **없음** — 결정형 분류 + 사전 전이폐포뿐 | ∅ 유일한 유용 추론이 dict 전이폐포 — DL 엔진은 사중 |
| **온톨로지 언어 직렬화(RDF/OWL/SKOS/JSON-LD)** | 자기기술 링크드데이터 | **부분** — 이제 `ontology_export('skos'\|'jsonld')` 로 SKOS/JSON-LD 내보내기 추가됨(→02) | ⊕ 가장 값싼 정직 단계(완료) |
| **통제어휘/데이터사전과의 구분** | 단순 용어목록·단일앱 필드메타를 초과 | **부분** — 의미 강화된 통제어휘 + 데이터사전 + 슬롯계약 | ⊕ '시맨틱 레이어/role 레지스트리'로 명명하는 것이 최저위험·최고정직 |

**요지:** 격차 대부분은 **조치 가능(⊕)** 이지만 상당수는 **저가치**다. 진짜 값어치 있는 조치는
"형식 온톨로지로 승격"이 아니라 **(a) 이미 있는 관계·제약을 소비자(LLM/MCP)에게 선언형으로 노출**,
**(b) 관계를 실행 가능하게(롤업) 연결**하는 것 — 02 에서 실제로 수행했다.

## 왜 'AI/MCP 에는 잘 맞는가'
MCP·LLM tool-use 가 요구하는 것은 **기계 판독형 JSON-Schema 계약**뿐이다. 평면 role 어휘 +
슬롯 계약 + value_labels + 집계 의미가 정확히 그 계약을 공급한다. 즉 **누락된 OWL/RDF 형식은
MCP 가치와 무관**하다 — "온톨로지"라는 이름은 형식을 오버셀하지만, 그 실체(시맨틱 레이어)는
AI 접목에 이례적으로 적합하다. 이것이 이 프로젝트의 전략적 강점이다(→04 실증).

## 인용 (형식·산업 기준)
- Gruber / Studer-Borst / Guarino 정의와 'shared conceptualization' 논쟁 — https://keet.wordpress.com/2017/01/20/on-that-shared-conceptualization-and-other-definitions-of-an-ontology/
- Guarino, *What is an Ontology* — https://iaoa.org/isc2012/docs/Guarino2009_What_is_an_Ontology.pdf
- 온톨로지 구성요소(클래스·택소노미·관계·개체·공리) — https://en.wikipedia.org/wiki/Ontology_components
- 온톨로지 vs 데이터사전/개념스키마(인식론적 구분) — https://aisel.aisnet.org/jais/vol8/iss2/4/
- RDFS/OWL subClassOf — http://webdam.inria.fr/Jorge/html/wdmch8.html · https://www.w3.org/TR/owl-ref/
- SKOS 개념 스킴(broader/notation/prefLabel) — https://www.w3.org/TR/skos-reference/
- SHACL(shapes) vs OWL 추론 — https://www.w3.org/TR/shacl/ · JSON-LD — https://www.w3.org/TR/json-ld11/
- T-Box/A-Box 구분 — https://ontologist.substack.com/p/a-box-t-box-r-box-c-box
- Kimball additive/semi-additive/non-additive — https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/additive-semi-additive-non-additive-fact/
- dbt Semantic Layer / MetricFlow — https://docs.getdbt.com/docs/build/semantic-models · https://docs.getdbt.com/docs/build/metrics-overview
- 시맨틱 레이어 vs 온톨로지(벤더 프레이밍) — https://www.alation.com/blog/semantic-layer-vs-ontology-vs-enterprise-context-layer/ · https://atlan.com/know/ontology-vs-semantic-layer/
- 시맨틱 레이어 vs text-to-SQL(LLM 그라운딩) — https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026 · https://www.getwren.ai/post/reducing-hallucinations-in-text-to-sql-building-trust-and-accuracy-in-data-access
- MCP 아키텍처·프리미티브 — https://modelcontextprotocol.io/docs/learn/architecture · https://modelcontextprotocol.io/specification/2025-06-18/schema
- geo part-of 표준(GeoNames·GeoSPARQL) — https://www.geonames.org/export/place-hierarchy.html · https://docs.ogc.org/is/22-047r1/22-047r1.html
