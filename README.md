# process-gpt-deepagents

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
