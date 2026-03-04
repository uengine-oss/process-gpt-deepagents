# ProcessGPT DeepAgents Action Server

ProcessGPT Agent SDK 폴링 서버 형태로, LangChain **Deep Agents (deepagents)** 로
DB 기반 멀티 에이전트 오케스트레이션을 수행하고, 결과를 A2A 이벤트로 래핑해 반환합니다.

## 아키텍처

```
ProcessGPT Agent SDK 폴링
       │
       ▼
DeepAgentsActionExecutor.execute(context, event_queue)
       │
       ├─ extras.agents (ID 목록) ──► Supabase users 테이블 조회 (database.get_supabase)
       │
       ├─ 스킬 로딩 ──► Skills API 호출 (SKILL.md)
       ├─ MCP 툴 ──► SafeToolLoader (에이전트별 필터링)
       ├─ 서브에이전트 구성 ──► create_deep_agent(subagents=[...])
       ▼
A2A 이벤트 발행 (TaskStatusUpdateEvent / TaskArtifactUpdateEvent)
```

## 구성

- `deepagents_action_server.py`
  - ProcessGPT Agent SDK 서버 엔트리포인트 (폴링)
- `deepagents_action_executor.py`
  - A2A 래핑 멀티 에이전트 실행기 (AgentExecutor)
- `database.py`
  - Supabase 클라이언트 (agent_loader 등에서 사용)
- `deep_agent_factory.py`
  - 오케스트레이터 에이전트 생성 (서브에이전트 포함)
- `agent_loader.py`
  - Supabase에서 에이전트 정의 로딩, 스킬 API 호출, 서브에이전트 설정 구성
- `output_utils.py`
  - LLM 결과를 ProcessGPT 반환 포맷으로 변환
- `health_server.py`
  - `/health` 엔드포인트 제공

## 환경변수

| 변수 | 설명 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase 서비스 롤 키 |
| `SKILLS_API_BASE_URL` | 스킬 파일 API 베이스 URL (기본: `http://localhost:3000`) |
| `OPENAI_API_KEY` | OpenAI API 키 |
## 로컬 테스트 설치 (버전 충돌 회피)

`process-gpt-llm-factory`가 `langchain<0.4`를 요구해 `deepagents`(langchain>=1.0)와 충돌합니다.  
로컬에서만 쓰려면 **2단계 설치** 후, 필요 시 가상환경 안의 패키지를 직접 수정하세요.

### 1) 2단계 설치

```powershell
# 프로젝트 루트에서
uv venv
.\scripts\install-local.ps1
```

또는 수동:

```powershell
uv pip install -r requirements.txt
```

### 2) 여전히 충돌이 나면: 가상환경 패키지 직접 수정

로컬 테스트용으로만, 설치된 `process-gpt-llm-factory`의 langchain 요구 조건을 바꿉니다.

- **METADATA 수정**  
  `.venv\Lib\site-packages\process_gpt_llm_factory-*.dist-info\METADATA`  
  - `Requires-Dist: langchain (>=0.2.0,<0.4.0)` 를  
  - `Requires-Dist: langchain (>=1.0.0)` 로 변경 (또는 해당 줄 삭제)

- **소스에서 버전 체크가 있다면**  
  `.venv\Lib\site-packages\process_gpt_llm_factory\` 아래에서  
  `langchain` 버전을 검사하는 코드가 있으면 해당 조건 완화/삭제

실제 SDK·llm-factory 수정은 나중에 레포에서 진행하면 됩니다.

## 로컬 실행 (uv)

```powershell
# .env 로드 후
$env:APP_ENV="local"
uv run python deepagents_action_server.py
```

## Docker

```bash
docker build -t process-gpt-deepagents-action:local .

docker run --rm -p 8000:8000 \
  -e SUPABASE_URL="https://xxx.supabase.co" \
  -e SUPABASE_KEY="eyJ..." \
  -e OPENAI_API_KEY="sk-..." \
  process-gpt-deepagents-action:local
```

## 런타임 입력/출력

- 입력: SDK가 전달하는 `RequestContext` (get_user_input, get_context_data → row, extras)
- 출력: A2A 이벤트 (TaskStatusUpdateEvent, TaskArtifactUpdateEvent) → SDK가 전달

## 커스터마이징

- **모델**: `extras.model` (예: `openai:gpt-4o`), 없으면 기본값 `openai:gpt-4o`
- **시스템 프롬프트**: `extras.system_prompt`로 오케스트레이터 프롬프트 오버라이드
- **에이전트**: Supabase `users` 테이블에 `is_agent=true`로 등록
  - `skills`: 쉼표 구분 스킬명 → API로 SKILL.md 자동 로딩
  - `tools`: 쉼표 구분 MCP 툴명 → 에이전트별 자동 필터링
  - `persona` + `goal`: 서브에이전트 시스템 프롬프트 자동 구성
  - `model`: 서브에이전트별 모델 오버라이드 가능
