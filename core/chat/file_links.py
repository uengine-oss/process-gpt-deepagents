"""
채팅 답변에 남은 샌드박스 파일 경로를 내려받을 수 있는 링크로 바꾼다.

무엇이 잘못돼 있었나
    스킬이 만든 문서는 도커 샌드박스 안에만 있고, 채팅 답변에는 컨테이너 경로만
    실렸다.

        다운로드할 문서를 만들었습니다.
        파일 경로: /workspace/.bpmn/<방>/Team Overview.docx

    사용자에게 이 경로는 아무 뜻이 없다. **만들어졌다는 말만 있고 받을 길이
    없다.** 폼(worklist) 산출물은 이미 실체화하고 있었지만(`if form_id:`)
    채팅은 그 갈래를 타지 않아 경로가 그대로 남았다.

무엇으로 바꾸는가
    경로 자리에 마크다운 링크를 넣는다. 웹과 앱 모두 답변을 마크다운으로
    그리므로, 링크 하나면 양쪽에서 그대로 눌러 받을 수 있다 — 화면 코드는
    손대지 않는다.

이 모듈은 샌드박스도 토큰 발급기도 알지 못한다. 경로를 주소로 바꿔 주는 함수를
받아서 쓴다. 그래야 이 규칙만 따로 시험할 수 있다.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

# 경로가 끝났다고 볼 글자들.
#
# 파일 이름에는 공백이 들어간다("Team Overview.docx"). 공백에서 끊으면 경로의
# 앞토막만 잡아 링크가 깨진다 — 실제로 그랬다. 그래서 공백은 허용하고, 줄바꿈과
# 따옴표·괄호처럼 글 속에서 경로가 끝났다고 볼 만한 것에서만 끊는다.
_PATH_STOP = "\r\n\"'`)]"

# 채팅 답변에서 잡아낼 문서 경로.
#
# hwpx 는 일부러 뺀다. 한글 문서는 미리보기·편집이 되는 별도 경로가 이미 맡고
# 있어서, 여기서도 링크로 바꾸면 같은 파일이 두 번 나온다.
#
# 뒤에 글자·숫자·점이 이어지면 아직 끝이 아니다(`a.docx.docx`).
WS_DOCUMENT_RE = re.compile(
    r"/workspace/[^" + re.escape(_PATH_STOP) + r"]*?\.(?:pdf|docx?|xlsx?|pptx?)(?![.\w])",
    re.IGNORECASE,
)


def find_document_paths(text: Optional[str]) -> list[str]:
    """답변에 나온 문서 경로들. 나온 순서를 지키고 중복은 없앤다."""
    if not text:
        return []
    return list(dict.fromkeys(WS_DOCUMENT_RE.findall(text)))


def file_name_of(path: str) -> str:
    """경로 끝의 파일 이름. 링크에 보일 글자다."""
    return str(path).rstrip("/").rsplit("/", 1)[-1] or "첨부파일"


def apply_links(text: str, links: dict) -> str:
    """
    경로를 링크로 바꾼다.

    긴 경로부터 바꾼다. 짧은 경로가 긴 경로의 일부일 때 순서가 어긋나면 이미
    만든 링크 안쪽이 다시 치환되어 깨진다.
    """
    out = text
    for path in sorted(links, key=len, reverse=True):
        out = out.replace(path, links[path])
    return out


async def materialize(
    text: Optional[str],
    resolve: Callable[[str], Awaitable[Optional[str]]],
) -> str:
    """
    답변 속 문서 경로를 내려받기 링크로 바꾼 답변을 돌려준다.

    Args:
        text:    에이전트의 최종 답변
        resolve: 컨테이너 경로를 받아 내려받기 주소를 돌려주는 함수.
                 못 만들면 None — 그 경로는 손대지 않는다.

    지어낸 주소로 링크를 만들지 않는다. 눌렀는데 아무것도 안 나오는 것이
    경로가 그대로 보이는 것보다 나쁘다.
    """
    if not text:
        return text or ""

    paths = find_document_paths(text)
    if not paths:
        return text

    links: dict = {}
    for path in paths:
        url = await resolve(path)
        if not url:
            continue
        links[path] = f"[{file_name_of(path)}]({url})"

    return apply_links(text, links) if links else text
