# process-gpt-deepagents

## 채팅 인프라는 SDK 가 소유한다

`/chat/stream`, `/chat/stream/attach`, `/chat/stop`, `/health` 는 모두
`process-gpt-agent-sdk` 가 마운트한다(`server.py` 의 `mount_chat_routes`).
이 저장소에 남은 채팅 관련 코드는 에이전트 로직뿐이다.

| 이 저장소가 갖는 것 | 무엇 |
|---|---|
| `DeepAgentExecutor.should_supersede()` | 새 turn 이 이전 turn 을 대체하는지 이어가는지(HITL) |
| `core/chat/event_tap.py` | 이벤트를 흘려보내며 채팅방 컨텍스트로 증분 저장 |
| `core/chat/chat_room.py`, `hitl.py`, `activity.py` 등 | 채팅방 컨텍스트·HITL·활동 기록 |

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
