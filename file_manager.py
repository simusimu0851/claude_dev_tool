"""
File Manager — 파일 저장/로드 전담 모듈
"""
import re
import shutil
from pathlib import Path
from typing import Optional


class FileManager:
    """세션 상태 저장/로드 및 프로젝트 파일 관리."""

    SESSION_STATE_FILENAME = "SESSION_STATE.md"
    PROJECT_FILENAME = "PROJECT.md"

    def __init__(self, output_path: str, project_path: str):
        self.output_dir = Path(output_path)
        self.project_path = Path(project_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generated_files: list[str] = []

    # ------------------------------------------------------------------
    # SESSION_STATE.md (세션마다 갱신 — 최근 작업·TODO·주의사항)
    # ------------------------------------------------------------------

    def save_session_state(self, content: str) -> Path:
        path = self.output_dir / self.SESSION_STATE_FILENAME
        path.write_text(content, encoding="utf-8")
        self.generated_files.append(str(path))
        return path

    def load_session_state(self) -> Optional[str]:
        path = self.output_dir / self.SESSION_STATE_FILENAME
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    # ------------------------------------------------------------------
    # PROJECT.md (거의 불변 — 목표·아키텍처·규칙)
    # ------------------------------------------------------------------

    def save_project_context(self, content: str) -> Path:
        """PROJECT.md 저장. 세션 간 캐시를 최대로 활용하기 위해 내용 변화 최소화."""
        path = self.output_dir / self.PROJECT_FILENAME
        path.write_text(content, encoding="utf-8")
        self.generated_files.append(str(path))
        return path

    def load_project_context(self) -> Optional[str]:
        """PROJECT.md 로드. 없으면 None."""
        path = self.output_dir / self.PROJECT_FILENAME
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def project_context_exists(self) -> bool:
        return (self.output_dir / self.PROJECT_FILENAME).exists()

    # ------------------------------------------------------------------
    # 일반 파일
    # ------------------------------------------------------------------

    def save_file(self, filename: str, content: str) -> Path:
        path = self.output_dir / filename
        path.write_text(content, encoding="utf-8")
        self.generated_files.append(str(path))
        print(f"💾 저장됨: {path}")
        return path

    # ------------------------------------------------------------------
    # Diff 적용
    # ------------------------------------------------------------------

    def apply_diff(self, diff_text: str, base_path: Optional[Path] = None) -> list[str]:
        """
        unified diff 텍스트를 파싱하여 실제 파일에 적용.
        LLM 응답의 ```diff 블록 내용을 받아 처리.
        Returns: 수정된 파일의 상대 경로 목록
        """
        base = base_path or self.project_path
        modified: list[str] = []

        sections = re.split(r"(?=^--- )", diff_text, flags=re.MULTILINE)

        for section in sections:
            if not section.strip():
                continue

            m = re.search(r"^\+\+\+ (?:b/)?(.+)", section, re.MULTILINE)
            if not m:
                continue

            rel_path = m.group(1).strip()
            if rel_path in ("/dev/null", "dev/null"):
                continue

            file_path = base / rel_path
            if not file_path.exists():
                alt = self.output_dir / rel_path
                if alt.exists():
                    file_path = alt
                else:
                    print(f"  ⚠️  파일 없음, diff 스킵: {rel_path}")
                    continue

            try:
                original = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
                patched = self._apply_hunks(original, section)
                # 백업 후 적용 (토큰 비용 0, 안정성 확보)
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                shutil.copy2(file_path, backup_path)
                file_path.write_text("".join(patched), encoding="utf-8")
                modified.append(rel_path)
                print(f"  ✅ 패치 적용됨: {rel_path}  (백업: {backup_path.name})")
            except Exception as e:
                # 백업이 있으면 복원
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                if backup_path.exists():
                    shutil.copy2(backup_path, file_path)
                    print(f"  ❌ 패치 실패, 원본 복원됨 ({rel_path}): {e}")
                else:
                    print(f"  ❌ 패치 실패 ({rel_path}): {e}")

        return modified

    def _apply_hunks(self, lines: list[str], diff_text: str) -> list[str]:
        result = list(lines)
        offset = 0

        hunk_re = re.compile(
            r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@[^\n]*\n"
            r"((?:[+\- \\][^\n]*(?:\n|$))*)",
            re.MULTILINE,
        )

        for m in hunk_re.finditer(diff_text):
            orig_start = int(m.group(1)) - 1
            hunk_body = m.group(3)

            old_block: list[str] = []
            new_block: list[str] = []

            for line in hunk_body.splitlines(keepends=True):
                if not line:
                    continue
                marker, content = line[0], line[1:]
                if marker == "-":
                    old_block.append(content)
                elif marker == "+":
                    new_block.append(content)
                elif marker in (" ", "\t"):
                    old_block.append(content)
                    new_block.append(content)

            start = orig_start + offset
            end = start + len(old_block)
            result[start:end] = new_block
            offset += len(new_block) - len(old_block)

        return result

    # ------------------------------------------------------------------
    # 선택적 컨텍스트 로딩
    # ------------------------------------------------------------------

    def read_relevant_section(
        self,
        file_path: str,
        query: str = "",
        context_lines: int = 25,
    ) -> str:
        """
        파일 전체 대신 query와 관련된 섹션만 반환.
        query 없음 또는 파일 300줄 미만 → 전체 반환.
        """
        path = Path(file_path)
        if not path.exists():
            alt = self.project_path / file_path
            if alt.exists():
                path = alt
            else:
                return f"❌ 파일 없음: {file_path}"

        raw = path.read_text(encoding="utf-8")
        content_lines = raw.splitlines()

        if not query or len(content_lines) < 150:
            return raw

        keywords = [w for w in re.split(r"\s+", query) if len(w) >= 2]
        if not keywords:
            return raw

        hit_indices: set[int] = set()
        for i, line in enumerate(content_lines):
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in keywords):
                hit_indices.add(i)

        if not hit_indices:
            return raw

        relevant: set[int] = set()
        half = context_lines // 2
        for idx in hit_indices:
            relevant.update(range(max(0, idx - half), min(len(content_lines), idx + half)))

        sorted_indices = sorted(relevant)
        result_lines: list[str] = [f"# 📄 {path.name} (관련 섹션만 발췌)\n"]
        prev = -2

        for ln in sorted_indices:
            if ln > prev + 1:
                if prev >= 0:
                    result_lines.append(f"\n... [{ln - prev - 1}줄 생략] ...\n")
            result_lines.append(f"{ln + 1:4d} | {content_lines[ln]}")
            prev = ln

        return "\n".join(result_lines)

    # ------------------------------------------------------------------
    # 프로젝트 키워드 검색 (grep 기반)
    # ------------------------------------------------------------------

    # 검색 제외 디렉토리·확장자
    _SEARCH_SKIP_DIRS = frozenset({
        ".git", "__pycache__", "node_modules", ".vs", "Library",
        "Temp", "Logs", "Builds", "obj", "bin", ".idea",
    })
    _SEARCH_EXTENSIONS = frozenset({
        ".cs", ".py", ".js", ".ts", ".lua", ".gd", ".cfg", ".json",
        ".yaml", ".yml", ".xml", ".txt", ".md", ".shader", ".hlsl",
        ".cginc", ".compute", ".html", ".css", ".sh", ".bat",
    })
    _SEARCH_MAX_RESULTS = 30

    def search_project(self, keyword: str, max_results: int = 0) -> list[dict]:
        """
        프로젝트 전체에서 keyword를 대소문자 무시로 검색.
        Returns: [{"file": 상대경로, "line": 줄번호, "text": 해당 줄 내용}, ...]
        """
        if not max_results:
            max_results = self._SEARCH_MAX_RESULTS
        keyword_lower = keyword.lower()
        results: list[dict] = []

        for path in self._iter_source_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                if keyword_lower in line.lower():
                    results.append({
                        "file": str(path.relative_to(self.project_path)),
                        "line": i,
                        "text": line.strip()[:120],
                    })
                    if len(results) >= max_results:
                        return results
        return results

    def _iter_source_files(self):
        """프로젝트 내 소스 파일을 yield. 바이너리·빌드 폴더 제외."""
        for item in self.project_path.rglob("*"):
            if any(part in self._SEARCH_SKIP_DIRS for part in item.parts):
                continue
            if item.is_file() and item.suffix.lower() in self._SEARCH_EXTENSIONS:
                yield item

    # ------------------------------------------------------------------
    # 프로젝트 구조 분석
    # ------------------------------------------------------------------

    def analyze_project_structure(self, max_items: int = 60) -> str:
        if not self.project_path.exists():
            return "❌ 프로젝트 경로 없음"

        lines: list[str] = []
        self._walk(self.project_path, lines, max_items, depth=0, prefix="")
        return "\n".join(lines)

    def _walk(self, path: Path, lines: list, max_items: int, depth: int, prefix: str) -> None:
        if depth > 3 or len(lines) >= max_items:
            return

        try:
            items = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return

        for item in items:
            if item.name.startswith(".") or item.name in ("__pycache__", "node_modules", ".git"):
                continue
            if len(lines) >= max_items:
                lines.append(f"{prefix}... (더 있음)")
                return

            if item.is_dir():
                sub_count = sum(1 for _ in item.rglob("*") if _.is_file())
                lines.append(f"{prefix}📁 {item.name}/  ({sub_count}개 파일)")
                self._walk(item, lines, max_items, depth + 1, prefix + "  ")
            else:
                size = item.stat().st_size
                size_str = f"{size:,}B" if size < 10_000 else f"{size // 1024}KB"
                lines.append(f"{prefix}📄 {item.name}  [{size_str}]")
