# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 참조하는 가이드 문서입니다.

## 프로젝트 개요

이것은 **AI 지원 개발 파이프라인**으로, Claude AI 모델과 통합된 인터랙티브 세션 기반 개발 환경입니다. 여러 상호작용에 걸쳐 컨텍스트를 유지하면서 소프트웨어 개발 작업을 지원하도록 설계되었으며, 게임 개발(Unity)에 초점을 맞추고 있지만 모든 프로젝트에 확장 가능합니다.

**핵심 특징**: 전통적인 REPL 환경과 달리, 이 파이프라인은 세션 간 **영구적인 상태**를 유지하여 컨텍스트와 의사결정이 계속 이어지는 장기 개발 워크플로우를 가능하게 합니다.

## 아키텍처

프로젝트는 **SRP(단일 책임 원칙) 기반의 모듈식 설계**로 여섯 가지 핵심 구성 요소를 가집니다:

### 1. **dev_pipeline_v4.py** (진입점)
- `DevSession` 클래스: 세션 생명주기, 인터랙티브 루프, 명령어 디스패치 관리
- 사용자 입력 및 명령어 라우팅 처리 (`prompt_toolkit` 있으면 자동 사용 — 한글 입력 개선 + 히스토리 파일 지원)
- 신호 처리 구현 (Ctrl+C 1회: 경고, 2회: 강제 종료)
- 이전 세션 상태 복원 관리
- 시작 시 RAG 서버 연결 확인 (`_check_rag_server`)
- **주요 명령어**:
  - `exit` / `quit` / `프로그램종료`: 프로그램 완전 종료 (미저장 상태 경고 포함)
  - `session` / `세션종료`: SESSION_STATE.md 저장 후 재시작 (PROJECT.md는 유지)
  - `save` / `저장`: 체크포인트 저장 (세션 재시작 없음)
  - `project update`: PROJECT.md 재생성 (아키텍처 변경 시만 — 캐시 미스 유발)
  - `find <키워드>` / `검색 <키워드>`: 프로젝트 전체 grep 검색 (LLM 호출 없음, 토큰 비용 0)
  - `read <파일> [검색어]` / `파일 <파일> [검색어]`: 파일 또는 관련 섹션을 컨텍스트에 주입
  - `status` / `상태`: 세션 통계 + 캐시 절약량 출력
  - `help` / `도움말`: 도움말 표시
  - 그 외 모든 텍스트: AI에게 개발 요청으로 전달

### 2. **llm_client.py** (API 통신)
- Anthropic SDK를 래핑하여 Claude API 호출 담당
- 스트리밍(`stream`) 및 비스트리밍(`complete`) 응답 지원
- 모델 등급별 토큰 사용량 추적 (`cost_tracker` dict)
- 기본 모델 (`DEFAULT_MODELS`):
  - `cheap`: claude-haiku-4-5-20251001 (빠르고 저렴 — 압축, 상태 생성, 일반 응답)
  - `advisor`: claude-opus-4-6 (높은 추론 — advisor 도구 사용 시)
  - `standard`: claude-sonnet-4-6 (균형)
- `stream()`: 실시간 출력 + full_response 반환, system prompt에 `cache_control` 적용
  - `use_rag=True`이면 RAG 도구를 포함하여 LLM이 tool_use 시 MCP 서버 자동 호출 → 결과 주입 → 재응답 루프
- `complete()`: 화면 출력 없음, 내부 처리(압축, 상태 생성)에만 사용
- `_build_advisor_tools()`: advisor beta 도구 구성 (`advisor-tool-2026-03-01`)
- `check_rag_server()`: 시작 시 RAG 서버 접속 확인
- 캐시 통계 실시간 로그 (`_log_cache_stats`: 히트율/생성량 출력)
- 캐시 절약량 계산: `cache_read * 0.9` (캐시 히트 시 90% 비용 절감)

### 3. **memory_manager.py** (5계층 메모리 시스템)
핵심 혁신 기능으로, 긴 대화에서 컨텍스트를 관리합니다:

