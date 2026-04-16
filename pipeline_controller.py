"""
Pipeline Controller — 요청 처리 흐름 제어
"""
import re
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_client import LLMClient
    from memory_manager import MemoryManager
    from file_manager import FileManager

# 요청 유형별 max_tokens
_TOKENS_CODE  = 4096   # 구현·수정 요청
_TOKENS_MID   = 2048   # 일반 대화
_TOKENS_SHORT = 800    # 단순 질문·설명

_CODE_KEYWORDS = frozenset([
    "구현", "만들", "작성", "코드", "함수", "클래스", "수정", "추가", "변경",
    "버그", "고쳐", "픽스", "리팩", "최적화", "생성",
    "implement", "create", "write", "code", "function", "class",
    "fix", "refactor", "optimize", "add", "modify", "update",
])
_SHORT_KEYWORDS = frozenset([
    "설명", "왜", "뭐야", "뭔가요", "어떻게", "차이", "의미", "정의",
    "explain", "what", "why", "how", "difference", "meaning",
])


class PipelineController:
    def __init__(self, llm: "LLMClient", memory: "MemoryManager", files: "FileManager", config: dict):
        self.llm = llm
        self.memory = memory
        self.files = files
        self.config = config
        self.system_prompt = self._load_prompt("system.md")

    # ------------------------------------------------------------------
    # 요청 처리
    # ------------------------------------------------------------------

    def process_request(self, user_input: str) -> str:
        """
        처리 흐름:
        1. 메모리에 사용자 입력 추가
        2. 필요 시 컨텍스트 압축
        3. 요청 유형에 따라 max_tokens 동적 결정
        4. 스트리밍 응답
        5. diff 블록 자동 감지·적용
        6. 응답 메모리에 추가
        """
        self.memory.add_message("user", user_input)

        if self.memory.needs_compression():
            print("\n[💭 컨텍스트 압축 중 — 잠시 기다려주세요...]\n")
            self.memory.compress(self.llm, self.system_prompt)

        messages = self.memory.get_messages_for_api()
        max_tokens = self._estimate_max_tokens(user_input)

        response, _, _ = self.llm.stream(
            messages=messages,
            system=self.system_prompt,
            max_tokens=max_tokens,
            use_rag=True,
            print_prefix="\n🤖 ",
        )

        self._extract_and_apply_diffs(response)
        self.memory.add_message("assistant", response)
        return response

    # ------------------------------------------------------------------
    # SESSION_STATE.md 생성 (compact)
    # ------------------------------------------------------------------

    def generate_session_state(self, session_info: dict) -> str:
        """
        세션 종료 시 SESSION_STATE.md 생성.

        이전 방식과의 차이:
        - 입력: 전체 대화 대신 최근 10개 메시지만 사용 → 입력 토큰 ~80% 절감
        - 출력: 산문 없이 구조화된 compact 포맷 → 출력 800토큰 이하
        - 폴더 구조: 포함 안 함 (PROJECT.md에 있음)
        """
        recent = self.memory.all_messages()[-10:]
        recent_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:600]}" for m in recent
        )
        if self.memory.summary_memory:
            recent_text = f"[이전 대화 요약]\n{self.memory.summary_memory}\n\n[최근 대화]\n{recent_text}"

        timestamp = session_info.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        prompt = f"""다음 개발 세션을 분석하여 SESSION_STATE.md를 작성하세요.
요건: 산문 없이 불릿 형식만. 다음 세션에서 즉시 작업 재개에 필요한 정보만.

[최근 대화]
{recent_text}

아래 형식을 정확히 따르세요 (섹션 제목 변경 금지):

---
# SESSION_STATE
업데이트: {timestamp}
요청 수: {session_info.get('request_count', 0)} | 경과: {session_info.get('duration', 'N/A')}

## 마지막 작업
- (완료한 작업 3개 이하, 한 줄씩)

## 진행 중
- (세션 종료 시점에 하던 작업, 없으면 "없음")

## TODO
- [ ] (미완성 항목, 우선순위 높은 것부터 5개 이하)

## 주의사항
- (알려진 버그·제약·실수 방지 메모, 없으면 생략)
---
"""

        print("\n📝 세션 상태 저장 중...")
        # 내부 처리 → complete() 사용 (스트리밍 오버헤드 제거)
        # 메인 시스템프롬프트 재사용 → 캐시 히트 가능
        response, inp, out = self.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self.system_prompt,
            max_tokens=800,
        )
        print(f"  완료 ({inp + out:,} 토큰)")
        return response

    # ------------------------------------------------------------------
    # PROJECT.md 생성 (최초 1회 또는 명시적 갱신)
    # ------------------------------------------------------------------

    def generate_project_context(self) -> str:
        """
        PROJECT.md 생성 — 프로젝트의 고정 컨텍스트.

        언제 호출:
        - 최초 세션: PROJECT.md 없을 때 자동 생성
        - 사용자가 'project update' 명령어 실행 시

        내용: 목표·아키텍처·핵심 파일·규칙
        특징: 세션 재시작에도 변하지 않으므로 캐시 히트율 최대화
        """
        project_structure = self.files.analyze_project_structure()
        existing = self.files.load_project_context() or "(없음)"

        # 대화에서 프로젝트 목표를 파악하기 위해 전체 컨텍스트 사용
        context_parts = []
        if self.memory.session_memory:
            context_parts.append(f"[이전 세션 상태]\n{self.memory.session_memory}")
        msgs = self.memory.all_messages()[-20:]
        if msgs:
            context_parts.append("\n".join(
                f"{m['role'].upper()}: {m['content'][:500]}" for m in msgs
            ))
        conversation = "\n\n".join(context_parts) or "(대화 없음)"

        prompt = f"""프로젝트 컨텍스트 문서(PROJECT.md)를 작성하세요.
이 문서는 여러 세션에 걸쳐 재사용되므로 시간이 지나도 유효한 고정 정보만 포함합니다.

[현재 폴더 구조]
{project_structure}

[기존 PROJECT.md]
{existing}

[대화 컨텍스트]
{conversation}

아래 형식으로 작성 (섹션 제목 변경 금지, 산문 최소화):

---
# PROJECT
엔진/언어: {self.config.get('engine', 'unknown')}

## 목표
- (프로젝트가 달성하려는 것, 2~3줄)

## 아키텍처
- (핵심 모듈·컴포넌트와 역할, 각 1줄)

## 핵심 파일
- `경로`: 역할

## 규칙 / 제약
- (반드시 지켜야 할 설계 결정·코딩 규칙)
---
"""

        print("\n📋 PROJECT.md 생성 중...\n")
        # 메인 시스템프롬프트 재사용 → 캐시 히트
        response, _, _ = self.llm.stream(
            messages=[{"role": "user", "content": prompt}],
            system=self.system_prompt,
            max_tokens=1200,
            print_prefix="",
        )
        return response

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _estimate_max_tokens(self, user_input: str) -> int:
        lower = user_input.lower()
        words = set(re.split(r"[\s,]+", lower))
        if words & _CODE_KEYWORDS:
            return _TOKENS_CODE
        if words & _SHORT_KEYWORDS:
            return _TOKENS_SHORT
        return _TOKENS_MID

    def _extract_and_apply_diffs(self, response: str) -> None:
        diff_blocks = re.findall(r"```diff\n(.*?)```", response, re.DOTALL)
        if not diff_blocks:
            return

        print("\n🔧 diff 블록 감지 — 파일에 적용 중...")
        all_modified: list[str] = []
        for diff in diff_blocks:
            all_modified.extend(self.files.apply_diff(diff))

        if all_modified:
            print(f"  수정된 파일: {', '.join(all_modified)}")
        else:
            print("  ⚠️  적용 가능한 파일을 찾지 못했습니다 (경로 확인 필요)")

    def _load_prompt(self, filename: str, default: str = "") -> str:
        prompt_path = Path(__file__).parent / "prompts" / filename
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return default or _DEFAULT_SYSTEM_PROMPT


_DEFAULT_SYSTEM_PROMPT = """당신은 전문 소프트웨어 개발자입니다.
세션 내에서 이전 대화를 항상 참조하며, 실행 가능한 완전한 코드를 제공합니다.
파일 수정 시 전체 파일 대신 unified diff 형식을 사용합니다.
"""
