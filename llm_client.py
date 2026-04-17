"""
LLM Client — API 호출 전담 모듈
"""
import os
import anthropic
from typing import Optional

from rag_client import RAGClient, RAG_TOOLS


DEFAULT_MODELS = {
    "cheap": "claude-haiku-4-5-20251001",
    "advisor": "claude-opus-4-6",
    "standard": "claude-sonnet-4-6",
}


def _make_cached_system(text: str) -> list[dict]:
    """system 문자열을 cache_control 포함 리스트로 변환."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class LLMClient:
    """Anthropic API 호출 전담. 스트리밍 / 비스트리밍 모두 지원."""

    def __init__(self, config: dict):
        api_key = os.getenv("ANTHROPIC_API_KEY") or config.get("api_key")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY 환경변수를 설정하거나 config에 api_key를 입력하세요."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.config = config
        # RAG 클라이언트 (MCP 서버)
        rag_url = config.get("rag_server_url", "http://127.0.0.1:8097")
        self.rag = RAGClient(rag_url)
        self.rag_available = False  # start()에서 확인
        # 모델 유형별 토큰 누적 (cache_read는 약 10% 비용)
        self.cost_tracker: dict[str, int] = {
            "cheap": 0,
            "advisor": 0,
            "standard": 0,
            "cache_saved": 0,  # 캐시 히트로 절약된 토큰 수
        }

    # ------------------------------------------------------------------
    # 공개 인터페이스
    # ------------------------------------------------------------------

    def stream(
        self,
        messages: list,
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        use_advisor: bool = False,
        use_rag: bool = False,
        print_prefix: str = "\n🤖 ",
    ) -> tuple[str, int, int]:
        """
        스트리밍 응답.
        텍스트를 실시간 출력하면서 full_response 반환.
        system 파라미터에 prompt caching 적용.

        use_rag=True이면 RAG tool을 포함하고, LLM이 tool_use를 반환하면
        MCP 서버에 실행 → 결과 주입 → 재응답하는 루프를 실행.
        """
        model = model or self.config.get("cheap_model", DEFAULT_MODELS["cheap"])
        tools, betas = self._build_advisor_tools() if use_advisor else ([], [])

        # RAG tool 추가 (서버 사용 가능 시)
        if use_rag and self.rag_available:
            tools = tools + RAG_TOOLS

        full_response = ""
        total_input = 0
        total_output = 0
        cache_read = 0
        cache_creation = 0

        if print_prefix:
            print(print_prefix, end="", flush=True)

        # messages를 복사해서 tool_use 루프에서 안전하게 확장
        loop_messages = [m for m in messages]

        while True:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                messages=loop_messages,
            )
            if system:
                kwargs["system"] = _make_cached_system(system)
            if betas:
                kwargs["betas"] = betas
            if tools:
                kwargs["tools"] = tools

            with self.client.beta.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
                    full_response += text

                final = stream.get_final_message()
                if hasattr(final, "usage"):
                    total_input += final.usage.input_tokens
                    total_output += final.usage.output_tokens
                    cache_read += getattr(final.usage, "cache_read_input_tokens", 0) or 0
                    cache_creation += getattr(final.usage, "cache_creation_input_tokens", 0) or 0

            # tool_use 블록 확인 (advisor는 서버 측 자동 처리 → client 루프 제외)
            tool_uses = [
                b for b in final.content
                if b.type == "tool_use" and b.name != "advisor"
            ]
            if not tool_uses:
                break  # 텍스트만 반환 → 루프 종료

            # tool_use 실행: MCP 서버 호출
            # assistant 응답 전체를 대화에 추가 (API 규약)
            loop_messages.append({"role": "assistant", "content": _content_to_dicts(final.content)})

            tool_results = []
            for tu in tool_uses:
                print(f"\n  🔍 [{tu.name}] \"{tu.input.get('query', '')}\"", flush=True)
                result_text = self.rag.call_tool(tu.name, tu.input)
                # 결과 길이 표시
                chunk_lines = result_text.count("\n") + 1
                print(f"  📄 {chunk_lines}줄 반환", flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text,
                })

            loop_messages.append({"role": "user", "content": tool_results})
            # 루프 계속 → LLM이 검색 결과를 바탕으로 최종 응답 생성

        print()

        bucket = "advisor" if use_advisor else "cheap"
        self.cost_tracker[bucket] += total_input + total_output
        self.cost_tracker["cache_saved"] += int(cache_read * 0.9)

        self._log_cache_stats(total_input, cache_read, cache_creation)

        return full_response, total_input, total_output

    def complete(
        self,
        messages: list,
        model: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> tuple[str, int, int]:
        """
        비스트리밍 응답. 내부 처리(압축, 상태 생성)에만 사용.
        화면 출력 없음. system에 prompt caching 적용.
        """
        model = model or self.config.get("cheap_model", DEFAULT_MODELS["cheap"])

        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = _make_cached_system(system)

        response = self.client.messages.create(**kwargs)
        text = response.content[0].text if response.content else ""
        inp = response.usage.input_tokens
        out = response.usage.output_tokens
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0

        cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        self.cost_tracker["cheap"] += inp + out
        self.cost_tracker["cache_saved"] += int(cache_read * 0.9)

        self._log_cache_stats(inp, cache_read, cache_creation)

        return text, inp, out

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _log_cache_stats(self, total_input: int, cache_read: int, cache_creation: int) -> None:
        """캐시 히트/생성 시 한 줄 로그 출력. 캐시 없으면 무출력."""
        if not cache_read and not cache_creation:
            return
        parts = []
        if cache_read:
            pct = round(cache_read / total_input * 100) if total_input else 0
            parts.append(f"히트 {cache_read:,}t ({pct}%)")
        if cache_creation:
            parts.append(f"생성 {cache_creation:,}t")
        print(f"  [캐시] {' | '.join(parts)}")

    def check_rag_server(self) -> bool:
        """RAG 서버 접속 확인. 시작 시 1회 호출."""
        self.rag_available = self.rag.is_available()
        return self.rag_available

    def _build_advisor_tools(self) -> tuple[list, list]:
        advisor_model = self.config.get("advisor_model", DEFAULT_MODELS["advisor"])
        tools = [{
            "type": "advisor_20260301",
            "name": "advisor",
            "model": advisor_model,
            "caching": {"type": "ephemeral", "ttl": "5m"},
        }]
        betas = ["advisor-tool-2026-03-01"]
        return tools, betas

    def get_total_tokens(self) -> int:
        return sum(v for k, v in self.cost_tracker.items() if k != "cache_saved")

    def get_cost_summary(self) -> dict:
        return self.cost_tracker.copy()


def _content_to_dicts(content_blocks) -> list[dict]:
    """Anthropic SDK의 content block 객체를 JSON-serializable dict로 변환."""
    result = []
    for b in content_blocks:
        if b.type == "text":
            result.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return result
