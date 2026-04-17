"""
Pipeline Controller — 요청 처리 흐름 제어

코드 요청: 계획 → 실행 → [리뷰 ↔ 수정 루프]
  - 각 단계마다 advisor_tool(Opus) 활성화 → Haiku가 필요시에만 Opus 위임
  - 각 단계 전 사용자 확인 + 단계별 브리핑
  - 리뷰는 "설계 브리핑 형식"으로 개선안을 구조화 → 수정이 명확해져 Haiku 중심 처리 가능
  - 리뷰-수정 루프(최대 N회): REVIEW_PASS / 사용자 중단 / 최대 루프 도달 시 종료
  - 파이프라인 내부 messages는 누적되며 prefix caching으로 재사용
  - 중간 단계는 memory에 저장하지 않음 → 다음 요청 컨텍스트 깔끔 유지
단순 질문: 직접 응답 (파이프라인 스킵)

README와의 일관성 — 유지되는 토큰 절약 전략:
1. Prompt caching: system + PROJECT.md + SESSION_STATE.md + Summary chunks
2. Prefix caching: 파이프라인 내 단계별 동일 prefix 자동 재사용
3. 동적 max_tokens: 요청/단계 특성에 맞춰 출력 길이 조정
4. advisor 최소 호출: Haiku가 판단하여 필요할 때만 Opus 호출 (늘 쓰지 않음)
5. 5계층 메모리 + 자동 압축 유지
6. diff 자동 감지·적용 유지
7. RAG 자동 통합 유지 (외부 정보가 필요한 단계에서만 활성화)
"""
import re
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from llm_client import LLMClient
    from memory_manager import MemoryManager
    from file_manager import FileManager

# 단계별 max_tokens — 용도에 맞춰 출력 상한 설정 (토큰 낭비 방지)
_TOKENS_PLAN    = 1500   # 계획: 불릿 목록 중심
_TOKENS_EXEC    = 4096   # 실행: 코드/diff 포함
_TOKENS_REVIEW  = 1500   # 리뷰: 설계 브리핑 포맷
_TOKENS_REVISE  = 4096   # 수정: 재구현 가능
_TOKENS_CODE    = 4096
_TOKENS_MID     = 2048
_TOKENS_SHORT   = 800

