[README.md](https://github.com/user-attachments/files/26777444/README.md)
# dev_pipeline_v4

**AI-Powered Development Pipeline with Session Persistence**

Claude AI 모델을 기반으로 상태를 유지하며 장기 개발 워크플로우를 지원하는 인터랙티브 개발 환경입니다.

## 🎯 개요

전통적인 REPL 환경과 달리, 이 파이프라인은 세션 간 **영구적인 상태**를 유지합니다. 복잡한 개발 작업을 여러 상호작용에 걸쳐 진행할 때, 이전 컨텍스트와 의사결정이 자동으로 다음 세션으로 이어져 개발 효율을 극대화합니다.

- **게임 개발(Unity)에 최적화** — 모든 프로젝트에 확장 가능
- **5계층 메모리 시스템** — 효율적인 컨텍스트 관리
- **자동 diff 적용** — AI 응답의 코드 수정 자동 적용
- **RAG 통합** — MCP 서버를 통한 외부 지식 검색
- **비용 추적** — 모델별 토큰 사용량 실시간 모니터링

## ⚡ 주요 기능

### 1. 세션 영속성
- `SESSION_STATE.md`: 최근 작업 상태, TODO, 주의사항 자동 저장
- `PROJECT.md`: 프로젝트 아키텍처 및 규칙 (세션 간 캐시 재사용)
- 세션 재시작 시에도 이전 대화 컨텍스트 완전 복원

### 2. 지능형 메모리 관리
- **Working Memory**: 최근 10개 메시지 (압축 불가)
- **Sliding Memory**: 현재 대화 이력 (길이 초과 시 자동 축약)
- **Summary Memory**: 요약 기반 청크 관리 (최대 8개 chunk)
- **압축 조건**: 메시지 50개 또는 총 120k 문자 초과 시 자동 실행

### 3. Prompt Caching
- System prompt, PROJECT.md, SESSION_STATE.md 캐시
- 세션 내 2번째 요청부터 캐시 히트로 **입력 토큰 90% 절감**
- Summary chunks는 append-only로 관리하여 기존 캐시 유지

### 4. 자동 코드 수정
응답에 ````diff` 블록이 포함되면:
- 자동 감지 및 파일 패치
- 실패 시 `.bak` 백업에서 자동 복원
- 사용자 개입 없이 코드 적용

### 5. RAG/MCP 통합
- 개인 지식베이스 검색 (옵시디언 노트, 메모)
- 라이브러리 공식 문서 자동 검색 (DaVinci Resolve, Unity 등)
- LLM이 필요시 자동으로 도구 호출

## 🚀 빠른 시작

```bash
# 1. 의존성 설치
pip install anthropic python-dotenv mcp httpx

# 선택: 한글 입력 개선 + 히스토리 지원
pip install prompt_toolkit

# 2. API 키 설정
export ANTHROPIC_API_KEY="sk-ant-..."
# 또는 .env 파일
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# 3. 파이프라인 시작
python dev_pipeline_v4.py
```

## 📖 사용법

### 기본 명령어

```
텍스트 입력 (일반)     → AI에게 개발 요청으로 전달
find <키워드>         → 프로젝트 전체 검색 (토큰 0)
read <파일> [검색어]  → 파일의 관련 섹션 컨텍스트 주입
save / 저장           → 체크포인트 저장 (세션 계속)
session / 세션종료    → 상태 저장 + 세션 재시작
project update        → PROJECT.md 재생성 (아키텍처 변경 시만)
status / 상태         → 세션 통계 + 캐시 절약량 표시
help / 도움말         → 도움말 표시
exit / 프로그램종료   → 프로그램 완전 종료
```

### 예시 워크플로우

```bash
# 1단계: 프로젝트 구조 파악
프로젝트의 주요 파일들을 정리해줘
→ AI가 파일 목록 정렬 + 권장사항 제시

# 2단계: 특정 파일 검색
find authentication
→ 인증 관련 파일 전체 나열

# 3단계: 상세 분석
read src/auth.py __init__
→ __init__ 메서드 관련 섹션만 로드

# 4단계: 개발 요청
로그인 에러 핸들링을 개선해줘
→ diff 자동 적용 + SESSION_STATE.md 업데이트

# 5단계: 상태 저장
save
→ 현재 진행도 저장
```

## ⚙️ 설정

### pipeline_config.json (권장)

```json
{
  "cheap_model": "claude-haiku-4-5-20251001",
  "advisor_model": "claude-opus-4-6",
  "engine": "unity",
  "project_path": "/my/game/project",
  "output_path": "./game_dev_output",
  "rag_server_url": "http://127.0.0.1:8097"
}
```

### CLI 인자

```bash
python dev_pipeline_v4.py \
  --engine unity \
  --path /my/game/project \
  --save-path ./output
```

### 설정 우선순위
1. 코드 기본값 (`DEFAULT_CONFIG`)
2. `pipeline_config.json` 덮어쓰기
3. CLI 인자 덮어쓰기

## 🏗️ 아키텍처

6개의 독립적 모듈 (단일 책임 원칙):

| 모듈 | 역할 |
|------|------|
| **dev_pipeline_v4.py** | 세션 생명주기 + CLI 루프 + 명령어 디스패치 |
| **llm_client.py** | Anthropic API + 캐시 관리 + RAG 도구 통합 |
| **memory_manager.py** | 5계층 메모리 + 자동 압축 |
| **file_manager.py** | 파일 I/O + diff 패치 + 선택적 로딩 |
| **pipeline_controller.py** | 요청 오케스트레이션 + 상태 생성 |
| **rag_client.py** | MCP 서버 통한 외부 지식 검색 |

## 📊 메모리 층계

```
┌─────────────────────────────────┐
│  1. Working Memory (최근 10개)    │ ← 압축 제외
├─────────────────────────────────┤
│  2. Sliding Memory (대화 이력)    │ ← 자동 축약
├─────────────────────────────────┤
│  3. Summary Memory (요약 청크)    │ ← append-only
├─────────────────────────────────┤
│  4. Session Memory (상태 문서)    │ ← SESSION_STATE.md
├─────────────────────────────────┤
│  5. Project Memory (고정 정보)    │ ← PROJECT.md
└─────────────────────────────────┘
```

**압축 트리거**:
- Sliding 메시지 > 50개, 또는
- 총 문자 > 120,000자 (~30k 토큰)

## 💰 비용 추적

모든 요청마다 모델별 토큰 사용량 기록:

```
status 명령어 출력:
─────────────────────
cheap (haiku) 모델: 12,345 입력 + 3,456 출력
advisor (opus) 모델: 0 입력 + 0 출력
캐시 절약: 8,765 tokens (입력 토큰의 90%)
```

## 🔌 RAG 서버 연동

MCP 호환 서버에 연결하면 자동 검색 가능:

```python
# rag_client.py에 정의된 도구
- ask_knowledge_base    # 개인 지식베이스 (옵시디언 등)
- ask_resolve_api       # 라이브러리 API 문서
```

**연결 확인**:
```bash
# 서버 실행 (별도 터미널)
python -m mcp.server rag_server  # 포트 8097

# 파이프라인 시작 시 자동 감지
🔍 RAG 서버 연결됨 (LLM이 필요시 자동 검색)
```

## 📝 주요 개념

### 영구 상태 문서

**PROJECT.md** (`project update` 시 생성):
- 목표, 아키텍처, 핵심 파일, 규칙
- 내용 불변 → 세션 간 캐시 재사용
- 자주 갱신하지 말 것 (캐시 효과 감소)

**SESSION_STATE.md** (`save` / `session` 시 생성):
- 마지막 작업, TODO, 주의사항 (compact 포맷)
- 최근 10개 메시지만 사용 → 토큰 80% 절감
- 세션 재시작 시 자동 로드

### 2단계 검색 워크플로우

```
1️⃣ find <키워드>
   → 프로젝트 전체 grep 검색 (토큰 비용 0)
   → 파일별 그룹핑 결과 표시

2️⃣ read <파일> [검색어]
   → 관련 섹션만 컨텍스트 주입
   → 150줄 미만이면 전체, 초과면 검색어 기반
```

### 동적 max_tokens 추정

| 요청 타입 | max_tokens |
|----------|-----------|
| 구현/버그/fix 관련 | 4,096 |
| 설명/why/what 질문 | 800 |
| 기타 | 2,048 |

## 🛠️ 개발 가이드

### 새 기능 추가
```python
# PipelineController.process_request()에 로직 추가
# 또는 새 컨트롤러 메서드 작성

# 메모리 업데이트 (자동)
self.memory.add_message(...)

# 파일 출력 (자동)
self.files.save_file(...)
```

### 시스템 프롬프트 수정
- `prompts/system.md` 편집
- 세션 시작 시 한 번 로드 → 모든 후속 요청에 적용

### 디버깅
```bash
# 생성된 상태 문서 확인
cat output_path/SESSION_STATE.md
cat output_path/PROJECT.md

# 캐시 로그 (매 API 호출마다)
[캐시] 히트 12,345 t (45%) | 생성 8,765 t

# 메모리 압축 조건 확인
- Sliding 메시지 수: MemoryManager.messages 길이
- 총 문자 수: 120,000자 임계값
```

## 📋 구조

```
.
├── dev_pipeline_v4.py         # 진입점 (세션 루프)
├── llm_client.py              # Anthropic API 클라이언트
├── memory_manager.py          # 5계층 메모리 + 압축
├── file_manager.py            # 파일 I/O + diff 패치
├── pipeline_controller.py      # 요청 오케스트레이션
├── rag_client.py              # MCP 서버 통한 검색
├── prompts/
│   └── system.md              # 시스템 프롬프트
├── pipeline_config.json       # 설정 파일 (선택)
├── SESSION_STATE.md           # 세션 상태 (자동 생성)
├── PROJECT.md                 # 프로젝트 정보 (자동 생성)
└── README.md
```

## 📦 의존성

**필수**:
- `anthropic` — Claude API 호출
- `python-dotenv` — 환경변수 로드
- `mcp` — MCP 프로토콜
- `httpx` — HTTP 클라이언트

**선택**:
- `prompt_toolkit` — 한글 입력 개선 + 히스토리

```bash
# 모두 설치
pip install anthropic python-dotenv mcp httpx prompt_toolkit
```

## 🔑 환경변수

```bash
ANTHROPIC_API_KEY          # Anthropic API 키 (필수)
RAG_SERVER_URL             # RAG 서버 주소 (선택, 기본: http://127.0.0.1:8097)
```

또는 `.env` 파일:
```
ANTHROPIC_API_KEY=sk-ant-...
RAG_SERVER_URL=http://127.0.0.1:8097
```

## 💡 팁

- **세션 저장 타이밍**: 중요한 지점마다 `save` 실행 → 체크포인트 생성
- **RAG 활용**: `find`로 로컬 검색 후, 필요시 `read`로 로드 → 컨텍스트 절약
- **캐시 최적화**: PROJECT.md 자주 갱신 금지 (캐시 breakpoint 유지)
- **비용 모니터링**: `status` 명령어로 토큰 사용량 확인 → 필요시 세션 재시작


