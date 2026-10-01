# process-gpt-deepagents

Process-GPT 의 딥 에이전트 실행체. 사용자의 요청을 받아 **샌드박스 컨테이너 안에서**
도구를 돌려 프로세스 정의·스킬·문서 같은 산출물을 만든다. 일은 두 군데로 들어온다.

| 입구 | 무엇 | 끄는 법 |
|---|---|---|
| `POST /chat/stream` | 화면의 채팅방에서 직접 — 토큰·도구·산출물을 SSE 로 돌려준다 | — |
| todolist 폴링 | 프로세스 워크아이템. SDK 가 큐를 긁어 같은 실행체에 넘긴다 | `DEEPAGENTS_PROCESS_POLLING=false` |

## 시작 방법

### 필요한 것

| | 왜 |
|---|---|
| Python 3.13 (`uv` 권장) | `.python-version` · `uv.lock` 기준 |
| **Docker 데몬** | 에이전트의 파일 작업과 코드 실행은 전부 샌드박스 컨테이너에서 돈다. 없으면 도구가 통째로 멎는다 |
| Supabase | 대화 이력(`chats`) · 워크아이템 · 스킬 등록. 로컬 인스턴스든 원격이든 상관없다 |
| LLM | `LLM_PROXY_URL`+`LLM_PROXY_API_KEY` 를 쓰거나, `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` 를 직접 준다. 모델 이름은 `LLM_MODEL` |
| memento (`:8005`) | **산출물 보관소.** 문서·이미지는 여기를 거쳐 비공개 버킷에 들어가고 서명 주소로 나간다. 없으면 파일을 만들어도 받을 길이 없다 |

### 1. 설치

```bash
uv sync                 # 또는: uv venv && uv pip install -r requirements.txt
cp .env.example .env    # 아래 값을 채운다
```

`.env` 에서 최소한 이 다섯 가지는 채워야 돈다 — `SUPABASE_URL` · `SUPABASE_KEY` ·
`LLM_MODEL` · LLM 자격증명(프록시 또는 API 키) · `MEMENTO_BASE_URL`.
나머지 항목의 뜻은 `.env.example` 에 주석으로 적혀 있다.

### 2. 띄우기

```bash
uv run python server.py
```

```
서버 시작: http://0.0.0.0:8888/chat/stream (todolist 폴링 켬)
```

포트는 `HOST`/`PORT` 로 바꾼다(기본 `0.0.0.0:8888`). 살아 있는지는 `/health` 로 본다 —
응답의 `busy` 는 세션 라우터가 유휴 Pod 를 회수해도 되는지 판정하는 근거다.

```bash
curl -s localhost:8888/health
```

### 3. 화면에서 써 보기

`process-gpt-vue3` 를 `npm run dev` 로 띄우면(`:8088`) `/process-gpt-deepagents/**` 를
`127.0.0.1:8888` 로 프록시한다. 홈(`/definition-map`) 하단 채팅에서 에이전트를
**딥 에이전트**로 바꾸고 보내면 이 서버로 들어온다.

### 4. 샌드박스는 어디에 무엇을 두는가

에이전트가 만지는 파일은 호스트의 두 디렉터리가 컨테이너로 마운트된 것이다.

| 호스트 | 컨테이너 | |
|---|---|---|
| `workspace/` | `/workspace` (rw) | 방마다 `.bpmn/<room>/` 아래로 격리된다. 산출물이 여기 생긴다 |
| `skills/` | `/skills/<스킬>` (ro) | 스킬별로 따로 마운트한다 |

컨테이너 이름은 테넌트당 하나(`deepagent-sandbox-<tenant>`)이고 이미지는
`SANDBOX_IMAGE`(기본 `python:3.13-slim`)다. 전용 브리지 네트워크를 쓰는 이유는
`.env.example` 의 MTU 주석에 적어 뒀다 — 기본 브리지로는 GKE 등에서 `git clone` 이
매달리다 죽는다.

### 테스트

```bash
uv run pytest -q
```

`GITHUB_TOKEN` 이 없으면 스킬 저장소 API 테스트(`tests/test_github_skill_repo_api.py`
등)는 실패한다 — 그 밖의 실패는 진짜 실패다.

### 자주 막히는 자리

| 증상 | 원인 |
|---|---|
| 문서를 만들었다는데 받을 수 없다 | memento 가 안 떠 있거나 `MEMENTO_BASE_URL` 이 틀렸다 |
| `save-to-storage` 가 500, 로그에 `Bucket not found` | 산출물이 들어갈 **비공개 `artifacts` 버킷**이 없다. Supabase 에 만든다 |
| 하루 지난 방에서 받기를 누르면 404 | 산출물 카탈로그 등록이 실패해 `/artifact-url` 재발급이 막힌 것. memento 쪽 `knowledge_files.path` 컬럼이 있는지 본다 |
| 도구가 하나도 안 돌고 멎는다 | Docker 데몬이 없거나 권한이 없다 |
| 채팅은 되는데 워크아이템을 안 집는다 | `DEEPAGENTS_PROCESS_POLLING` 이 꺼져 있다 |