# 리뷰-수정 루프 상한 (비용 상한)
_MAX_REVIEW_LOOPS = 3

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
    # 요청 처리 (공개 진입점)
    # ------------------------------------------------------------------

    def process_request(self, user_input: str) -> str:
        """
        흐름:
        1. 메모리에 사용자 입력 추가
        2. 필요 시 컨텍스트 압축 (README: 자동 압축 조건 유지)
        3. 코드 요청 → 4단계 파이프라인 / 단순 질문 → 직접 응답
        """
        self.memory.add_message("user", user_input)

        if self.memory.needs_compression():
            print("\n[💭 컨텍스트 압축 중 — 잠시 기다려주세요...]\n")
            self.memory.compress(self.llm, self.system_prompt)

        if self._is_simple_question(user_input):
            return self._run_direct(user_input)

        return self._run_pipeline(user_input)

    # ------------------------------------------------------------------
    # 단순 질문: 직접 응답 (파이프라인 오버헤드 없음)
    # ------------------------------------------------------------------

    def _run_direct(self, user_input: str) -> str:
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
    # 개발 파이프라인 (4단계)
    # ------------------------------------------------------------------

    def _run_pipeline(self, user_input: str) -> str:
        self._print_pipeline_intro()

        # base_ctx: memory의 모든 블록 중 마지막 user 메시지(=방금 추가한 user_input) 제외
        # PROJECT.md / SESSION_STATE.md / Summary 블록의 cache_control이 여기에 그대로 포함됨
        base_ctx = self.memory.get_messages_for_api()[:-1]

        # 파이프라인 내부에서만 사용하는 messages (memory에는 저장하지 않음)
        # prefix caching으로 단계가 진행될수록 앞부분은 캐시 히트
        pipeline_messages = list(base_ctx)

        # ── Stage 1: 계획 ──────────────────────────────────────────────
        plan, pipeline_messages = self._stage_plan(user_input, pipeline_messages)
        if plan is None:
            return self._pipeline_cancelled()

        # ── Stage 2: 실행 ──────────────────────────────────────────────
        implementation, pipeline_messages = self._stage_execute(
            user_input, plan, pipeline_messages
        )
        if implementation is None:
            return self._pipeline_cancelled()
        self._extract_and_apply_diffs(implementation)

        # ── Stage 3-4: 리뷰-수정 루프 ─────────────────────────────────
        # 매 루프: 리뷰(설계 브리핑) → FAIL이면 수정 → 다음 루프에서 수정분 재리뷰
        # 종료 조건: REVIEW_PASS / 사용자 중단(n·s) / _MAX_REVIEW_LOOPS 도달
        current_impl = implementation
        for loop_idx in range(1, _MAX_REVIEW_LOOPS + 1):
            review, has_issues, pipeline_messages = self._stage_review(
                user_input, plan, current_impl, pipeline_messages,
                loop_idx=loop_idx, max_loops=_MAX_REVIEW_LOOPS,
            )
            if review is None:
                # 사용자가 리뷰 취소·스킵 → 현재 구현 확정
                break
            if not has_issues:
                # REVIEW_PASS → 루프 종료
                break

            revised, pipeline_messages = self._stage_revise(
                user_input, current_impl, review, pipeline_messages,
                loop_idx=loop_idx, max_loops=_MAX_REVIEW_LOOPS,
            )
            if revised is None:
                # 사용자가 수정 취소·스킵 → 현재 구현 확정
                break

            self._extract_and_apply_diffs(revised)
            current_impl = revised
            # 루프 계속 → 다음 반복에서 revised 대상으로 재리뷰
        else:
            # for-else: 루프가 break 없이 모두 소진된 경우 (MAX 도달)
            print(f"\n│  ↳ 리뷰-수정 루프 {_MAX_REVIEW_LOOPS}회 도달 — 현재 구현 확정")

        final = current_impl
        self._print_pipeline_summary()
        # memory에는 최종 결과만 저장 (중간 prompt/응답은 제외 → 토큰 절약)
        self.memory.add_message("assistant", final)
        return final

    def _pipeline_cancelled(self) -> str:
        msg = "(파이프라인 취소됨 — 기존 상태 유지)"
        print(f"\n⚠️  {msg}")
        self.memory.add_message("assistant", msg)
        return msg

    # ------------------------------------------------------------------
    # Stage 1: 계획 수립
    # ------------------------------------------------------------------

    def _stage_plan(
        self, user_input: str, pipeline_messages: list
    ) -> tuple[Optional[str], list]:
        self._print_stage_header(
            "1/4", "계획 수립",
            [
                "Advisor(Opus) 활성화 — Haiku가 필요시에만 Opus에 추론 위임",
                "수정/생성 파일, 구현 순서, 예상 리스크 분석",
                "코드 작성 없이 계획만 생성 → 출력 토큰 절약",
            ]
        )
        action = self._confirm("계획 수립을 실행")
        if action == "n":
            return None, pipeline_messages
        if action == "s":
            print("│  ↳ 스킵 — 계획 없이 바로 실행 단계로 진행")
            return "(계획 스킵됨)", pipeline_messages

        plan_prompt = (
            f"다음 개발 요청에 대한 구현 계획을 수립하세요.\n"
            f"복잡한 설계 판단이 필요하면 advisor 도구를 활용해 Opus에 위임하세요.\n"
            f"코드는 작성하지 않고 계획만 제시합니다.\n\n"
            f"[요청]\n{user_input}\n\n"
            f"포함 항목 (불릿 중심으로 간결하게):\n"
            f"1. 수정/생성할 파일 목록 (경로 포함)\n"
            f"2. 각 파일의 변경 내용 요약\n"
            f"3. 구현 순서\n"
            f"4. 예상 리스크·주의사항"
        )
        pipeline_messages.append({"role": "user", "content": plan_prompt})

        tokens_before = self.llm.get_total_tokens()
        plan, _, _ = self.llm.stream(
            messages=pipeline_messages,
            system=self.system_prompt,
            max_tokens=_TOKENS_PLAN,
            use_advisor=True,
            use_rag=False,  # 계획은 설계 판단 → RAG 도구 정의 토큰 절약
            print_prefix="\n📋 계획:\n",
        )
        used = self.llm.get_total_tokens() - tokens_before
        pipeline_messages.append({"role": "assistant", "content": plan})
        self._print_stage_footer("계획 수립", used, advisor=True)
        return plan, pipeline_messages

    # ------------------------------------------------------------------
    # Stage 2: 실행
    # ------------------------------------------------------------------

    def _stage_execute(
        self, user_input: str, plan: str, pipeline_messages: list
    ) -> tuple[Optional[str], list]:
        self._print_stage_header(
            "2/4", "실행",
            [
                "수립된 계획에 따라 코드를 작성 (diff 형식)",
                "Advisor(Opus) + RAG 활성화 — 필요시 문서 검색 + 깊은 추론",
                "변경은 README 규칙 그대로 ```diff 블록 자동 적용",
            ]
        )
        action = self._confirm("실행")
        if action == "n":
            return None, pipeline_messages
        if action == "s":
            print("│  ↳ 실행은 스킵 불가 — 취소하려면 n 선택")
            return None, pipeline_messages

        exec_prompt = (
            f"위 계획에 따라 코드를 구현하세요.\n"
            f"복잡한 알고리즘·설계 결정은 advisor 도구로 Opus에 위임하세요.\n"
            f"기존 파일 수정은 ```diff 형식, 새 파일 생성은 전체 코드 블록으로 출력합니다.\n\n"
            f"[원 요청]\n{user_input}"
        )
        pipeline_messages.append({"role": "user", "content": exec_prompt})

        tokens_before = self.llm.get_total_tokens()
        impl, _, _ = self.llm.stream(
            messages=pipeline_messages,
            system=self.system_prompt,
            max_tokens=_TOKENS_EXEC,
            use_advisor=True,
            use_rag=True,
            print_prefix="\n🤖 구현:\n",
        )
        used = self.llm.get_total_tokens() - tokens_before
        pipeline_messages.append({"role": "assistant", "content": impl})
        self._print_stage_footer("실행", used, advisor=True)
        return impl, pipeline_messages

    # ------------------------------------------------------------------
    # Stage 3: 리뷰
    # ------------------------------------------------------------------

    def _stage_review(
        self,
        user_input: str,
        plan: str,
        implementation: str,
        pipeline_messages: list,
        loop_idx: int = 1,
        max_loops: int = _MAX_REVIEW_LOOPS,
    ) -> tuple[Optional[str], bool, list]:
        """
        리뷰를 '설계 브리핑' 형식으로 생성.
        수정 단계가 이 브리핑을 명확한 설계도로 삼아 Haiku 중심으로 처리 가능.
        """
        header_label = "3/4 리뷰 (설계 브리핑)" if loop_idx == 1 else f"리뷰 루프 {loop_idx}/{max_loops}"
        self._print_stage_header(
            header_label, "개선점을 구조화된 브리핑으로 정리",
            [
                "Advisor(Opus)로 사용자 의도 정합성 / 개선점 / 보완점 점검",
                "출력은 수정 단계가 설계도로 쓸 수 있는 구조화 포맷",
                "결론: REVIEW_PASS(종료) 또는 REVIEW_FAIL(다음 루프)",
            ]
        )
        action = self._confirm("리뷰를 실행")
        if action in ("n", "s"):
            verdict = "스킵" if action == "s" else "취소"
            print(f"│  ↳ 리뷰 {verdict} — 현재 구현 결과를 그대로 확정")
            return None, False, pipeline_messages

        review_prompt = (
            "위 구현 결과를 system prompt의 '리뷰-수정 루프 규약 — 리뷰 단계' 포맷으로 검토하세요.\n"
            "사소한 취향 차이는 지적하지 않고, 개선 필요 항목이 비면 REVIEW_PASS로 종료하세요."
        )
        pipeline_messages.append({"role": "user", "content": review_prompt})

        tokens_before = self.llm.get_total_tokens()
        review, _, _ = self.llm.stream(
            messages=pipeline_messages,
            system=self.system_prompt,
            max_tokens=_TOKENS_REVIEW,
            use_advisor=True,
            use_rag=False,
            print_prefix="\n🔍 리뷰:\n",
        )
        used = self.llm.get_total_tokens() - tokens_before
        pipeline_messages.append({"role": "assistant", "content": review})

        has_issues = "REVIEW_FAIL" in review
        verdict = "⚠️ 수정 필요 (REVIEW_FAIL)" if has_issues else "✅ 통과 (REVIEW_PASS)"
        stage_name = f"리뷰({loop_idx}/{max_loops})"
        self._print_stage_footer(stage_name, used, advisor=True, extra=verdict)
        return review, has_issues, pipeline_messages

    # ------------------------------------------------------------------
    # Stage 4: 수정
    # ------------------------------------------------------------------

    def _stage_revise(
        self,
        user_input: str,
        implementation: str,
        review: str,
        pipeline_messages: list,
        loop_idx: int = 1,
        max_loops: int = _MAX_REVIEW_LOOPS,
    ) -> tuple[Optional[str], list]:
        """
        리뷰 브리핑을 설계도로 삼아 수정.
        브리핑이 명확한 항목은 Haiku에서 직접 처리, 판단 애매한 부분만 advisor.
        """
        header_label = "4/4 수정 (브리핑 기반)" if loop_idx == 1 else f"수정 루프 {loop_idx}/{max_loops}"
        self._print_stage_header(
            header_label, "리뷰 브리핑의 개선 항목을 우선순위대로 반영",
            [
                "설계도 역할의 리뷰 브리핑을 근거로 Haiku 중심 수정",
                "판단이 정말 애매한 부분에서만 advisor(Opus) 위임",
                "수정분만 diff 형식으로 출력 → 출력 토큰 최소화",
            ]
        )
        action = self._confirm("수정을 실행")
        if action in ("n", "s"):
            verdict = "스킵" if action == "s" else "취소"
            print(f"│  ↳ 수정 {verdict} — 기존 구현 결과를 그대로 확정")
            return None, pipeline_messages

        revise_prompt = (
            "위 리뷰 브리핑을 설계도로 삼아 수정하세요.\n"
            "system prompt의 '리뷰-수정 루프 규약 — 수정 단계' 규칙을 따릅니다."
        )
        pipeline_messages.append({"role": "user", "content": revise_prompt})

        tokens_before = self.llm.get_total_tokens()
        revised, _, _ = self.llm.stream(
            messages=pipeline_messages,
            system=self.system_prompt,
            max_tokens=_TOKENS_REVISE,
            use_advisor=True,
            use_rag=False,
            print_prefix="\n🔧 수정:\n",
        )
        used = self.llm.get_total_tokens() - tokens_before
        pipeline_messages.append({"role": "assistant", "content": revised})
        stage_name = f"수정({loop_idx}/{max_loops})"
        self._print_stage_footer(stage_name, used, advisor=True)
        return revised, pipeline_messages

    # ------------------------------------------------------------------
    # 사용자 확인
    # ------------------------------------------------------------------

    def _confirm(self, action: str) -> str:
        """Returns: 'y'(계속), 'n'(취소), 's'(스킵)"""
        try:
            ans = input(f"│\n│  ▶ {action}할까요? [Enter/y=예  n=취소  s=스킵] > ").strip().lower()
        except EOFError:
            return "n"
        if ans in ("n", "no", "아니", "취소"):
            return "n"
        if ans in ("s", "skip", "스킵"):
            return "s"
        return "y"

    # ------------------------------------------------------------------
    # 출력 헬퍼
    # ------------------------------------------------------------------

    def _print_pipeline_intro(self) -> None:
        print("\n" + "─" * 58)
        print("🔄 개발 파이프라인 시작  (Advisor=Opus, Main=Haiku)")
        print(f"   [1]계획 → [2]실행 → [리뷰 ↔ 수정 루프, 최대 {_MAX_REVIEW_LOOPS}회]")
        print("   리뷰는 설계 브리핑 형식 → 수정은 그 설계도를 그대로 반영")
        print("   각 단계 전 진행 여부를 묻습니다.")
        print("─" * 58)

    def _print_stage_header(self, step: str, title: str, bullets: list[str]) -> None:
        print(f"\n┌─ [{step}] {title}")
        for b in bullets:
            print(f"│  · {b}")

    def _print_stage_footer(
        self, stage: str, tokens_used: int, advisor: bool = False, extra: str = ""
    ) -> None:
        advisor_str = "Advisor(Opus) 활성" if advisor else "Haiku"
        extra_str = f" | {extra}" if extra else ""
        print(f"\n└─ [{stage} 완료] {advisor_str} | 이번 단계 토큰: {tokens_used:,}{extra_str}")

    def _print_pipeline_summary(self) -> None:
        costs = self.llm.get_cost_summary()
        total = self.llm.get_total_tokens()
        saved = costs.get("cache_saved", 0)
        print("\n" + "─" * 58)
        print("✅ 파이프라인 완료")
        print(f"   누적 토큰: cheap={costs.get('cheap', 0):,}  advisor={costs.get('advisor', 0):,}")
        if saved:
            pct = saved * 100 // max(total + saved, 1)
            print(f"   캐시 절약: {saved:,} 토큰 (~{pct}% 절감)")
        print("─" * 58)

    # ------------------------------------------------------------------
    # 분류 / 토큰 추정
    # ------------------------------------------------------------------

    def _is_simple_question(self, user_input: str) -> bool:
        """
        단순 질문(설명/개념) → 파이프라인 스킵.
        SHORT 키워드가 있고 CODE 키워드가 없으면 질문으로 분류.
        """
        lower = user_input.lower()
        words = set(re.split(r"[\s,]+", lower))
        has_short = bool(words & _SHORT_KEYWORDS)
        has_code = bool(words & _CODE_KEYWORDS)
        if has_short and not has_code:
            return True
        # 키워드가 전혀 없으면 짧은 입력(50자 미만)은 질문으로, 그 외는 파이프라인
        if not has_short and not has_code:
            return len(user_input) < 50
        return False

    def _estimate_max_tokens(self, user_input: str) -> int:
        lower = user_input.lower()
        words = set(re.split(r"[\s,]+", lower))
        if words & _CODE_KEYWORDS:
            return _TOKENS_CODE
        if words & _SHORT_KEYWORDS:
            return _TOKENS_SHORT
        return _TOKENS_MID

    # ------------------------------------------------------------------
    # SESSION_STATE.md 생성 (README: 최근 10개만 사용 → 입력 토큰 80% 절감)
    # ------------------------------------------------------------------

    def generate_session_state(self, session_info: dict) -> str:
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
        # complete() 사용 — 내부 처리(출력 없음), 캐시 히트 활용
        response, inp, out = self.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self.system_prompt,
            max_tokens=800,
        )
        print(f"  완료 ({inp + out:,} 토큰)")
        return response

    # ------------------------------------------------------------------
    # PROJECT.md 생성
    # ------------------------------------------------------------------

    def generate_project_context(self) -> str:
        project_structure = self.files.analyze_project_structure()
        existing = self.files.load_project_context() or "(없음)"

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
