# 06 — 이슈 #43 의 모순·오류 정정 (errata)

이슈 #43 `[Design] 온톨로지 개념 + AI 분석가` 의 개념은 타당하나, **Part 3 구현 스케치의 내부 API
참조가 실제 코드와 불일치**하고 **용어 오버클레임**이 있다. 아래는 코드 실측 기반 정정이다.
(사용자 지시에 따라 라이브 이슈/PR 은 건드리지 않고, 정정본을 이 브랜치 문서로 남긴다.)

## 코드 스케치 오류 (실측)
| # | 이슈 #43 표기 | 실제 코드 | 정정 |
|---|---|---|---|
| E1 | `ontology.sources_summary()` | **미존재.** 레지스트리는 `registry.sources()/get()/meta()` | `agent_tools.list_sources()` / `describe_source()` |
| E2 | `querybuilder.validate_spec(args)` | **미존재.** 검증은 `build()` 내부 + `validate_filter_tree/validate_having` | `agent_tools.run_query()` 가 캡슐화(스펙 오류 → `spec_error`) |
| E3 | `querybuilder.build(spec)` | 시그니처 **`build(source: dict, spec: dict)`** — 인자 2개 | `querybuilder.build(source, spec)` |
| E4 | `backends.execute(sql, spec.backend)` | 시그니처 **`execute(source: dict, sql: str, max_rows=…, force=…)`** — 첫 인자는 **source dict** | `backends.execute(source, sql, …)` |
| E5 | `spec.backend` | `QueryRequest` 에 **`backend` 필드 없음** — 백엔드는 **소스**(`SourceDetail.backend`)에 있음 | 소스에서 방언 해석(`resolve_dialect(source["backend"])`) |
| E6 | `app/inputguard.py (159줄)` | 실제 **164줄** | 경미(문서 드리프트) |

> 나머지 파일 라인수(ontology 776·querybuilder 602·models 266·router 421·backends 278·extract 1061·trino 262)는 정확.

## 용어 정정 (→ 01 판정)
- 이슈 핵심 주장 *"온톨로지는 이미 기계가 읽을 수 있는 의미 계층(semantic layer)이다"* 는 **부분만 옳다.**
  형식(OWL/DL) 기준으로는 온톨로지가 아니다. **"자동 추론 시맨틱/메트릭 레이어 + role 레지스트리"**
  가 정확한 명칭이며, 그럼에도 **MCP/LLM tool schema 자산으로서의 가치 주장은 유효**하다(형식 부재는
  MCP 가치와 무관 — 01·04 참조).

## 스케치가 옳았던 부분 (정정 아님)
- `model="claude-opus-4-8"`, `thinking={"type":"adaptive"}` — 현행 Anthropic API 로 **정확**.
- `anthropic.Anthropic()` + 주석 `ANTHROPIC_API_KEY / ant auth login 프로필 자동 해석` — **정확**
  (bare 클라이언트가 `ant auth login` 프로필을 자동 해석).
- 안전 불변식(숫자·SQL 강제 인용, `additive=false` sum 금지, truncated 정직, 코드 식별/한글 표기) — **유효**.

## 정정된 구현 스케치 (실제로 동작)
이슈 #43 의 `run_tool` 을 **실존 함수**로 대체한 정본은 `examples/ai_analyst_example.py`. 요지:
```python
from app.charts import agent_tools

TOOLS = agent_tools.tool_schemas()          # 온톨로지가 곧 도구 계약 (E1/E2 해결)

def run_tool(name, args):
    return agent_tools.call_tool(name, args) # list_sources/describe_source/run_query 디스패치
    # 내부적으로: querybuilder.build(source, spec) (E3) → backends.execute(source, sql) (E4),
    #            방언은 source["backend"] 로 해석 (E5)
```
루프·시스템 프롬프트·예산은 이슈 #43 과 동일 사상이되, 함수 참조만 실존으로 바로잡았다.

## 선행(#39) 대비
이슈 #43 은 개념·타당성 문서화까지가 범위였다. 본 브랜치는 그 **(a) 동작 PoC** 를 실제로 구현했다:
`app/charts/agent_tools.py`(도구 표면·설정부·보강) + `examples/`(AI/MCP 접속) + `docs/ontology/`(검증·활용).
