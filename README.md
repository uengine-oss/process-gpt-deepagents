# ProcessGPT DeepAgents Server

ProcessGPT 환경에서 LangChain **Deep Agents (deepagents)** 를 Middleware-First로 돌리는 서버입니다. 단일 그래프 안에서 planning(todo), 서브에이전트 위임(`task()`), 스킬·MCP·샌드박스, HITL 등을 묶어 처리합니다.

## 실행 흐름 (아키텍처)

`process-gpt-agent-sdk`가 `RequestContext`와 이벤트 싱크를 준비한 뒤, 아래 실행 계층으로 넘깁니다.

```
process-gpt-agent-sdk              DeepAgentsExecutor · 그래프 런타임
(RequestContext, EventQueue)                 │
        │                                      ▼
        └────────────────────────► DeepAgentsExecutor.execute()
                                              │
                                              ▼
                              build_runtime_agent() → 컴파일된 LangGraph 그래프
                              (deepagents + 미들웨어 + 서브에이전트 + 체크포인터)
```

## 딥 에이전트와 서브에이전트 구성

- **런타임 생성**: `app/agent/factory.py`의 `build_runtime_agent`가 `deepagents.create_deep_agent`로 **단일** 컴파일 그래프를 만듭니다. `create_deep_agent`가 이미 `TodoListMiddleware`, `FilesystemMiddleware`, `SubAgentMiddleware` 등을 넣으므로 **같은 미들웨어를 바깥에서 한 번 더 붙이면 중복 오류**가 납니다.
- **오케스트레이터 도구**: HITL용 `request_human_input` 등은 `get_hitl_tools()`로 묶이고, 테넌트/작업별 루트 MCP·로컬 도구·추가 미들웨어가 여기에 합쳐집니다. 백엔드는 `make_deepagent_backend_factory()` (`app/agent/backend.py`).
- **서브에이전트 정의**: `app/agent/subagents.py`가 Supabase `users` 중 `is_agent=true` 행을 읽어 `task()`에 넘길 스펙을 만듭니다. `skills`, `tools`, `persona`, `goal`, (선택) `model` 등이 시스템 프롬프트·도구 구성에 반영됩니다.
- **도구·MCP 티어(요지)**: 서브에이전트 기본 MCP 등은 모듈 상수·주석(`DEFAULT_MCP_SERVER_NAMES`, work-assistant는 루트 전용 등)에 정리되어 있습니다. 샌드박스는 `get_sandbox_tools()`와 연동됩니다.

모델/프록시/프롬프트 오버라이드는 아래 **커스터마이징**을 참고하세요.

## Human-in-the-loop (HITL)과 재개

역할을 **그래프 안**과 **실행기(오케스트레이션) 쪽**으로 나누면 다음과 같습니다.

1. **그래프 안** (`app/agent/hitl.py`): `request_human_input` 도구가 LangGraph `interrupt(...)`를 호출해 실행을 멈춥니다. 결과 dict에서 interrupt 페이로드를 꺼낼 때는 `extract_interrupt_payload()`를 씁니다.
2. **실행기** (`deepagents_executor.py`, `app/executor/*`): `human_asked` 등 이벤트 발행, `run_state` 조립, 재개 시 `human_response_answer`와의 매칭이 여기서 이뤄집니다. Postgres(`DB_*`)가 설정되면 **비동기 LangGraph 체크포인터**로 스레드 상태를 유지하고, `run_state` + 사용자 답(채팅은 보통 `metadata`로 보낸 값이 SDK를 거쳐 컨텍스트에 실림, todolist는 DB의 최신 human 응답 로드 등)이 맞으면 **그래프 resume** 경로를 탑니다.
3. **todolist 경로** (`deepagents_server.py`, `app/database.py`): 인터럽트로 멈추면 `waiting_for_human`과 `run_state`를 `set_task_waiting_hitl` 등으로 남기고, 이후 **`claim_resumable_hitl_task`** 보조 폴링이 응답이 채워진 행을 다시 집어 같은 `execute()`로 넣습니다.
4. **채팅 HTTP 경로**: 동일 실행기이나, 클라이언트는 이전 응답의 `run_state`와 사용자 답을 **`metadata`에 실어 다음 요청**으로 재개합니다(필드명은 SDK 스키마를 따릅니다).

