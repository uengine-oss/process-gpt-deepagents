# ProcessGPT DeepAgents Action Server

ProcessGPT Agent SDK 폴링 서버 형태로, LangChain **Deep Agents (deepagents)** 를
Middleware-First 방식으로 사용합니다. 단일 DeepAgent 실행 안에서 planning(todo),
subagent 위임, 파일시스템 메모리, sandbox 실행, A2A 이벤트 로깅을 수행합니다.

## 아키텍처

```
ProcessGPT Agent SDK 폴링
       │
       ▼
DeepAgentsExecutor.execute(context, event_queue)
       │
       ├─ extras.agents (ID 목록) ──► Supabase users 테이블 조회 (app.database.get_supabase)
       │
       ├─ 런타임 라우팅 결정 ──► direct provider (Anthropic)
       ├─ 스킬/로컬 도구 로딩 ──► SafeToolLoader (선택적 로컬 도구)
       ├─ sandbox tools 주입 ──► sandbox_create/write/run/read/cleanup
       ├─ 단일 DeepAgent 실행 ──► TodoList + Filesystem + SubAgent middleware
       ▼
A2A 이벤트 발행 (TaskStatusUpdateEvent / TaskArtifactUpdateEvent)
```

## 구성

- **루트 엔트리**
  - `deepagents_server.py` — ProcessGPT Agent SDK 서버 (폴링)
  - `deepagents_executor.py` — A2A 래핑 멀티 에이전트 실행기 (`AgentExecutor`)
- **`app/`** — 애플리케이션 패키지
  - `database.py` (Supabase + **public** Storage 업로드·`public_url`), `execution_context.py`, `runtime_config.py`, `output_utils.py`, `health_server.py` — 인프라·유틸
  - `app/sandbox/` — `runtime`, `tools`, `output_delivery` (산출 파일은 선택적으로 Supabase **public** Storage `files` 버킷에 올려 `public_url`을 manifest/본문에 실음; `SUPABASE_PUBLIC_FILES_BUCKET`, `DISABLE_SUPABASE_STORAGE_UPLOAD`)
  - `app/skills/` — 스킬 탐색·해결·볼륨 이벤트/미들웨어/백엔드 계측
  - `app/tooling/` — `TaggingSafeToolLoader` (Memento는 `MEMENTO_RETRIEVE_URL` 설정 시에만 로드)
  - `app/agent/` — DeepAgent `factory`, `hitl`, `backend`, `subagents`
  - `app/executor/` — `deepagents_executor`가 조합하는 실행 보조 모듈(일부는 역할별로 한 파일에 묶여 있음)
    - `context.py` — `ExecutionContext`, `build_event_payload`, `_build_execution_evidence`(최종 래핑용 내부 증거)
    - `execution_state_validator.py` — 네이티브 실행 계약 검사, `strict_pipeline_state_violations`, `messages_to_audit_blob`
    - `tool_calls.py` — LangGraph `ToolCallRequest` 헬퍼 및 이벤트용 `_json_safe` 직렬화
    - `pipeline_runner.py` — 파이프라인 스텝 정렬/검증 및 단계별 TODO 빌더(`StepExecutorRegistry` / `default_step_registry`)
    - 그 외 `a2a_events`, `hitl_utils`, `todo_runner`, `pipeline_spec_middleware`, `asset_role_resolver` 등

## 환경변수

| 변수 | 설명 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase 서비스 롤 키 |
| `SUPABASE_PUBLIC_FILES_BUCKET` | (선택) public Storage 버킷명, 기본 `files` |
| `DISABLE_SUPABASE_STORAGE_UPLOAD` | (선택) `1`/`true`/`yes`이면 산출물 public 업로드 비활성 |
| `MEMENTO_RETRIEVE_URL` | (선택) Memento 검색 API 베이스 URL; 미설정 시 로컬 도구 로더는 Memento를 주입하지 않음 |
| `SKILLS_ROOT_DIR` | 스킬 파일 루트 경로 (예: `/app/skills`) |
| `LLM_MODEL` | 기본 LLM 모델명 (예: `anthropic:claude-opus-4-6`). 프록시 모드에서는 게이트웨이가 기대하는 `model` 문자열에 맞추세요(보통 `anthropic:` 뒤 접미사가 OpenAI 호환 요청에 전달됨). |
| `LLM_PROXY_URL` | (선택) OpenAI 호환 게이트웨이 베이스 URL(`ChatOpenAI`의 `base_url` 그대로). 경로는 게이트웨이 문서대로. **URL만 있고 프록시 키가 없으면** 직접 Anthropic으로 폴백(`app/runtime_config.py`). |
| `LLM_PROXY_API_KEY` | (선택) 프록시용 키. 없으면 `OPENAI_API_KEY`를 동일 용도로 사용. |
| `ANTHROPIC_API_KEY` | 직접 Anthropic 호출(프록시 미사용·위 폴백 시). |
| `OPENAI_API_KEY` | 프록시 키 대체(`LLM_PROXY_API_KEY` 없을 때). Mem0 등 로드되는 도구/SDK가 임베딩 등에 동일 변수를 읽는 경우 있음. |
| `SANDBOX_BASE_DIR` | sandbox 워크스페이스 루트 경로 |