1. **Working Memory**: 최근 10개 메시지 (`WORKING_SIZE`, Sliding의 읽기 전용 뷰, 압축 불가)
2. **Sliding Memory**: 현재 대화 이력 (메시지 추가 시 코드 블록 >600자 축약)
3. **Summary Memory**: append-only chunk 기반 요약 (최대 8개 chunk, 초과 시 가장 오래된 2개 병합)
4. **Session Memory**: 이전 세션 상태 (SESSION_STATE.md — 최근 작업·TODO·주의사항)
5. **Project Memory**: 프로젝트 고정 컨텍스트 (PROJECT.md — 목표·아키텍처·규칙)

**압축 트리거 조건** (둘 중 하나):
- Sliding 메시지 수 > 50개 (`SLIDING_LIMIT`)
- 총 문자 수 > 120,000자 (`MESSAGE_CHAR_LIMIT`, ~30k 토큰)

**파일 컨텍스트 자동 축약**: 새 파일이 `read` 명령어로 주입되면 이전 `[파일 컨텍스트:]` 메시지를 헤더+메타데이터만 남기고 축약 (`_shrink_old_file_contexts`)

**캐시 전략 (prompt caching)**:
- `system.md`: 항상 동일 → 최강 캐시 후보
- `PROJECT.md`: 세션 재시작에도 내용 불변 → 세션 간 캐시 재사용 가능
- `SESSION_STATE.md`: 세션 내에서 불변 → 세션 내 2번째 호출부터 캐시 히트
- Summary chunks: append-only → 이전 chunk는 prefix caching으로 자동 히트, 마지막 chunk에만 `cache_control`
- sliding_memory: 매 메시지마다 변경 → 캐시 불가 (정상)

API 메시지 주입 순서: `[PROJECT.md + cache]` → `[SESSION_STATE.md + cache]` → `[Summary chunks + cache]` → `[Sliding memory]`

### 4. **file_manager.py** (파일 작업)
- `SESSION_STATE.md` / `PROJECT.md` 저장·로드 (세션 영구성)
- **diff 적용** (`apply_diff`): unified diff 파싱 → `.bak` 백업 후 파일 패치, 실패 시 자동 복원
- **선택적 파일 로딩** (`read_relevant_section`): 150줄 미만이면 전체, 이상이면 검색어 관련 섹션만 (context 25줄)
- **프로젝트 검색** (`search_project`): grep 기반 키워드 검색 (최대 30건, 바이너리·빌드 폴더 제외)
- 프로젝트 구조 분석 (깊이 3, 최대 60항목의 트리 뷰)
- 모든 출력은 `output_dir` (기본 = `project_path`, `--save-path` 지정 시 해당 폴더)로 저장

### 5. **pipeline_controller.py** (요청 오케스트레이션)
- **요청 분기**:
  - `_is_simple_question` → 단순 질문 (설명/왜/what) → `_run_direct`로 파이프라인 스킵
  - 코드 요청 → `_run_pipeline`로 4단계 파이프라인 진입
- **파이프라인 흐름** (`_run_pipeline`): `계획 → 실행 → [리뷰 ↔ 수정 루프, 최대 _MAX_REVIEW_LOOPS회]`
  1. **Stage 1: 계획** (`_stage_plan`) — 수정/생성 파일, 구현 순서, 리스크 분석. 코드 작성 없음. `use_advisor=True, use_rag=False` (설계 판단 → RAG 도구 정의 토큰 절약)
  2. **Stage 2: 실행** (`_stage_execute`) — 계획 기반 구현, diff 출력. `use_advisor=True, use_rag=True`, 완료 후 `_extract_and_apply_diffs` 즉시 적용
  3. **Stage 3-4: 리뷰-수정 루프** — `_MAX_REVIEW_LOOPS=3` 상한. 매 루프에서:
     - **`_stage_review`** — Opus advisor로 **설계 브리핑** 생성. 출력 포맷: `# 리뷰 브리핑 / ## 사용자 의도와의 정합성 / ## 개선 필요 항목 (P1, P2…) / ## 보완할 점 / ## 결론 REVIEW_PASS|REVIEW_FAIL`. `use_advisor=True, use_rag=False`
     - **`_stage_revise`** — 리뷰 브리핑을 **설계도**로 삼아 수정. "명확한 항목은 Haiku에서 직접 처리, 판단이 정말 애매한 부분에서만 advisor 호출" 프롬프트로 Haiku 중심 유도. 수정분만 diff. `use_advisor=True, use_rag=False`
     - **루프 종료 조건**: `REVIEW_PASS` / 사용자 `n·s` / `_MAX_REVIEW_LOOPS` 도달
     - 매 루프 끝에 `current_impl ← revised`로 갱신 → 다음 리뷰는 수정분 대상으로 재평가
