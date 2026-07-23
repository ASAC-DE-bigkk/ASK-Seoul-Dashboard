# 온톨로지 독스 — Charts Studio 의미 계층 분석·활용·검증

Charts Studio(`app/charts/`)가 스스로를 부르는 **"온톨로지"** 가 정말 온톨로지인지 학술·산업
기준으로 검증하고, 그 위에 **MCP/AI·RAG·다중 백엔드(D1/RDB)** 로 얹는 모든 활용 패턴을 정리하며,
부족한 부분 중 **조치 가능한 것**을 실제로 보강한 기록이다. 모든 수치는 라이브 Trino(R2 gold)·
SQLite 로 **실증**했다.

## 읽는 순서
1. [01-ontology-verdict.md](01-ontology-verdict.md) — 이게 정말 온톨로지인가? (기준 대조표·판정)
2. [02-gaps-and-remediation.md](02-gaps-and-remediation.md) — 부족한 부분 = 없는 것/조치 가능/조치 불가, 그리고 **실제로 보강한 것**
3. [03-usage-patterns.md](03-usage-patterns.md) — 온톨로지 사용법 **총람** (MCP·RAG·분석·이식성…) — 각 패턴에 **가치 등급 0~3**
4. [04-mcp-vs-legacy-and-empirical.md](04-mcp-vs-legacy-and-empirical.md) — MCP×온톨로지 vs 레거시(같은 리포 `app/chat`)와 **실증 트랜스크립트**
5. [05-backend-portability.md](05-backend-portability.md) — Trino(R2)에서 검증한 온톨로지가 **D1·RDB** 에서도 동일 동작하는지 (실증)
6. [06-issue-43-corrections.md](06-issue-43-corrections.md) — 이슈 #43 의 모순·오류 정정

관련 코드: `app/charts/agent_tools.py`(도구 표면), `examples/ai_analyst_example.py`(AI 붙이기),
`examples/mcp_ontology_server.py`(MCP 서버). 상위 규약: `SHARE.md`, `docs/charts-design-intents.md`.

## 가치 등급 (0~3) 범례
문서 전체에서 각 항목·패턴에 아래 등급을 매긴다. **정말 명백히 가치 없는 것도 버리지 않고 0으로 기록**한다.

| 등급 | 의미 | 취급 |
|---|---|---|
| **3** | 전략적·필수 — 이 온톨로지가 아니면 못 하는, 큰 가치 | 핵심으로 상술 |
| **2** | 분명한 실무 가치 | 방법까지 기록 |
| **1** | 상황에 따라 유용 | 요점만 기록 |
| **0** | 거의 쓸모없음(오해·안티패턴 포함) | 버리지 않고 "왜 낮은가"까지 정리 |

## 한 줄 판정 (상세는 01)
> 형식(OWL/DL) 기준으로는 **온톨로지가 아니다.** 산업 "시맨틱 레이어" 기준으로는 **온톨로지라 불러도
> 무방하다.** 가장 정확한 이름은 **"자동 추론된 단일테이블 시맨틱/메트릭 레이어 + role 레지스트리"**.
> 그럼에도 **AI/MCP 접목에는 이례적으로 잘 맞는다** — MCP 가 요구하는 건 기계 판독형 JSON 계약뿐이고,
> 이 레지스트리가 정확히 그것이기 때문이다(형식 온톨로지의 부재는 MCP 가치와 무관).
