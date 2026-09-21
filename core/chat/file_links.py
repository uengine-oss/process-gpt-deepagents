"""채팅 답변에 나온 샌드박스 산출물 경로를 찾는다.

에이전트가 만든 문서는 도커 샌드박스 안에만 있고, 답변에는 컨테이너 경로만 실린다.

    다운로드할 문서를 만들었습니다.
    파일 경로: /workspace/.bpmn/<방>/Team Overview.docx

사용자에게 이 경로는 아무 뜻이 없다. **만들어졌다는 말만 있고 받을 길이 없다.**

이 모듈이 아는 것은 여기까지다 — *어디를 봐야 산출물이 있는가*. 그것을 꺼내 보관하고
주소를 발급하고 본문을 고쳐 쓰는 일은 SDK 가 모든 에이전트 공용으로 한다
(`processgpt_agent_sdk.artifacts`, `core/chat/artifacts.py`). 예전에는 그 일을 이 파일이
직접 했고, 한 시간짜리 토큰 주소라 다음 날에는 아무것도 받을 수 없었다.
"""

from __future__ import annotations

import re
from typing import Optional

from processgpt_agent_sdk.artifacts import DEFAULT_EXTENSIONS

# 경로가 끝났다고 볼 글자들.
#
# 파일 이름에는 공백이 들어간다("Team Overview.docx"). 공백에서 끊으면 경로의
# 앞토막만 잡아 링크가 깨진다 — 실제로 그랬다. 그래서 공백은 허용하고, 줄바꿈과
# 따옴표·괄호처럼 글 속에서 경로가 끝났다고 볼 만한 것에서만 끊는다.
_PATH_STOP = "\r\n\"'`)]"

# hwpx·hwp 는 일부러 뺀다. 한글 문서는 미리보기·편집이 되는 별도 경로가 이미 맡고
# 있어서, 여기서도 내려받기로 내면 같은 파일이 두 번 나온다.
_CHAT_ARTIFACT_EXTENSIONS = sorted(DEFAULT_EXTENSIONS - {"hwp", "hwpx"}, key=len, reverse=True)

# 채팅 답변에서 잡아낼 산출물 경로. 뒤에 글자·숫자·점이 이어지면 아직 끝이 아니다(`a.docx.docx`).
WS_DOCUMENT_RE = re.compile(
    r"/workspace/[^" + re.escape(_PATH_STOP) + r"]*?\.(?:"
    + "|".join(_CHAT_ARTIFACT_EXTENSIONS)
    + r")(?![.\w])",
    re.IGNORECASE,
)


def find_document_paths(text: Optional[str]) -> list[str]:
    """답변에 나온 산출물 경로들. 나온 순서를 지키고 중복은 없앤다."""
    if not text:
        return []
    return list(dict.fromkeys(WS_DOCUMENT_RE.findall(text)))


__all__ = ["WS_DOCUMENT_RE", "find_document_paths"]
