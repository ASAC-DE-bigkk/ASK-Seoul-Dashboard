"""MCP 서버 — 온톨로지 도구를 Model Context Protocol 로 노출한다 (LLM 미포함).

이 서버는 app/charts/agent_tools 의 도구 표면을 그대로 MCP tools/resources 로 감싼다.
LLM 은 이 파일에 없다 — Claude Desktop·Claude Code·기타 MCP 호스트가 이 서버에 '붙어서'
list_sources/describe_source/run_query/list_metrics/run_metric 를 호출한다. 즉 온톨로지가
곧 tool 계약이고, 안전 경계(querybuilder 화이트리스트 + 읽기전용 실행)는 그대로 재사용된다.

설치·실행:
    pip install mcp
    python -m examples.mcp_ontology_server        # stdio 트랜스포트

Claude Desktop 등록(claude_desktop_config.json):
    {
      "mcpServers": {
        "ask-seoul-ontology": {
          "command": "python",
          "args": ["-m", "examples.mcp_ontology_server"],
          "cwd": "/path/to/dashboard",
          "env": {"CHARTS_AGENT_ALLOWED_DOMAINS": "commerce,culture", "CHARTS_AGENT_MAX_ROWS": "200"}
        }
      }
    }
그 뒤 호스트에서 자연어로 물으면, 호스트의 LLM 이 이 도구들을 호출해 답한다.
"""
from __future__ import annotations

import asyncio
import json

import mcp.types as types  # pip install mcp
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app.charts import agent_tools

server = Server("ask-seoul-ontology")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    # 온톨로지가 그대로 도구 계약이 된다 — tool_schemas() 는 Anthropic tools 이자 MCP inputSchema.
    return [
        types.Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"])
        for t in agent_tools.tool_schemas()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    # 모든 호출은 agent_tools 를 거쳐 querybuilder 화이트리스트 + 읽기전용 실행을 통과한다.
    result = agent_tools.call_tool(name, arguments or {})
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(uri="ontology://manifest", name="Ontology manifest",
                       description="역할 어휘·role 개념·geo part-of·도표 슬롯 계약·질의 예산 요약",
                       mimeType="application/json"),
        types.Resource(uri="ontology://export/jsonld", name="Ontology (JSON-LD)",
                       description="매니페스트 + JSON-LD @context (형식적 시맨틱 노출)",
                       mimeType="application/ld+json"),
        types.Resource(uri="ontology://export/skos", name="Role vocabulary (SKOS)",
                       description="역할 어휘 SKOS 개념 스킴(geo part-of = skos:broader)",
                       mimeType="application/ld+json"),
    ]


@server.read_resource()
async def read_resource(uri) -> str:
    key = str(uri)
    if key == "ontology://manifest":
        return json.dumps(agent_tools.ontology_manifest(), ensure_ascii=False, default=str)
    if key == "ontology://export/jsonld":
        return json.dumps(agent_tools.ontology_export("jsonld"), ensure_ascii=False, default=str)
    if key == "ontology://export/skos":
        return json.dumps(agent_tools.ontology_export("skos"), ensure_ascii=False, default=str)
    raise ValueError(f"알 수 없는 리소스: {uri}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