## 채팅 인프라는 SDK 가 소유한다

`/chat/stream`, `/chat/stream/attach`, `/chat/stop`, `/chat/steer`, `/health` 는 모두
`process-gpt-agent-sdk` 가 마운트한다(`server.py` 의 `mount_chat_routes`).
이 저장소에 남은 채팅 관련 코드는 에이전트 로직뿐이다.

| 이 저장소가 갖는 것 | 무엇 |
|---|---|
| `DeepAgentExecutor.should_supersede()` | 새 turn 이 이전 turn 을 대체하는지 이어가는지(HITL) |
| `DeepAgentExecutor.steer()` | 수정 지시를 지금 받을 수 있는지 판정한다(사람 확인 대기 중이면 거절) |
| `core/agents/steering.py` | 돌고 있는 실행의 다음 모델 판단에 그 지시를 얹는다(아래) |
| `core/chat/event_tap.py` | 이벤트를 흘려보내며 채팅방 컨텍스트로 증분 저장 |
| `core/chat/chat_room.py`, `hitl.py`, `activity.py` 등 | 채팅방 컨텍스트·HITL·활동 기록 |

## 수정 지시(스티어링)는 진행 중인 실행에 얹는다

작업 중에 사용자가 방향을 바꾸면, 실행을 취소하거나 다시 시작하지 않고 **돌고 있는
실행의 다음 모델 판단에** 그 지시를 얹는다. 요청 형태와 이벤트(`steer_accepted` /
`steer_applied`)는 SDK 가 소유하고, 이 저장소는 **언제 얹을지**를 정한다
(`core/agents/steering.py` 의 `SteeringMiddleware`).

얹는 자리는 `abefore_model` — 모델 노드 바로 앞이다. 도구가 도는 동안에는 이 훅이
불리지 않으므로 **도구를 중간에 끊지 않고**, 도구 호출과 결과가 반드시 짝지어진
지점이라 정합성을 따로 지킬 필요가 없다(짝이 깨지면 모델 API 자체가 그 대화를
거절한다). 반영이 늦어지는 만큼이 곧 남은 도구 실행 시간이다.

모델이 도구를 부르지 않고 답을 내놓으면 그 턴은 끝나고, 그 뒤로는 훅이 다시 불리지
않는다. 그래서 `aafter_model` 에서 한 번 더 확인해 남은 지시가 있으면 모델 노드로
되돌리고(`jump_to`), 없으면 그때 대기열을 닫는다 — 닫은 뒤의 접수 요청은 SDK 가
거절한다. **`@hook_config(can_jump_to=["model"])` 가 없으면 langchain 이 `jump_to` 를
조용히 무시한다** — 지시를 집어 가 놓고 반영은 안 되는 제일 나쁜 결과가 된다.

사람 확인(`request_human_input`)으로 멈춰 있는 방은 돌고 있는 실행이 없어 얹을 곳이
없다. `DeepAgentExecutor.steer()` 가 `awaiting_human_input` 으로 거절하고, 사용자는 그
질문에 답하는 기존 경로로 방향을 바꾼다.

미들웨어는 루트에만 붙는다. 하위 에이전트가 도는 동안 접수된 지시는 제어가 루트로
돌아온 뒤 반영된다 — 늦어질 뿐 유실되지 않는다.

## 운영 전제: 한 대화 = 한 파드

진행 중인 turn 의 상태(SSE 구독자, 실행 중인 task, 워크스페이스)는 프로세스
메모리에 있다. `asyncio.Task.cancel()` 은 그 task 를 만든 프로세스 안에서만
가능하므로, **같은 `conversation_id` 의 요청은 항상 같은 파드로 가야 한다.**

`process-gpt-session-router` 가 그 전제를 만든다 — `conversation_id` 로 세션 Pod 를
찾거나 만들고, 유휴 TTL 이 지나면 회수한다. 회수 후 다시 찾아온 대화는 새 Pod 에서
Supabase(대화 이력)와 Supabase Storage(워크스페이스)로부터 복원된다.

> 라우터 없이 여러 레플리카로 띄우면 재접속과 중지가 조용히 깨진다 — 요청이 그 turn 을
> 들고 있지 않은 파드에 떨어지면 "활성 스트림 없음"으로 응답한다. 예전에는 이를 위해
> Redis 소유권 키와 pub/sub 취소 신호, Redis Streams 팬아웃을 뒀지만, 라우팅이 그
> 계층을 대신한다.

### 채팅이 아닌 경로

`/skills/*` 와 `/api/internal/workspace-file` 은 세션에 매이지 않는다. 게이트웨이에서 이 경로들은
라우터를 거치지 않고 기존 Deployment 로 보낸다.

에이전트 **산출물**은 이 경로로 내주지 않는다. 채팅이든 폼(worklist)이든 SDK 의 공용
규약을 타고 비공개 버킷에 보관되며, 주소는 한 시간짜리 서명 주소다
(`core/chat/artifacts.py`). 예전에는 파일 내용을 이 서버(또는 Redis)에 담고 토큰 주소를
내줬는데, 그 시간이 지나거나 파드가 바뀌면 파일을 영영 받을 수 없었다.
