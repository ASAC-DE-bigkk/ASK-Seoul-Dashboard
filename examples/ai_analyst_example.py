"""AI 분석가 — 온톨로지 위에 Claude 만 '바로 붙이면' 되는 예제.

이 파일이 하는 일: 자연어 질문 → Claude 가 온톨로지 도구(list_sources/describe_source/
run_query)를 호출 → agent_tools 가 안전 SQL 로 실행 → Claude 가 숫자·SQL 을 인용해 결론.

AI 는 raw SQL 을 만지지 않는다. 모든 도구 호출은 querybuilder 화이트리스트 + 읽기전용
실행기를 통과하므로, AI 가 실수하거나 프롬프트 주입을 당해도 임의 조회가 성립하지 않는다.

필요한 것은 단 하나 — 인증뿐:
    pip install anthropic
    ant auth login              # 또는  export ANTHROPIC_API_KEY=sk-...
    # (선택) 질의 예산·허용 소스
    export CHARTS_AGENT_MAX_ROWS=200
    export CHARTS_AGENT_ALLOWED_DOMAINS=commerce,culture
실행:
    python -m examples.ai_analyst_example "강남구에서 개업이 가장 많은 업종은?"
"""
from __future__ import annotations

import json
import sys

import anthropic  # pip install anthropic

from app.charts import agent_tools

MODEL = "claude-opus-4-8"

# 안전 불변식은 프롬프트에도 못박는다 — querybuilder 검증과 이중으로.
SYSTEM = """너는 서울 열린데이터 온톨로지(Charts Studio) 위의 데이터 분석가다.

원칙:
1) 먼저 list_sources 로 어떤 소스가 있는지 보고, describe_source 로 필드의 role·가산성
   (additive)·허용 집계·값 라벨(코드→한글)·롤업 관계를 확인한 뒤에만 run_query 를 짠다.
2) 결론의 모든 숫자는 run_query 가 돌려준 행에서 나와야 한다. 근거 없는 수치를 만들지 마라.
   결론에는 사용한 렌더 SQL 을 함께 인용한다.
3) additive=false 인 필드(비율·평균·순위·LQ 등)에는 절대 sum 을 쓰지 마라 — 필드의
   allowed_aggs 안에서만 집계한다(서버도 거부한다).
4) 지역은 코드로 집계되고 한글 라벨로 표기된다(신사동·강남구처럼 동명이지역도 코드로 분리).
   결론 표기는 labeled_rows 의 한글 라벨을 쓴다.
5) truncated=true 면 "전수가 아니라 상위 N"임을 결론에 밝혀라(조용한 절단 금지).
한국어로, 근거(숫자+SQL)를 들어 간결하게 답한다."""


def run(question: str, max_turns: int | None = None) -> str:
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 또는 ant auth login 프로필 자동 해석
    tools = agent_tools.tool_schemas()  # ← 온톨로지가 곧 도구 계약
    max_turns = max_turns or agent_tools.CONFIG.max_turns
    messages: list[dict] = [{"role": "user", "content": question}]

    final = ""
    for _ in range(max_turns):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},  # 다단 추론(어떤 축으로 나눌지→질의→재질의)
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        final = "".join(b.text for b in resp.content if b.type == "text") or final
        if resp.stop_reason != "tool_use":
            break
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            out = agent_tools.call_tool(block.name, block.input)  # 화이트리스트 검증·읽기전용 실행
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(out, ensure_ascii=False, default=str),
            })
        messages.append({"role": "user", "content": results})
    return final


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "상권 도메인에서 개업과 폐업 총 건수를 비교해줘."
    print(run(q))