## 구성

- **루트 엔트리**
  - `deepagents_server.py` — `main()`에서 `DeepAgentsServer.run()`과 Uvicorn(기본 **8888**, `mount_chat_sse`)을 `asyncio.gather`로 함께 기동. 포트가 이미 쓰이면 HTTP만 생략하고 폴링은 유지
  - `deepagents_executor.py` — `AgentExecutor` 구현체
- **`app/`**
  - `database.py`, `execution_context.py`, `runtime_config.py`, `output_utils.py`, `health_server.py`
  - `app/sandbox/` — 런타임·도구·산출물 전달(선택적 Supabase public Storage)
  - `app/skills/` — 스킬 탐색·해결·볼륨
  - `app/tooling/` — `TaggingSafeToolLoader`
  - `app/agent/` — `factory`, `hitl`, `backend`, `subagents`
  - `app/executor/` — A2A 이벤트, 파이프라인, HITL 연동, 토큰/툴 결과 처리 등

## SDK 연동·HTTP (참고)

바디 스키마·SSE 이벤트 이름 등 **세부 계약은 SDK 문서**가 정본입니다. 로컬에서 자주 쓰는 것만 적습니다.

- `GET /health` — 헬스체크
- `POST /chat/stream` — SSE(`mount_chat_sse`). `tenant_id`, `message` 필수. JWT는 바디 `user_jwt` 또는 `Authorization: Bearer`. 테넌트에 에이전트가 없으면 SDK 스키마대로 `agent_ids` 등을 지정
- HITL 후 재호출: `run_state`, `human_response_answer` 또는 `ask_user_response` / `openui_hitl_response`를 `metadata`에 포함
- 프로세스 할 일: SDK가 row를 넘기면 `ProcessGPTRequestContext.prepare_context()` 이후 동일 `execute()`

### 예시: `/chat/stream` (curl)

```bash
curl -N -sS -X POST "http://localhost:8888/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "tenant_id": "YOUR_TENANT_ID",
    "message": "할 일 목록 조회에 필요한 정보만 단계적으로 알려줘.",
    "conversation_id": "optional-stable-room-id",
    "user_jwt": "OPTIONAL_JWT_FOR_TOOLS"
  }'
```

클라이언트는 `text/event-stream`의 `event: metadata` / `event: message` 등을 읽으면 됩니다(페이로드는 JSON).

## 환경변수

| 변수 | 설명 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase 서비스 롤 키 |
| `SUPABASE_PUBLIC_FILES_BUCKET` | (선택) public Storage 버킷명, 기본 `files` |
| `DISABLE_SUPABASE_STORAGE_UPLOAD` | (선택) `1`/`true`/`yes`이면 산출물 public 업로드 비활성 |
| `SKILLS_ROOT_DIR` | 스킬 파일 루트 경로 (예: `/app/skills`) |
| `LLM_MODEL` | 기본 LLM 모델명 (예: `anthropic:claude-opus-4-6`). 프록시 모드에서는 게이트웨이가 기대하는 `model` 문자열에 맞추세요(보통 `anthropic:` 뒤 접미사가 OpenAI 호환 요청에 전달됨). |
| `LLM_PROXY_URL` | (선택) OpenAI 호환 게이트웨이 베이스 URL(`ChatOpenAI`의 `base_url` 그대로). 경로는 게이트웨이 문서대로. **URL만 있고 프록시 키가 없으면** 직접 Anthropic으로 폴백(`app/runtime_config.py`). |
| `LLM_PROXY_API_KEY` | (선택) 프록시용 키. 없으면 `OPENAI_API_KEY`를 동일 용도로 사용. |
| `ANTHROPIC_API_KEY` | 직접 Anthropic 호출(프록시 미사용·위 폴백 시). |
| `OPENAI_API_KEY` | 프록시 키 대체(`LLM_PROXY_API_KEY` 없을 때). Mem0 등 로드되는 도구/SDK가 임베딩 등에 동일 변수를 읽는 경우 있음. |
| `SANDBOX_BASE_DIR` | sandbox 워크스페이스 루트 경로 |
| `DEEPAGENTS_FILE_LOGGING` | (선택) `on`이면 DB 이벤트 payload 파일 로그 훅 |
| `DB_USER` | Postgres 사용자 (`processgpt_agent_utils`·LangGraph 체크포인트 공용 DSN) |
| `DB_PASSWORD` | Postgres 비밀번호 |
| `DB_HOST` | Postgres 호스트 |
| `DB_PORT` | Postgres 포트(기본 `5432`) |
| `DB_NAME` | Postgres DB 이름 |

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

