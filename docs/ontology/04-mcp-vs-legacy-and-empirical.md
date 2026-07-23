# 04 — MCP × 온톨로지 vs 레거시, 그리고 실증 트랜스크립트

"온톨로지를 MCP 로 노출하면 기존(레거시) 방식과 무엇이 다른가?" — 이 리포에는 **레거시가 실제로
존재**한다: `app/chat/` 이 온톨로지 유도·화이트리스트 SQL 빌더·tool 스키마·Anthropic 연결을 전부
**직접 재구현**한 일회성 통합이다. 그 대비로 시너지를 실증한다.

## MCP × 온톨로지 vs 레거시(`app/chat`) 8차원

| 차원 | 레거시 (`app/chat`, 일회성) | MCP × 온톨로지 (`agent_tools`) |
|---|---|---|
| **통합 위상** | 모델×호스트마다 별도 통합(M×N). 새 모델/호스트 = 새 통합 | 하나의 JSON-RPC 계약을 어느 호스트나 재사용(M+N). `agent_tools` 는 anthropic/mcp SDK import 0 |
| **안전 경계** | 중복 — `queryspec.py` 가 **두 번째** 화이트리스트 SQL 빌더(OPS/AGGS 별도, 드리프트 위험). 하드닝을 두 곳에 | 단일 재사용 — `run_query` → `querybuilder.build`(레지스트리 화이트리스트·고정 AGGS/OPS·리터럴 이스케이프) + `backends.execute`(방언별 읽기전용) |
| **tool 계약 출처** | 손으로 쓴 `TOOL_INPUT_SCHEMA`(온톨로지와 분리 → 드리프트) | 온톨로지에서 투영(`tool_schemas()`) — role·allowed_aggs·allowed_filter_ops·슬롯·value_labels. 모델 스펙이 **구성상 온톨로지-유효** |
| **프로바이더 결합** | 벤더 락 — `llm.py` 가 Anthropic wire 를 urllib 로 하드코딩 | 모델·호스트 중립 — 같은 도구가 Claude Desktop/Code·IDE·임의 MCP 호스트에서 무변경 |
| **발견/신선도** | 정적 — 온톨로지 카드를 캐시된 시스템 프롬프트로 직렬화(매 세션 토큰비용, 스키마 변경 시 stale) | 동적 — `tools/list`+resources 가 라이브 `Registry` 기반. 스냅샷 mtime 핫리로드(`Registry._fresh`)가 자연스런 `list_changed` 소스 → 새 gold 자동 등장 |
| **온톨로지 유도** | 중복 — 번들 격리로 `app/chat/ontology.py` 가 스냅샷 재읽기·카드 재구축 | 단일 공유 `Registry` 를 하나의 계약으로 서빙 |
| **결과 조합성** | 종착 — 행이 인앱 렌더러(`app.js`)에 용접, 외부로 못 흘림 | `run_query` 가 columns/rows/value_labels/실행SQL 을 **백엔드 중립 JSON** 으로 반환 → 다른 MCP 도구(viz·노트북)로 파이프 |
| **요구 형식성** | n/a | MCP 는 기계판독 JSON-Schema 계약만 요구 — 평면 role 어휘 + 슬롯 + value_labels 가 정확히 그것. 즉 **누락된 OWL/RDF 는 MCP 가치와 무관** |

> 핵심: `app/chat` 은 "온톨로지 위 LLM"의 **가치를 증명**하되 동시에 **레거시 안티패턴(중복·락·드리프트)**
> 을 보여준다. `agent_tools` 는 같은 가치를 **재사용 가능한 단일 계약**으로 되돌린다.

## 분석가 루프 (ground → plan → execute → observe → conclude)
MCP 호스트의 LLM 이 도는 루프. 아래는 **내가(Claude) MCP 클라이언트 역할을 대신 수행**해 실제 도구를
호출하고 라이브 Trino 결과로 실증한 트랜스크립트다(수치는 재현 가능).

### 실증 1 — 개·폐업 총계 (ground→plan→execute→observe→conclude)
```
plan:  run_query("gold_license_flow_monthly", dims=["event_type"],
                 measures=[{"field":"cnt","agg":"sum","alias":"total"}], order_by=[desc total])
sql :  select "event_type" as "event_type", cast(sum("cnt") as double) as "total"
       from "iceberg_dev"."commerce"."gold_license_flow_monthly"
       group by 1 order by "total" desc limit N        (mode=live, 2883ms)
rows:  [{event_type:opened, event_type__label:"개업", total:2,783,157},
        {event_type:closed, event_type__label:"폐업", total:1,654,011}]
```
> observe/conclude: 서울 인허가 누적 **개업 2,783,157 · 폐업 1,654,011**. 코드로 집계(homonym-safe),
> 한글 라벨로 표기. AI 는 SQL 을 만지지 않았고, 결론 숫자는 전부 tool_result 행에서 나왔다.

### 실증 2 — 온톨로지가 실수를 **차단** (환각 방어의 코드화)
```
run_query("gold_license_gu_specialization", dims=["gu"], measures=[{"field":"lq","agg":"sum"}])
→ {"error":"spec_error","message":"'lq' 필드에는 sum 집계를 사용할 수 없습니다"}
```
> `lq`(입지계수)는 `additive=false`. AI 가 sum 을 시도해도 **온톨로지 계약이 SQL 생성 전에 거부**한다.
> 레거시라면 "합계"를 그럴듯하게 반환해 조용히 틀렸을 값을, 여기선 원천 차단.

### 실증 3 — 관계를 실행: 동→구 롤업
```
run_query("gold_license_flow_monthly", dims=["gu_code"], measures=[{"field":"cnt","agg":"sum"}], order desc)
→ 강남구(11680) 481,622 · 서초구(11650) 279,861 · 송파구(11710) 273,927   (labeled, live)
```
> `describe_source` 가 `admin_dong_code.rollup_columns=["gu_code"]` 를 알려주므로, AI 는 상위 축으로
> 즉시 롤업한다 — 관계가 '설명'이 아니라 '실행'.

## 붙이는 법(요약)
- **AI 직접(수동 루프):** `examples/ai_analyst_example.py` — `tool_schemas()`+`call_tool()`, `claude-opus-4-8`,
  `thinking={"type":"adaptive"}`. `ANTHROPIC_API_KEY` 또는 `ant auth login` 이면 끝.
- **MCP 서버:** `examples/mcp_ontology_server.py` — `list_tools/call_tool/list_resources/read_resource`.
  Claude Desktop `mcpServers` 에 등록하면 호스트 LLM 이 붙는다(LLM 코드는 우리 쪽에 없음).
- **안전 불변식은 프롬프트 + querybuilder 이중:** 숫자·SQL 인용 강제, 비가산 sum 금지, truncated 정직,
  코드 식별/한글 표기 — 05 의 이식성과 함께 어느 백엔드에서도 유지된다.