- **각 단계 사용자 확인** (`_confirm`): `y/Enter`=진행, `n`=취소, `s`=스킵. 리뷰/수정 루프는 매 반복마다 다시 물음
- **단계별 브리핑**: 헤더(무슨 일이 일어나는지) + 풋터(advisor 활성 여부 + 이번 단계 토큰 + 루프 인덱스)
- **pipeline_messages**: 파이프라인 내부 누적 메시지 리스트. 루프 내 리뷰·수정 응답도 여기 append → 다음 루프 리뷰가 수정본을 전제로 검토 가능. prefix caching으로 앞부분 캐시 재사용. memory에는 최종 assistant만 저장하여 다음 요청 컨텍스트 깔끔 유지
- **advisor 도구 전략** (토큰 절약의 핵심): Haiku가 메인, `use_advisor=True`면 Opus advisor 도구가 제공됨 → Haiku가 복잡한 추론이 필요할 때만 advisor 호출. 특히 수정 단계는 리뷰 브리핑이 설계도 역할을 하므로 Haiku만으로 처리되는 비율이 높음 (advisor 호출률 추가 하락)
- **동적 max_tokens** (단계별 상수 + `_estimate_max_tokens`):
  - `_TOKENS_PLAN=1500` / `_TOKENS_EXEC=4096` / `_TOKENS_REVIEW=1500` (설계 브리핑 포맷 수용) / `_TOKENS_REVISE=4096`
  - 직접 응답: 코드 키워드 4096, 질문 800, 기타 2048
- **루프 비용 상한**: `_MAX_REVIEW_LOOPS=3`. 3회 도달 시 현재 구현 확정. 비용 폭주 방지
- **diff 자동 적용** (`_extract_and_apply_diffs`): 실행 + 매 수정 루프 완료 직후 호출하여 ````diff` 블록을 FileManager로 패치
- SESSION_STATE.md 생성: 최근 10개 메시지 + 요약만 사용 (입력 토큰 ~80% 절감), compact 불릿 포맷
- PROJECT.md 생성: 프로젝트 구조 + 최근 20개 메시지 + 세션 상태 분석
- `prompts/system.md`에서 시스템 프롬프트 로드 (advisor 도구 사용 지침 포함)

### 6. **rag_client.py** (RAG/MCP 지식 검색)
- MCP SSE 서버에 연결하여 외부 지식 검색 전담
- 두 가지 검색 도구 정의 (`RAG_TOOLS`):
  - `ask_knowledge_base`: 개인 옵시디언 노트(공부 내용, AI 요약본, 메모) 검색
  - `ask_resolve_api`: 라이브러리 공식 문서(DaVinci Resolve, Unity 등) API 검색
- `call_tool()`: 동기 래퍼 — 매 호출마다 SSE 연결 → initialize → call → 종료 (로컬 서버라 오버헤드 무시)
- `is_available()`: 서버 접속 확인 (httpx로 health check)
- LLM이 `tool_use`로 검색 요청 → RAGClient가 MCP 서버 호출 → 결과를 대화에 주입 → LLM 재응답

## 파이프라인 실행 방법

### 설정
```bash
# 의존성 설치
pip install anthropic python-dotenv mcp httpx

# 선택적 의존성 (한글 입력 개선 + 입력 히스토리)
pip install prompt_toolkit

