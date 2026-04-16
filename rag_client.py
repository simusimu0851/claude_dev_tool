"""
RAG Client — MCP 서버를 통한 지식 검색 전담 모듈

MCP SSE 서버에 연결해서 tool을 호출하고 결과를 반환.
LLM의 tool_use 응답을 처리할 때 사용.
"""
import asyncio
from typing import Optional

from mcp import ClientSession
from mcp.client.sse import sse_client


# Anthropic API tool_use 정의 (LLM에게 전달)
RAG_TOOLS = [
    {
        "name": "ask_knowledge_base",
        "description": (
            "개인 옵시디언 노트(공부 내용, AI 요약본, 메모)에서 관련 청크를 검색합니다. "
            "개념 설명, 학습 내용 확인, 개인 노트 검색이 필요할 때 호출하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 쿼리"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask_resolve_api",
        "description": (
            "라이브러리 공식 문서(DaVinci Resolve, Unity, Openclaw 등)에서 "
            "메서드 시그니처, 파라미터, 반환값, 코드 예시를 검색합니다. "
            "API 사용법이나 공식 문서 참조가 필요할 때 호출하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 쿼리"}
            },
            "required": ["query"],
        },
    },
]


class RAGClient:
    """MCP SSE 서버에 tool 호출을 전달하고 결과를 반환."""

    def __init__(self, server_url: str = "http://127.0.0.1:8097"):
        self.server_url = server_url

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        MCP 서버의 tool을 동기적으로 호출.
        매 호출마다 SSE 연결 → initialize → call → 종료.
        로컬 서버라 오버헤드 무시 가능.
        """
        try:
            return asyncio.run(self._call(tool_name, arguments))
        except Exception as e:
            return f"[RAG 오류] {e}"

    async def _call(self, tool_name: str, arguments: dict) -> str:
        async with sse_client(self.server_url + "/sse") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                # MCP 결과에서 텍스트 추출
                if result.content:
                    parts = []
                    for block in result.content:
                        if hasattr(block, "text"):
                            parts.append(block.text)
                    return "\n".join(parts) if parts else "(결과 없음)"
                return "(결과 없음)"

    def is_available(self) -> bool:
        """서버 접속 가능 여부 확인 (시작 시 1회)."""
        try:
            import httpx
            with httpx.stream("GET", self.server_url + "/sse", timeout=3) as r:
                return r.status_code == 200
        except Exception:
            return False