기동 시 **8888** 포트에 HTTP 채팅 API가 함께 올라갑니다(이미 사용 중이면 HTTP만 스킵되고 폴링은 계속됩니다).

## Docker

```bash
docker build -t process-gpt-deepagents:local .

docker run --rm -p 8888:8888 \
  -e SUPABASE_URL="https://xxx.supabase.co" \
  -e SUPABASE_KEY="eyJ..." \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  process-gpt-deepagents:local
```

## 런타임 입력/출력

입력은 SDK가 만든 **`RequestContext`**(row·extras·메타데이터)이고, 출력은 **`DeepAgentsExecutor`**가 이벤트 큐/SSE 쪽으로 전달합니다. 채팅 JSON 필드명·SSE 프레임·프로세스 이벤트 스키마는 **SDK가 정의**하고, 그 위에서 그래프 실행과 HITL·체크포인트·A2A 페이로드 조립은 본 서버 코드가 담당합니다.

## 커스터마이징

- **모델**: 채팅은 `metadata.model`, 프로세스는 `extras.model` 우선, 없으면 `LLM_MODEL` 사용(기본 `anthropic:claude-opus-4-6`)
- **프록시(OpenAI 호환 게이트웨이)**: `LLM_MODEL`·`LLM_PROXY_URL`·`LLM_PROXY_API_KEY` 다음에 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`를 두는 것을 권장(`.env.example`과 동일). `LLM_PROXY_URL` + (`LLM_PROXY_API_KEY` 또는 `OPENAI_API_KEY`)가 있으면 `langchain_openai.ChatOpenAI`로 붙고, **URL만 있고 둘 다 없으면** `resolve_model_routing`이 직접 Anthropic으로 폴백합니다. `LLM_PROXY_URL`은 가공하지 않습니다. Supabase `users.model`로 서브에이전트 모델만 바꿀 수 있으며, 프록시 경로에선 게이트웨이가 이해하는 `model` id여야 합니다.
- **시스템 프롬프트**: `extras.system_prompt`로 오케스트레이터 프롬프트 오버라이드
- **서브에이전트·도구·스킬**: Supabase `users`(`is_agent=true`) 등은 위 **「딥 에이전트와 서브에이전트 구성」**절과 `app/agent/subagents.py`를 참고하세요.

## 테스트

```bash
python3 -m pytest tests -q
```

핵심 검증 범위:
- LLM 라우팅 결정
- sandbox 런타임/도구 동작
- factory의 단일 runtime agent 생성
- executor 이벤트 payload 기본 구조
- (선택) HITL·체크포인트 재개

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
- `DB_HOST`·`DB_NAME` 등 Postgres(`DB_*`) 설정 시 LangGraph 체크포인트로 재개 가능
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

### 6) SDK HTTP 채팅 SSE(선택)

검증:
1. `GET /health` → `{"status":"ok"}`
2. `POST /chat/stream`으로 테넌트·메시지 전송
3. (스트리밍) `token` 이벤트 후 `done` 또는 HITL 시 `input_required` 이벤트 확인

합격 기준:
- 400/500 없이 응답 또는 스트림 완료
- HITL인 경우 `run_state`를 메타데이터에 넣어 재호출 시 이어짐