# API 키 설정 (환경변수 또는 .env 파일)
export ANTHROPIC_API_KEY="sk-ant-..."
# 또는 .env 파일 생성:
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### 파이프라인 시작
```bash
python dev_pipeline_v4.py
```

### 설정 방법
두 가지 방식:

**방식 1: 커맨드라인 인자**
```bash
python dev_pipeline_v4.py --engine unity --path /my/project --save-path ./output
```

**방식 2: pipeline_config.json 수정** (권장)
```json
{
  "cheap_model": "claude-haiku-4-5-20251001",
  "advisor_model": "claude-opus-4-6",
  "engine": "unity",
  "project_path": ".",
  "rag_server_url": "http://127.0.0.1:8097"
}
```

코드 내 기본값 (`DEFAULT_CONFIG` in dev_pipeline_v4.py):
```python
{
    "cheap_model": "claude-haiku-4-5-20251001",
    "advisor_model": "claude-opus-4-6",
    "engine": "python",
    "project_path": ".",
    "output_path": None,  # None이면 project_path와 동일
}
```

**경로 규칙** (v4 변경):
- `--path` (`project_path`): 작업 폴더. 소스 코드 검색/diff 적용 대상 + `PROJECT.md`·`SESSION_STATE.md`도 여기서 읽고 씀 (기본).
- `--save-path` (`output_path`): 메타 파일을 별도 폴더에 분리하고 싶을 때만 지정. 미지정 시 `project_path` 그대로 사용.
- `.bak` 백업·diff 패치는 항상 `project_path` 기준 (분리 시에도 동일).

설정 우선순위: `DEFAULT_CONFIG` → `pipeline_config.json` 덮어쓰기 → CLI 인자 덮어쓰기

## 핵심 개념

### 두 개의 영구 상태 문서

**PROJECT.md** (`project update` 명령어 또는 최초 생성 시):
- 목표, 아키텍처, 핵심 파일, 규칙 등 **세션 재시작에도 변하지 않는** 정보
- 내용이 바뀌지 않으면 Anthropic prompt cache가 **세션 간** 재사용됨
- 자주 갱신하면 캐시 효과가 줄어드므로 아키텍처 변경 시에만 실행

**SESSION_STATE.md** (`session` / `save` 명령어 실행 시):
- 마지막 작업, 진행 중 항목, TODO, 주의사항만 — compact 불릿 포맷
- 최근 10개 메시지만 사용하여 생성 (전체 대화 대비 입력 토큰 ~80% 절감)
- 세션 내에서 불변이므로 2번째 API 호출부터 캐시 히트
- 저장 위치: 기본은 `project_path` (작업 폴더). `--save-path` 지정 시 해당 폴더

### 2단계 검색 워크플로우
1. `find <키워드>` → 프로젝트 전체에서 파일별 그룹핑된 검색 결과 (토큰 비용 0)
2. `read <파일경로> [검색어]` → 원하는 파일의 관련 섹션만 컨텍스트에 주입

### 컨텍스트 압축
압축 조건 (둘 중 하나 충족 시):
- Sliding 메시지 > 50개 (`SLIDING_LIMIT`)
- 총 문자 수 > 120,000자 (`MESSAGE_CHAR_LIMIT`, ~30k 토큰)

압축 흐름:
1. 사용자 화면에 표시: `[💭 컨텍스트 압축 중 — 잠시 기다려주세요...]`
2. Working Memory(최근 10개)를 제외한 나머지를 haiku 모델로 요약
3. 요약이 새 Summary chunk로 append (기존 chunk 불변 → 캐시 유지)
4. Summary chunk > 8개 초과 시 가장 오래된 2개를 단순 병합 (LLM 호출 없음)

