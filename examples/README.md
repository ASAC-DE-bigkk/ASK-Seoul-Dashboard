# examples — 온톨로지에 AI/MCP '바로 붙이기'

`app/charts/agent_tools.py`(LLM 미포함 도구 표면) 위에 AI 를 붙이는 두 가지 방법. 어느 쪽도
온톨로지·SQL 빌더·실행기를 고치지 않는다 — 안전 경계(화이트리스트 + 읽기전용)를 그대로 재사용한다.

## 1) AI 분석가 (수동 tool-use 루프) — `ai_analyst_example.py`
자연어 질문 → Claude 가 도구 호출 → 안전 SQL 실행 → 숫자·SQL 인용 결론.
```bash
pip install anthropic
ant auth login            # 또는  export ANTHROPIC_API_KEY=sk-...
# (선택) 질의 예산·허용 범위
export CHARTS_AGENT_ALLOWED_DOMAINS=commerce,culture
export CHARTS_AGENT_MAX_ROWS=200
python -m examples.ai_analyst_example "상권에서 개업/폐업 총 건수를 비교해줘"
```

## 2) MCP 서버 (호스트가 붙음) — `mcp_ontology_server.py`
온톨로지 도구를 Model Context Protocol 로 노출. Claude Desktop/Code 등 **MCP 호스트가 붙는다**
(LLM 코드는 이 파일에 없음).
```bash
pip install mcp
python -m examples.mcp_ontology_server        # stdio
```
Claude Desktop `claude_desktop_config.json`:
```json
{ "mcpServers": { "ask-seoul-ontology": {
    "command": "python", "args": ["-m", "examples.mcp_ontology_server"],
    "cwd": "/path/to/dashboard",
    "env": { "CHARTS_AGENT_ALLOWED_DOMAINS": "commerce,culture", "CHARTS_AGENT_MAX_ROWS": "200" } } } }
```
노출 도구: `list_sources · describe_source · run_query · list_metrics · run_metric`
노출 리소스: `ontology://manifest · ontology://export/jsonld · ontology://export/skos`

## 설정부(env, `agent_tools.AgentToolsConfig`)
| env | 기본 | 의미 |
|---|---|---|
| `CHARTS_AGENT_ALLOWED_SOURCES` | `*`(전체) | AI 가 볼 소스 화이트리스트(콤마) |
| `CHARTS_AGENT_ALLOWED_DOMAINS` | `*`(전체) | 도메인 화이트리스트 |
| `CHARTS_AGENT_MAX_ROWS` | `200` | run_query 행 상한(하드 상한 `trino.MAX_ROWS` 로 클램프) |
| `CHARTS_AGENT_MAX_TURNS` | `8` | 오케스트레이션 루프 최대 반복 |

## 실행 환경 주의 (SHARE.md §0.1)
Windows PowerShell 이 리포 기본 셸이다. bash 예시(`export …`)는 PowerShell 에선
`$env:CHARTS_AGENT_MAX_ROWS="200"` 로, `python -m …` 은 `.venv\Scripts\python -m …` 로 바꾼다.
자세한 온톨로지 검증·활용·이식성은 [../docs/ontology/](../docs/ontology/README.md) 참조.
