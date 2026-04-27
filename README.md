# ProcessGPT DeepAgents Server

ProcessGPT 환경에서 LangChain **Deep Agents (deepagents)** 를 Middleware-First로 돌리는 서버입니다. 단일 실행 그래프 안에서 planning(todo), 서브에이전트 위임, 스킬·MCP 도구, 샌드박스 실행 등을 묶어 처리합니다.

동일한 `DeepAgentsExecutor` 위에 **두 가지 진입 방식**이 있습니다.

| 모드 | 진입점 | 용도 |
|------|--------|------|
| **프로세스 모드** | `deepagents_server.py` → ProcessGPT Agent SDK **폴링** | 할 일(todolist) 행이 들어오면 실행하고 **A2A 이벤트**를 SDK/DB로 전달 |
| **채팅 모드** | 같은 프로세스의 **HTTP API** (`app/http_server.py` → `app/http_chat.py`) | 대화형 요청을 **JSON 응답** 또는 **SSE 스트리밍**으로 처리(선택적으로 Supabase에 채팅 메시지 반영) |

## 아키텍처

```
                    ┌─────────────────────────────────────┐
                    │         DeepAgentsExecutor          │
                    │  (공통: 모델 라우팅, MCP/스킬/샌드박스, │
                    │   미들웨어 체인, HITL 체크포인트 등)   │
                    └─────────────────────────────────────┘
                                        ▲
              ┌─────────────────────────┴─────────────────────────┐
              │                                                   │
   ProcessGPTRequestContext                              SimpleChatContext
   (SDK가 준비한 row + extras)                    (row.tool=http_chat, extras.http_chat=True)
              │                                                   │
              ▼                                                   ▼
   ProcessGPTAgentServer 폴링                    FastAPI: /health, /chat, /chat/stream
   (deepagents_server.py)                              (포트 8888, uvicorn 스레드)
              │                                                   │
              ▼                                                   ▼
   ProcessGPTEventQueue → DB 이벤트                  NoopEventQueue (A2A 큐 없음)
   TaskStatus / Artifact …                           토큰·도구 이벤트 → SSE 또는 JSON 응답
```

- **프로세스 모드**: SDK가 `polling_pending_todos` 등으로 작업 행을 넘기면 `DeepAgentsServer.process_todolist_item` → `executor.execute(context, event_queue)`로 이어지고, 결과는 기존 ProcessGPT 파이프라인이 기대하는 이벤트 스키마로 나갑니다.
- **채팅 모드**: `extras["http_chat"]=True` 로 분기되어 라우팅 게이트(`write_http_chat_route` 등), 루트 도구 축소(work-assistant 위주), **스트리밍 시** 토큰/도구 이벤트를 콜백으로 넘겨 SSE에 실습니다. HITL이 필요하면 `status=input_required`와 `run_state`를 돌려주고, 재요청 시 `metadata`에 답변·`run_state`를 넣어 이어갈 수 있습니다.

## 구성

- **루트 엔트리**
  - `deepagents_server.py` — SDK 폴링 서버 + `start_http_server()`로 HTTP API 동시 기동
  - `deepagents_executor.py` — 멀티 에이전트 실행기 (`AgentExecutor`)
- **`app/`**
  - `http_server.py`, `http_chat.py` — 채팅 REST/SSE (`ChatRequest` / `ChatResponse`)
  - `database.py`, `execution_context.py`, `runtime_config.py`, `output_utils.py`, `health_server.py`
  - `app/sandbox/` — 런타임·도구·산출물 전달(선택적 Supabase public Storage)
  - `app/skills/` — 스킬 탐색·해결·볼륨
  - `app/tooling/` — `TaggingSafeToolLoader` (Memento는 `MEMENTO_RETRIEVE_URL` 설정 시)
  - `app/agent/` — DeepAgent `factory`, `hitl`, `backend`, `subagents`
  - `app/executor/` — A2A 이벤트, 파이프라인, HITL, 토큰 하이진/툴 결과 프로젝션 등 실행 보조 모듈

## 채팅 모드 사용 (요약)

- **엔드포인트**
  - `GET /health` — 헬스체크
  - `POST /chat` — 비스트리밍; `ChatResponse` JSON
  - `POST /chat/stream` — **SSE**(`EventSourceResponse`); `token` / `tool_*` / `run_meta` / `done` 등 이벤트
- **필수**: `tenant_id`, 사용자 메시지 `message`. 테넌트에 `is_agent=true` 에이전트가 없으면 `agent_ids`로 지정해야 합니다.
- **JWT**: 바디 `user_jwt` 또는 `Authorization: Bearer <jwt>` (MCP/업무 도구 연동에 사용).
- **HITL 재개**: 이전 응답의 `run_state`·`human_response_answer` 또는 `ask_user_response` / `openui_hitl_response`를 `metadata`에 넣어 다시 호출합니다.

### 예시: 비스트리밍 `/chat`

```bash
curl -sS -X POST "http://localhost:8888/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "YOUR_TENANT_ID",
    "message": "우리 테넌트의 프로세스 목록을 간단히 요약해 줘.",
    "user_email": "user@example.com",
    "user_uid": "user-uid-optional",
    "metadata": { "model": "anthropic/claude-sonnet-4-20250514" }
  }' | jq .
```

### 예시: 스트리밍 `/chat/stream` (SSE)

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

클라이언트는 `text/event-stream` 줄 단위로 `data: {json}` 형태의 이벤트를 읽으면 됩니다.

## 프로세스 모드 사용 (요약)

1. `deepagents_server.py` 실행 후 SDK가 DB에서 pending 할 일을 가져옵니다.
2. `ProcessGPTRequestContext.prepare_context()` 뒤 `DeepAgentsExecutor.execute`가 호출됩니다.
3. 결과는 **A2A 이벤트**(상태 업데이트·아티팩트 등)로 기록되며, HITL이면 `waiting_for_human` + `run_state` 저장 후 재개 폴링 경로로 이어질 수 있습니다.

로컬에서 프로세스 플로만 빠르게 보려면 기존처럼 서버를 띄운 뒤 SDK/대시보드 쪽에서 할 일을 enqueue 하는 방식이 일반적입니다.

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

- **프로세스 모드**: SDK `RequestContext` (get_user_input, get_context_data → row, extras) → A2A 이벤트 → SDK/DB
- **채팅 모드**: `ChatRequest` JSON → `ChatResponse` 또는 SSE 이벤트 스트림; (설정 시) Supabase 채팅 테이블에 메시지 upsert

## 커스터마이징

- **모델**: 채팅은 `metadata.model`, 프로세스는 `extras.model` 우선, 없으면 `LLM_MODEL` 사용(기본 `anthropic:claude-opus-4-6`)
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

### 6) HTTP 채팅(선택)

검증:
1. `GET /health` → `{"status":"ok"}`
2. `POST /chat` 또는 `/chat/stream`으로 테넌트·메시지 전송
3. (스트리밍) `token` 이벤트 후 `done` 또는 HITL 시 `input_required` 이벤트 확인

합격 기준:
- 400/500 없이 응답 또는 스트림 완료
- HITL인 경우 `run_state`를 메타데이터에 넣어 재호출 시 이어짐