## 설치

```bash
uv venv
uv pip install -r requirements.txt
```

## 로컬 실행 (uv)

```powershell
# .env 로드 후
$env:APP_ENV="local"
uv run python deepagents_server.py
```

## Docker

```bash
docker build -t process-gpt-deepagents-action:local .

docker run --rm -p 8888:8888 \
  -e SUPABASE_URL="https://xxx.supabase.co" \
  -e SUPABASE_KEY="eyJ..." \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  process-gpt-deepagents-action:local
```

## 런타임 입력/출력

- 입력: SDK가 전달하는 `RequestContext` (get_user_input, get_context_data → row, extras)
- 출력: A2A 이벤트 (TaskStatusUpdateEvent, TaskArtifactUpdateEvent) → SDK가 전달

## 커스터마이징

- **모델**: `extras.model` 우선, 없으면 `LLM_MODEL` 사용(기본 `anthropic:claude-opus-4-6`)
- **프록시(OpenAI 호환 게이트웨이)**: `LLM_MODEL`·`LLM_PROXY_URL`·`LLM_PROXY_API_KEY` 다음에 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`를 두는 것을 권장(`.env.example`과 동일). `LLM_PROXY_URL` + (`LLM_PROXY_API_KEY` 또는 `OPENAI_API_KEY`)가 있으면 `langchain_openai.ChatOpenAI`로 붙고, **URL만 있고 둘 다 없으면** `resolve_model_routing`이 직접 Anthropic으로 폴백합니다. `LLM_PROXY_URL`은 가공하지 않습니다. Supabase `users.model`로 서브에이전트 모델만 바꿀 수 있으며, 프록시 경로에선 게이트웨이가 이해하는 `model` id여야 합니다.
- **시스템 프롬프트**: `extras.system_prompt`로 오케스트레이터 프롬프트 오버라이드
- **에이전트**: Supabase `users` 테이블에 `is_agent=true`로 등록
  - `skills`: 쉼표 구분 스킬명 → 파일시스템 경로에서 SKILL.md 로딩
  - `tools`: 쉼표 구분 도구명 → 에이전트별 자동 필터링 + sandbox 도구 결합
  - `persona` + `goal`: 서브에이전트 시스템 프롬프트 자동 구성
  - `model`: 서브에이전트별 모델 오버라이드 가능

## 테스트

```bash
python3 -m pytest tests -q
```

핵심 검증 범위:
- LLM 라우팅 결정
- sandbox 런타임/도구 동작
- factory의 단일 runtime agent 생성
- executor 이벤트 payload 기본 구조

## 통합 검증 시나리오

### 1) Direct provider(Anthropic) 기본 시나리오

사전 조건:
- `ANTHROPIC_API_KEY` 설정
- `LLM_MODEL=anthropic:claude-opus-4-6` (또는 `extras.model`로 오버라이드)

검증:
1. 서버 실행: `uv run python deepagents_server.py`
2. 실제 작업 1건 실행(멀티스텝 요청 권장)
3. 로그에서 `mode=direct` 확인
4. 결과 이벤트에서 `task_started` -> `task_completed` -> 최종 artifact 순서 확인

합격 기준:
- 모델 호출이 실패 없이 완료
- 최종 결과 payload가 `output.status`/`output.result` 규격 만족

### 3) Todo + Subagent + Sandbox 시나리오

사전 조건:
- 대상 subagent에 sandbox 도구 사용 가능한 task 부여
- sandbox는 항상 활성화됨

검증:
1. 작업 입력을 코드 생성/실행형으로 구성
2. subagent가 sandbox 생성 -> 파일 작성 -> 쉘 실행 -> artifact 수집 흐름 수행
3. 실행 이벤트/요약에 subagent 도구 사용 흔적 확인

합격 기준:
- sandbox 명령 exit code 0
- artifact 경로가 반환됨
- 최종 응답에 실행 결과 반영

### 4) HITL 인터럽트/재개 시나리오

사전 조건:
- 승인/누락입력이 필요한 작업

검증:
1. 초기 실행 시 `human_asked` 이벤트 발행 확인
2. `human_response_answer` 포함하여 재실행
3. 이어서 완료까지 진행되는지 확인

합격 기준:
- interrupt -> resume -> complete 상태 전이 정상
- run_state(correlation_id, tool context) 유지

### 5) 회귀 시나리오 (이벤트 스키마 호환)

검증:
1. 기존 컨슈머가 사용하는 필수 필드 점검:
   - `metadata.event_type`, `metadata.job_id`
   - payload의 `logical_event_type`, `phase`
2. 최종 artifact 이벤트 정상 수신 확인

합격 기준:
- 기존 대시보드/컨슈머 파싱 오류 없음
