"""
Memory Manager — 5계층 메모리 관리
Working → Sliding → Summary → Session → Project
"""
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_client import LLMClient

# 메모리 경계값
WORKING_SIZE = 10    # 절대 압축 안 하는 최근 메시지 수
SLIDING_LIMIT = 50   # 이 수를 넘으면 압축 트리거

# 슬라이딩 메모리 저장 시 코드 블록 최대 길이
CODE_BLOCK_MAX_LEN = 600

# API 호출 전 메시지 크기 임계값 (문자 수 기준, ~4자=1토큰 추정)
# 이 값을 초과하면 SLIDING_LIMIT 미만이어도 조기 압축
MESSAGE_CHAR_LIMIT = 120_000  # ~30k 토큰

# Summary chunk 최대 개수 — 초과 시 가장 오래된 2개를 병합
MAX_SUMMARY_CHUNKS = 8


@dataclass
class MemoryManager:
    """
    5계층 메모리:
    1. Working      — 최근 N 메시지, 절대 손상 없음
    2. Sliding      — 슬라이딩 윈도우, 오래된 데이터 제거
    3. Summary      — 압축된 과거 대화 (기능/결정/버그 중심)
    4. Session      — 이전 세션 상태 (최근 작업·TODO·주의사항, 세션마다 갱신)
    5. Project      — 프로젝트 고정 컨텍스트 (목표·아키텍처·규칙, 거의 불변)

    캐시 전략:
    - Project: 세션 재시작에도 내용 불변 → 캐시가 세션 간 유지될 가능성 높음
    - Session: 세션 내에서 불변 → 2번째 호출부터 캐시 히트
    - system prompt: 항상 동일 → 최강 캐시 후보
    """
    sliding_memory: list = field(default_factory=list)
    summary_chunks: list = field(default_factory=list)  # chunk 기반 요약 (append-only)
    session_memory: str = ""   # SESSION_STATE.md (최근 작업·주의사항)
    project_memory: str = ""   # PROJECT.md (목표·아키텍처·규칙)

    @property
    def summary_memory(self) -> str:
        """하위호환: 기존 코드가 summary_memory를 문자열로 참조하는 경우 대응."""
        if not self.summary_chunks:
            return ""
        return "\n\n---\n\n".join(self.summary_chunks)

    # ------------------------------------------------------------------
    # 메시지 추가
    # ------------------------------------------------------------------

    def add_message(self, role: str, content: str) -> None:
        """
        새 메시지를 Sliding에 추가.
        - assistant 응답의 코드 블록이 길면 축약
        - 파일 컨텍스트 주입 시, 이전에 주입된 파일 내용을 축약 (최신 것만 전문 유지)
        """
        if role == "assistant" and len(content) > 1500:
            content = self._truncate_code_blocks(content)

        # 새 파일 컨텍스트가 들어오면 이전 파일 컨텍스트를 축약
        if role == "user" and content.startswith("[파일 컨텍스트:"):
            self._shrink_old_file_contexts()

        self.sliding_memory.append({"role": role, "content": content})

    @property
    def working_memory(self) -> list:
        """Working = Sliding의 최근 WORKING_SIZE개 (읽기 전용 뷰)."""
        return self.sliding_memory[-WORKING_SIZE:]

    # ------------------------------------------------------------------
    # API 메시지 구성
    # ------------------------------------------------------------------

    def get_messages_for_api(self) -> list:
        """
        LLM API에 넘길 messages 구성.

        주입 순서 (캐시 효율 최적):
          1. [PROJECT.md]      — cache_control: 내용 불변, 세션 간 캐시 재사용 가능
          2. [SESSION_STATE.md] — cache_control: 세션 내 불변
          3. [Summary]         — cache_control: 압축 후 ~40메시지 동안 불변
          4. sliding_memory    — 현재 대화 (캐시 불가)

        Project와 Session을 분리된 cache 블록으로 두는 이유:
        session 명령어 후 SESSION_STATE.md는 바뀌지만 PROJECT.md는 그대로이므로
        Project 블록의 캐시는 세션 재시작 후에도 유효할 수 있음.
        """
        messages = []

        # ── 1. Project 컨텍스트 (가장 안정적, 먼저 주입) ──
        if self.project_memory.strip():
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"[프로젝트 컨텍스트 — 직접 응답 불필요]\n\n{self.project_memory}",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            })
            messages.append({
                "role": "assistant",
                "content": "프로젝트 컨텍스트를 확인했습니다.",
            })

        # ── 2. Session 상태 (세션마다 갱신, 세션 내 불변) ──
        if self.session_memory.strip():
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"[이전 세션 상태 — 직접 응답 불필요]\n\n{self.session_memory}",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            })
            messages.append({
                "role": "assistant",
                "content": "이전 세션 상태를 확인했습니다. 이어서 작업하겠습니다.",
            })

        # ── 3. Summary chunks (각 chunk = 개별 메시지, 마지막에만 cache_control) ──
        # 이전 chunk는 내용 불변 → API prefix caching으로 자동 캐시 히트
        # 마지막 chunk에만 cache_control 설정 → 캐시 breakpoint 1개만 사용
        for i, chunk in enumerate(self.summary_chunks):
            is_last = (i == len(self.summary_chunks) - 1)
            text_block = {
                "type": "text",
                "text": f"[대화 요약 #{i + 1}]\n\n{chunk}",
            }
            if is_last:
                text_block["cache_control"] = {"type": "ephemeral"}
            messages.append({
                "role": "user",
                "content": [text_block],
            })
            messages.append({
                "role": "assistant",
                "content": "요약을 확인했습니다.",
            })

        # ── 4. 현재 대화 ──
        messages.extend(self.sliding_memory)
        return messages

    # ------------------------------------------------------------------
    # 압축
    # ------------------------------------------------------------------

    def needs_compression(self) -> bool:
        """메시지 수 OR 총 크기 기준으로 압축 필요 여부 판단."""
        if len(self.sliding_memory) > SLIDING_LIMIT:
            return True
        # 메시지 수가 적어도 코드가 많으면 총 크기가 클 수 있음
        total_chars = sum(len(m.get("content", "")) for m in self.sliding_memory)
        return total_chars > MESSAGE_CHAR_LIMIT

    def compress(self, llm: "LLMClient", system_prompt: str) -> None:
        """
        Sliding의 오래된 부분을 새 Summary chunk로 압축 (append-only).

        기존 chunk는 절대 수정하지 않으므로 API 호출 시
        이전 chunk 메시지가 prefix로 유지 → 캐시 히트.
        Working(최근 WORKING_SIZE 메시지)는 절대 건드리지 않음.
        """
        if not self.needs_compression():
            return

        to_compress = self.sliding_memory[:-WORKING_SIZE]
        keep = self.sliding_memory[-WORKING_SIZE:]

        conv_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:400]}"
            for m in to_compress
        )

        prompt = (
            "다음 대화를 구조화된 요약으로 압축하세요.\n"
            "반드시 보존: 구현된 기능, 코드 구조, 주요 결정, 버그와 해결법, TODO 항목.\n\n"
            f"[압축할 대화]\n{conv_text}\n\n"
            "압축된 요약 (마크다운):"
        )

        # haiku 명시: 압축은 저비용 모델로 충분, 시스템프롬프트 캐시 히트 가능
        summary, _, _ = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=system_prompt,
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
        )

        self.summary_chunks.append(summary)

        # chunk 수 제한: 초과 시 가장 오래된 2개를 단순 병합 (LLM 호출 없음)
        if len(self.summary_chunks) > MAX_SUMMARY_CHUNKS:
            merged = self.summary_chunks[0] + "\n\n" + self.summary_chunks[1]
            self.summary_chunks = [merged] + self.summary_chunks[2:]

        self.sliding_memory = keep

    # ------------------------------------------------------------------
    # 상태 로드
    # ------------------------------------------------------------------

    def load_project(self, content: str) -> None:
        """PROJECT.md 내용을 project_memory에 로드."""
        self.project_memory = content

    def load_session(self, content: str) -> None:
        """SESSION_STATE.md 내용을 session_memory에 로드."""
        self.session_memory = content

    # 하위 호환: 이전 코드가 load_persistent를 쓰는 경우를 위해 유지
    def load_persistent(self, content: str) -> None:
        self.session_memory = content

    # ------------------------------------------------------------------
    # 상태 조회
    # ------------------------------------------------------------------

    def total_messages(self) -> int:
        return len(self.sliding_memory)

    def all_messages(self) -> list:
        return list(self.sliding_memory)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _truncate_code_blocks(self, content: str) -> str:
        """
        코드 블록이 CODE_BLOCK_MAX_LEN을 초과하면 앞부분만 유지.
        메모리 크기를 줄이되 함수명·구조 등 핵심 맥락은 보존.
        """
        def _shorten(m: re.Match) -> str:
            lang = m.group(1) or ""
            code = m.group(2)
            if len(code) <= CODE_BLOCK_MAX_LEN:
                return m.group(0)
            preview = code[:CODE_BLOCK_MAX_LEN]
            return (
                f"```{lang}\n{preview}\n"
                f"... [코드 축약 — 원본 {len(code):,}자] ...\n```"
            )

        return re.sub(r"```(\w*)\n(.*?)```", _shorten, content, flags=re.DOTALL)

    # 파일 컨텍스트 최대 잔류 길이 (축약 시)
    _FILE_CTX_MAX = 400

    def _shrink_old_file_contexts(self) -> None:
        """
        이전에 주입된 [파일 컨텍스트:...] 메시지를 헤더+축약으로 교체.
        최신 파일만 전문 유지, 이전 것은 파일명+줄 수만 남김.
        """
        for msg in self.sliding_memory:
            if msg["role"] == "user" and msg["content"].startswith("[파일 컨텍스트:"):
                content = msg["content"]
                if len(content) <= self._FILE_CTX_MAX:
                    continue
                # 헤더 줄 (예: "[파일 컨텍스트: PlayerController.cs]") 보존
                first_line = content.split("\n", 1)[0]
                line_count = content.count("\n")
                msg["content"] = (
                    f"{first_line}\n"
                    f"[{line_count}줄, {len(content):,}자 — 이전 컨텍스트 축약됨]"
                )