### diff 자동 적용
LLM 응답에 ````diff` 블록이 포함되면:
1. 정규식으로 diff 블록 추출
2. 대상 파일의 `.bak` 백업 생성
3. hunk 단위로 패치 적용
4. 실패 시 백업에서 자동 복원

### 비용 추적
LLM 클라이언트는 모델 등급별 입출력 토큰을 추적합니다. 확인 방법:
- `status` 명령어: cheap, advisor 토큰 및 캐시 절약량 표시
- 세션 저장 시: 모델별 사용량 세분화 + 경과 시간

## 개발 가이드라인

### 새 기능 추가
- `PipelineController.process_request()`에 비즈니스 로직 추가 또는 새 컨트롤러 메서드 생성
- 메모리 업데이트는 `MemoryManager.add_message()`를 통해 자동으로 수행
- 파일 출력은 `FileManager.save_file()`을 통해 처리

### 시스템 프롬프트 수정
`prompts/system.md`를 편집합니다. 프롬프트는 세션 시작 시 한 번 로드되고 모든 후속 요청에 사용됩니다. 핵심 원칙:
- "이 세션 내에서 이전 대화 내용을 항상 참조합니다"
- 실행 가능하고 완전한 구현에 집중
- 추측성 추상화 피하기
- 파일 수정 시 unified diff 형식 강제

### 디버깅
- 생성된 `SESSION_STATE.md`를 확인하여 상태 이관 이해하기
- `MemoryManager.needs_compression()`으로 압축 임계값 확인 (50개 메시지 또는 120k 문자)
- 캐시 로그 확인: 매 API 호출마다 `[캐시] 히트 N t (X%) | 생성 M t` 출력
- 상태 출력에서 토큰 사용량 검토; 제한에 근접하면 세션 저장/재시작 트리거

### 언어 규칙
- 코드는 주로 영어, 한글 주석 (사용자 대면 출력용)
- 사용자 대면 메시지는 한글 (감정 표현을 위해 이모지 접두사 사용)
- 주석은 "무엇인가"가 아닌 "왜인가"를 설명

## 중요한 구현 세부사항

1. **세션 재시작 ≠ 메모리 손실**: `session` 명령어는 상태를 디스크에 저장한 후 MemoryManager를 새로 생성하고 PROJECT.md + SESSION_STATE.md를 재로드합니다.

2. **스트리밍 vs. 저장**: `llm_client.stream()`은 실시간 출력과 full_response 반환을 분리합니다. `complete()`는 화면 출력 없이 내부 처리(압축, 상태 생성)에만 사용합니다.

3. **첫 세션에는 영구 메모리 없음**: SESSION_STATE.md가 생성될 때까지 `session_memory`는 빈 문자열입니다. PROJECT.md도 없으면 빈 상태로 시작 — `project update`로 생성 권장.

4. **SRP 아키텍처**: 각 모듈은 하나의 책임을 가집니다:
   - LLMClient: API 호출 + 캐시 관리 + RAG 도구 통합
   - MemoryManager: 5계층 메모리 + 압축
   - FileManager: 파일 I/O + diff 패치
   - PipelineController: 요청 오케스트레이션 + 상태 생성
   - RAGClient: MCP 서버 통한 외부 지식 검색
   - DevSession: CLI 루프 + 세션 생명주기

5. **설정 우선순위**: `DEFAULT_CONFIG` → `pipeline_config.json` → CLI 인자 (--engine, --path, --save-path)

6. **캐시 breakpoint 배치**: system prompt (1개) + PROJECT.md (1개) + SESSION_STATE.md (1개) + Summary 마지막 chunk (1개) = 최대 4개 캐시 breakpoint

## 출력 파일

기본: `output_path`가 `project_path`와 동일 — 모두 작업 폴더에 저장됨:
- `SESSION_STATE.md`: 다음 세션을 위한 영구 상태 (저장 시 자동 생성)
- `PROJECT.md`: 프로젝트 고정 컨텍스트 (project update 시 생성)
- `.input_history`: prompt_toolkit 입력 히스토리
- 사용자 요청으로 생성된 파일들 (`FileManager.save_file()`을 통해 저장)
- `.bak` 파일: diff 적용 전 자동 백업 (프로젝트 디렉토리 내)

`--save-path`로 분리 시: 위 메타 파일들은 별도 폴더로, `.bak` 백업과 diff 패치는 여전히 `project_path`에 적용됨.
