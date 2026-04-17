#!/usr/bin/env python3
"""
AI 개발 파이프라인 v4
"한 번 실행되는 파이프라인" → "상태를 유지하며 계속 진화하는 개발 인터페이스"

명령어:
  세션종료 / session  — 현재 상태 저장 + 세션 재시작
  exit / 프로그램종료 — 프로그램 완전 종료
  save / 저장         — 중간 저장 (세션 계속)
  status / 상태       — 현재 세션 통계
  project update      — PROJECT.md 재생성
  read <파일> [검색어] — 파일(또는 관련 섹션)을 컨텍스트에 주입
  help / 도움말       — 도움말
"""

import os
import sys
import signal
import argparse
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.history import FileHistory
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False

from llm_client import LLMClient
from memory_manager import MemoryManager
from file_manager import FileManager
from pipeline_controller import PipelineController

DEFAULT_CONFIG = {
    "cheap_model": "claude-haiku-4-5-20251001",
    "advisor_model": "claude-opus-4-6",
    "engine": "python",
    "project_path": ".",
    # output_path 미지정 시 project_path와 동일 (작업 폴더 = 출력 폴더)
    # 분리가 필요하면 --save-path 또는 pipeline_config.json으로 override
    "output_path": None,
}


class DevSession:
    """세션 기반 개발 환경. 상태를 누적하며 여러 요청을 처리."""

    def __init__(self, config: dict):
        self.config = config
        self.start_time = datetime.now()
        self.request_count = 0
        self.is_active = False
        self.state_saved = False
        self._soft_exit_warned = False

        self.llm = LLMClient(config)
        self.memory = MemoryManager()
        project_path = config.get("project_path", ".")
        output_path = config.get("output_path") or project_path
        self.files = FileManager(
            output_path=output_path,
            project_path=project_path,
        )
        self.controller = PipelineController(self.llm, self.memory, self.files, config)

    # ------------------------------------------------------------------
    # 세션 시작
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._print_banner()
        self._check_rag_server()
        self._restore_previous_state()
        self.is_active = True
        self._register_interrupt_handler()
        self._run_loop()

    def _check_rag_server(self) -> None:
        if self.llm.check_rag_server():
            print("🔍 RAG 서버 연결됨 (LLM이 필요시 자동 검색)")
        else:
            print("📭 RAG 서버 미연결 (검색 없이 진행)")

    # ------------------------------------------------------------------
    # 인터랙티브 루프
    # ------------------------------------------------------------------

    def _prompt_input(self, prompt_text: str) -> str:
        """한글 입력이 깨지지 않는 프롬프트. prompt_toolkit이 있으면 사용."""
        if _PT_AVAILABLE:
            return pt_prompt(prompt_text, history=self._input_history).strip()
        return input(prompt_text).strip()

    def _run_loop(self) -> None:
        if _PT_AVAILABLE:
            history_path = Path(self.files.output_dir) / ".input_history"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            self._input_history = FileHistory(str(history_path))
        else:
            self._input_history = None

        while True:
            try:
                user_input = self._prompt_input("\nYou > ")
            except EOFError:
                self._save_and_restart_session()
                self._quit_program()
                break

            if not user_input:
                continue

            cmd = user_input.lower()

            if cmd in ("session", "세션종료"):
                self._save_and_restart_session()
                continue

            if cmd in ("exit", "quit", "프로그램종료"):
                self._quit_program()
                break

            if cmd in ("save", "저장"):
                self._save_now()
                continue

            if cmd == "project update":
                self._update_project_context()
                continue

            if cmd.startswith("find ") or cmd.startswith("검색 "):
                self._handle_find_command(user_input)
                continue

            if cmd.startswith("read ") or cmd.startswith("파일 "):
                self._handle_read_command(user_input)
                continue

            if cmd in ("status", "상태"):
                self._print_status()
                continue

            if cmd in ("help", "도움말"):
                self._print_help()
                continue

            self.request_count += 1
            self._soft_exit_warned = False
            self.controller.process_request(user_input)

        self.is_active = False

    # ------------------------------------------------------------------
    # 세션 종료 & 재시작
    # ------------------------------------------------------------------

    def _save_and_restart_session(self) -> None:
        """
        세션 재시작 흐름:
        1. SESSION_STATE.md 생성 (compact, 최근 10개 메시지만)
        2. 메모리·카운터 초기화
        3. PROJECT.md + SESSION_STATE.md 재로드
        4. 루프 계속

        PROJECT.md는 변경하지 않음 → 캐시 블록 재사용 가능
        """
        if self.request_count == 0:
            print("\n저장할 대화가 없습니다. 세션을 초기화합니다.")
            self._reset_session()
            self._print_session_restart_banner()
            return

        print("\n" + "=" * 60)
        print("📝 세션 상태 저장 중...")
        print("=" * 60)

        session_info = self._build_session_info()

        try:
            state_content = self.controller.generate_session_state(session_info)
            saved_path = self.files.save_session_state(state_content)
            self.state_saved = True
            print(f"\n✅ SESSION_STATE.md 저장: {saved_path}")
        except Exception as e:
            print(f"\n❌ 상태 저장 실패: {e}")
            return

        self._print_token_summary(session_info)

        # 세션 초기화 후 PROJECT + SESSION 재로드
        self._reset_session()
        self._load_persistent_memories()
        self._print_session_restart_banner()

    def _quit_program(self) -> None:
        if self.request_count > 0 and not self.state_saved:
            print(
                "\n⚠️  저장되지 않은 세션이 있습니다.\n"
                "   'session' / '세션종료' 로 저장 후 종료하거나\n"
                "   그냥 종료하려면 다시 exit를 입력하세요."
            )
            try:
                confirm = self._prompt_input("exit 확인 > ").lower()
            except EOFError:
                confirm = "exit"
            if confirm not in ("exit", "quit", "프로그램종료", "y", "yes"):
                return

        print("\n👋 프로그램을 종료합니다.")
        self.is_active = False
        sys.exit(0)

    def _reset_session(self) -> None:
        self.memory = MemoryManager()
        self.controller.memory = self.memory
        self.start_time = datetime.now()
        self.request_count = 0
        self.state_saved = False
        self._soft_exit_warned = False

    # ------------------------------------------------------------------
    # 중간 저장
    # ------------------------------------------------------------------

    def _save_now(self) -> None:
        if self.request_count == 0:
            print("저장할 내용이 없습니다.")
            return

        session_info = self._build_session_info()
        print("\n💾 중간 저장 중...")
        try:
            state_content = self.controller.generate_session_state(session_info)
            saved_path = self.files.save_session_state(state_content)
            print(f"✅ 저장됨: {saved_path}")
        except Exception as e:
            print(f"❌ 저장 실패: {e}")

    # ------------------------------------------------------------------
    # PROJECT.md 갱신
    # ------------------------------------------------------------------

    def _update_project_context(self) -> None:
        """
        PROJECT.md를 현재 대화를 바탕으로 재생성.
        주의: 이 명령 실행 후 다음 세션부터 캐시 미스 발생 (내용이 바뀌므로).
        자주 실행하지 말 것 — 아키텍처·목표가 실제로 바뀔 때만 사용.
        """
        print("\n📋 PROJECT.md 갱신 중... (다음 세션부터 새 캐시 적용)")
        try:
            content = self.controller.generate_project_context()
            saved_path = self.files.save_project_context(content)
            # 현재 세션의 project_memory도 즉시 갱신
            self.memory.load_project(content)
            print(f"\n✅ PROJECT.md 저장: {saved_path}")
            print("  ℹ️  현재 세션에 즉시 반영됨")
        except Exception as e:
            print(f"❌ 생성 실패: {e}")

    # ------------------------------------------------------------------
    # 프로젝트 검색
    # ------------------------------------------------------------------

    def _handle_find_command(self, raw_input: str) -> None:
        """
        find <키워드> — 프로젝트 전체에서 키워드 검색 (grep 기반, LLM 호출 없음).
        결과에서 원하는 파일을 골라 read 명령어로 상세 확인.
        """
        parts = raw_input.split(None, 1)
        if len(parts) < 2:
            print("사용법: find <키워드>")
            return

        keyword = parts[1]
        results = self.files.search_project(keyword)

        if not results:
            print(f"🔍 \"{keyword}\" — 결과 없음")
            return

        print(f"\n🔍 \"{keyword}\" 검색 결과 ({len(results)}건):\n")

        # 파일별 그룹핑
        by_file: dict[str, list] = {}
        for r in results:
            by_file.setdefault(r["file"], []).append(r)

        for file_path, hits in by_file.items():
            print(f"  📄 {file_path}")
            for h in hits:
                print(f"     {h['line']:>4d} | {h['text']}")
            print()

        print(f"  💡 상세 확인: read <파일경로> {keyword}")

    # ------------------------------------------------------------------
    # 파일 컨텍스트 로드
    # ------------------------------------------------------------------

    def _handle_read_command(self, raw_input: str) -> None:
        """
        read <파일경로> [검색어...]
        파일 전체 또는 검색어 관련 섹션만 컨텍스트에 주입.
        """
        parts = raw_input.split(None, 2)
        if len(parts) < 2:
            print("사용법: read <파일경로> [검색어...]")
            return

        file_path = parts[1]
        query = parts[2] if len(parts) > 2 else ""

        content = self.files.read_relevant_section(file_path, query)
        if content.startswith("❌"):
            print(content)
            return

        print(f"📄 {file_path} ({content.count(chr(10))+1}줄, {len(content):,}자) → 컨텍스트 주입")
        self.memory.add_message("user", f"[파일 컨텍스트: {file_path}]\n\n{content}")
        self.memory.add_message("assistant", f"📄 {file_path} 확인했습니다. 질문해주세요.")

    # ------------------------------------------------------------------
    # 이전 상태 복원
    # ------------------------------------------------------------------

    def _restore_previous_state(self) -> None:
        """
        시작 시 PROJECT.md + SESSION_STATE.md 로드.

        PROJECT.md: 없으면 자동 생성 여부를 묻지 않고 빈 상태로 시작
                    (대화가 쌓인 후 'project update' 명령어로 생성 권장)
        SESSION_STATE.md: 있으면 이전 작업 즉시 복원
        """
        has_project = self._load_persistent_memories()

        if not has_project:
            print("  💡 PROJECT.md 없음 — 대화 후 'project update'로 생성하세요")

    def _load_persistent_memories(self) -> bool:
        """
        PROJECT.md + SESSION_STATE.md 로드.
        Returns: PROJECT.md 존재 여부
        """
        has_project = False

        project = self.files.load_project_context()
        if project:
            self.memory.load_project(project)
            print(f"📋 PROJECT.md 로드됨 ({len(project):,}자)")
            has_project = True

        session = self.files.load_session_state()
        if session:
            self.memory.load_session(session)
            print(f"📂 SESSION_STATE.md 로드됨 ({len(session):,}자) → 이전 작업 복원")
        else:
            print("📝 새 세션 시작")

        return has_project

    # ------------------------------------------------------------------
    # Ctrl+C 핸들러
    # ------------------------------------------------------------------

    def _register_interrupt_handler(self) -> None:
        signal.signal(signal.SIGINT, self._on_interrupt)

    def _on_interrupt(self, sig, frame) -> None:
        if self.is_active and self.request_count > 0 and not self.state_saved:
            if not self._soft_exit_warned:
                self._soft_exit_warned = True
                print(
                    "\n\n⚠️  세션이 저장되지 않았습니다!\n"
                    "   'session' / '세션종료' → 저장 + 재시작\n"
                    "   'save' / '저장'        → 중간 저장\n"
                    "   강제 종료: Ctrl+C 한 번 더"
                )
                signal.signal(signal.SIGINT, self._on_force_exit)
        else:
            print("\n종료합니다.")
            sys.exit(0)

    def _on_force_exit(self, sig, frame) -> None:
        print("\n강제 종료합니다. (상태 미저장)")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 출력 헬퍼
    # ------------------------------------------------------------------

    def _build_session_info(self) -> dict:
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "request_count": self.request_count,
            "duration": str(datetime.now() - self.start_time).split(".")[0],
            "engine": self.config.get("engine", "python"),
        }

    def _print_token_summary(self, session_info: dict) -> None:
        costs = self.llm.get_cost_summary()
        total = self.llm.get_total_tokens()
        saved = costs.get("cache_saved", 0)
        print(f"\n📊 토큰 사용량")
        print(f"  cheap        : {costs.get('cheap', 0):>10,}")
        print(f"  advisor      : {costs.get('advisor', 0):>10,}")
        print(f"  합계         : {total:>10,}")
        if saved:
            print(f"  캐시 절약    : {saved:>10,}  (~{saved * 100 // max(total + saved, 1)}% 절감)")
        print(f"\n⏱  경과 시간: {session_info['duration']}")
        print(f"📬 총 요청 수: {self.request_count}")

    def _print_session_restart_banner(self) -> None:
        print("\n" + "=" * 60)
        print("🔄 새 세션 시작")
        print("   PROJECT.md  → 캐시 재사용 가능 (내용 불변)")
        print("   SESSION_STATE.md → 새 캐시 생성 (세션 내 유효)")
        print("=" * 60)

    def _print_banner(self) -> None:
        engine = self.config.get("engine", "python")
        model = self.config.get("cheap_model", "N/A")
        path = self.config.get("project_path", ".")
        output = str(self.files.output_dir)
        same = (Path(path).resolve() == self.files.output_dir.resolve())

        print("\n" + "=" * 60)
        print("🚀 AI 개발 파이프라인 v4")
        print("=" * 60)
        print(f"  도메인   : {engine}")
        print(f"  작업폴더 : {path}")
        if not same:
            print(f"  출력폴더 : {output}  (분리됨)")
        print(f"  모델     : {model}")
        print()
        print("  session / 세션종료 → 저장+재시작  |  exit → 종료")
        print("  find <키워드>      → 프로젝트 검색  |  read <파일> → 컨텍스트 주입")
        print("  project update     → PROJECT.md 갱신")
        print("=" * 60)

    def _print_status(self) -> None:
        costs = self.llm.get_cost_summary()
        total = self.llm.get_total_tokens()
        saved = costs.get("cache_saved", 0)
        duration = str(datetime.now() - self.start_time).split(".")[0]

        print(f"\n📊 세션 현황")
        print(f"  요청 수         : {self.request_count}")
        print(f"  메시지 수       : {self.memory.total_messages()}")
        print(f"  요약 메모리     : {'있음' if self.memory.summary_memory else '없음'}")
        print(f"  PROJECT.md      : {'있음' if self.memory.project_memory else '없음'}")
        print(f"  SESSION_STATE   : {'있음' if self.memory.session_memory else '없음'}")
        print(f"  토큰 (cheap)    : {costs.get('cheap', 0):,}")
        print(f"  토큰 (advisor)  : {costs.get('advisor', 0):,}")
        print(f"  총 토큰         : {total:,}")
        if saved:
            print(f"  캐시 절약       : {saved:,}  (~{saved * 100 // max(total + saved, 1)}% 절감)")
        print(f"  경과 시간       : {duration}")

    def _print_help(self) -> None:
        print("\n💡 사용 가능한 명령")
        print("  session / 세션종료          — 상태 저장 + 세션 재시작")
        print("  exit / 프로그램종료         — 프로그램 완전 종료")
        print("  save / 저장                 — 중간 저장 (세션 계속)")
        print("  project update              — PROJECT.md 재생성 (아키텍처 변경 시)")
        print("  find <키워드> / 검색        — 프로젝트 전체 키워드 검색 (토큰 비용 0)")
        print("  read <파일> [검색어...]     — 파일 또는 관련 섹션을 컨텍스트에 주입")
        print("  status / 상태               — 세션 통계 + 캐시 절약량")
        print("  help / 도움말               — 이 도움말")
        print("  (그 외 입력)                — AI에게 전달")
        print()
        print("  💡 find로 위치 파악 → read로 상세 주입 (2단계 워크플로우)")
        print("  💡 파일 수정 시 AI는 unified diff로 응답 → 자동 패치")
        print("  💡 PROJECT.md는 자주 바꾸면 캐시 미스 발생 — 꼭 필요할 때만 갱신")


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

def _load_config_file() -> dict:
    config_path = Path("pipeline_config.json")
    if config_path.exists():
        import json
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️  pipeline_config.json 로드 실패: {e}")
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="🚀 AI 개발 파이프라인 v4")
    parser.add_argument("--engine", default=None)
    parser.add_argument(
        "--path", default=None,
        help="작업 폴더. PROJECT.md·SESSION_STATE.md도 여기에 읽고 씀 (기본).",
    )
    parser.add_argument(
        "--save-path", default=None,
        help="PROJECT.md·SESSION_STATE.md·생성 파일을 별도 폴더에 저장하고 싶을 때만 지정.",
    )
    args = parser.parse_args()

    config = DEFAULT_CONFIG.copy()
    config.update(_load_config_file())
    if args.engine:
        config["engine"] = args.engine
    if args.path:
        config["project_path"] = args.path
    if args.save_path:
        config["output_path"] = args.save_path

    if not os.getenv("ANTHROPIC_API_KEY") and not config.get("api_key"):
        print("\n❌ ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    try:
        session = DevSession(config)
        session.start()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    except Exception as e:
        print(f"\n❌ 예상치 못한 오류: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
