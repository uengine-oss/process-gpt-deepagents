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
  - `database.py`, `execution_context.py`, `runtime_config.py`, `output_utils.py`, `health_server.py` — 인프라·유틸
  - `app/sandbox/` — `runtime`, `tools`, `output_delivery` (산출 파일은 선택적으로 Supabase **public** Storage `files` 버킷에 올려 `public_url`을 manifest/본문에 실음)
  - `app/storage_public.py` — 위 업로드·공개 URL 생성 (`SUPABASE_PUBLIC_FILES_BUCKET`, `DISABLE_SUPABASE_STORAGE_UPLOAD`)
  - `app/skills/` — 스킬 탐색·해결·볼륨 이벤트/미들웨어/백엔드 계측
  - `app/tooling/` — `TaggingSafeToolLoader`, `ConfigurableMementoTool`
  - `app/agent/` — DeepAgent `factory`, `hitl`, `backend`, `subagents`
  - `app/executor/` — A2A enqueue, HITL, 미들웨어, 도구 인스트루먼트 등 실행기 보조 모듈

## 환경변수

| 변수 | 설명 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase 서비스 롤 키 |
| `SKILLS_ROOT_DIR` | 스킬 파일 루트 경로 (예: `/app/skills`) |
| `LLM_MODEL` | 기본 LLM 모델명 (예: `anthropic:claude-opus-4-6`) |
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `LLM_EMBEDDING_MODEL` | (선택) 임베딩 모델명 |
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
- **프록시**: 사용하지 않습니다. (`LLM_PROXY_URL`이 설정돼 있어도 런타임에서 무시합니다.)
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
