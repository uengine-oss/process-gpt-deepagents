"""채팅에서 에이전트가 만든 산출물을 사용자가 받을 수 있게 한다.

예전에는 샌드박스 파일을 이 서버 메모리(또는 Redis)에 담고 한 시간짜리 토큰 주소를
답변에 박았다. 그 한 시간이 지나면 끝이었다 — 토큰도 파일도 사라져, 어제 대화를 다시
연 사람은 "만들었습니다"라는 문장과 죽은 링크만 봤다. 방을 새로 고치기만 해도 마찬가지다.

지금은 보관도 주소 발급도 SDK 가 모든 에이전트 공용으로 한다
(`processgpt_agent_sdk.artifacts`). 산출물은 Memento 의 비공개 버킷에 들어가고, 답변에는
한 시간짜리 서명 주소가 실린다. 그 주소가 죽어도 파일은 남아 있고, 레코드에 함께 실린
`file_id` 로 화면이 새 주소를 받아 간다.

이 모듈이 하는 일은 그 사이를 잇는 것뿐이다 — 샌드박스에서 파일을 꺼내 턴의 `outputs/`
에 놓는다. 그 다음은 SDK 의 규약이다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Optional

from processgpt_agent_sdk.artifacts import (
    ArtifactCollector,
    ArtifactStore,
    MementoArtifactStore,
    strip_local_paths,
)

from core.chat.file_links import find_document_paths

logger = logging.getLogger(__name__)

# 샌드박스 경로 → 내용. 못 꺼내면 None.
Fetch = Callable[[str], Awaitable[Optional[bytes]]]


def _store() -> ArtifactStore:
    return MementoArtifactStore(os.getenv("MEMENTO_BASE_URL", "http://127.0.0.1:8005"))


async def publish_one(
    *,
    tenant_id: str,
    conversation_id: str,
    file_name: str,
    content: bytes,
    store: ArtifactStore | None = None,
    turn_id: str = "",
) -> dict[str, Any] | None:
    """파일 하나를 보관하고 산출물 레코드를 돌려준다. 못 내주면 None.

    채팅만 산출물을 내는 것이 아니다 — 폼(worklist)도 문서를 만든다. 거두는 자리가 달라도
    보관하고 주소를 발급하는 규칙은 같아야 하므로, 같은 `ArtifactCollector` 를 태운다.
    형식 제한과 크기 상한, 해시와 content-type 이 그 안에 있다.
    """
    with tempfile.TemporaryDirectory(prefix="artifact-") as tmp:
        outputs = Path(tmp) / "outputs"
        outputs.mkdir()
        await asyncio.to_thread((outputs / Path(file_name).name).write_bytes, content)
        files = await ArtifactCollector(store or _store()).collect(
            outputs,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
    return files[0] if files else None


async def publish_chat_artifacts(
    *,
    tenant_id: str,
    conversation_id: str,
    text: str,
    fetch: Fetch,
    store: ArtifactStore | None = None,
    turn_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """답변에 나온 산출물을 보관하고, 본문의 경로를 받을 수 있는 주소로 바꾼다.

    Returns:
        (바뀐 본문, 산출물 레코드 목록). 레코드는 `chats.messages.pdfFiles` 로 저장돼
        새로고침 뒤에도, 주소가 만료된 뒤에도 화면이 파일을 다시 집을 수 있게 한다.

    실패는 베스트에포트다. 하나를 못 올려도 나머지는 내준다 — 링크를 못 만들어도
    답변 자체는 그대로 보인다.
    """
    paths = find_document_paths(text)
    if not paths:
        return text, []

    with tempfile.TemporaryDirectory(prefix="chat-artifacts-") as tmp:
        outputs = Path(tmp) / "outputs"
        outputs.mkdir()
        for container_path in paths:
            try:
                content = await fetch(container_path)
            except Exception:
                logger.warning("채팅 산출물: 샌드박스에서 꺼내지 못했다 | path=%s", container_path, exc_info=True)
                continue
            if not content:
                # 지어낸 주소로 링크를 만들지 않는다. 눌렀는데 아무것도 안 나오는 것이
                # 경로가 그대로 보이는 것보다 나쁘다.
                logger.warning("채팅 산출물: 샌드박스에 파일 없음 | path=%s", container_path)
                continue
            name = PurePosixPath(container_path).name
            await asyncio.to_thread((outputs / name).write_bytes, content)

        collector = ArtifactCollector(store or _store())
        files = await collector.collect(
            outputs,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

    return strip_local_paths(text, files), files


__all__ = ["publish_chat_artifacts", "publish_one"]
