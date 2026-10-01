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
from pathlib import Path
from typing import Iterable, Optional

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


# 답변이 말해 주지 않아도 거둬야 하는 문서 산출물의 형식.
#
# 이 형식들은 바이너리라서 `write_file` 스트림(텍스트)으로 화면에 뜨지 않는다. 그래서 본문에
# 경로가 적히지 않으면 **만들어졌다는 말만 남고 받을 길이 없다** — 모델이 경로를 적는지에
# 산출물의 운명이 걸려 있었다. 그래서 글이 아니라 작업 폴더를 본다(SDK 의 규약과 같은 원리).
#
# hwp·hwpx 는 일부러 뺀다. 한글 문서는 미리보기·편집이 되는 별도 경로가 이미 맡고 있다.
BINARY_DOCUMENT_EXTENSIONS = frozenset({"doc", "docx", "pdf", "xlsx", "pptx"})

# 작업 폴더 스캔에서 뺄 경로 조각. 스킬 평가 실행은 회차마다 수십 개의 내부 작업 파일을
# 남기는데, 그것은 사용자 산출물이 아니다.
_SCAN_EXCLUDED_MARKERS = (".skill-evals/", "/.checkouts/")


def find_room_documents(
    room_dir: Path,
    base_container: str,
    *,
    since_ns: int = 0,
    extensions: Iterable[str] = BINARY_DOCUMENT_EXTENSIONS,
) -> list[str]:
    """방 작업 폴더에서 이번 턴에 생기거나 바뀐 문서 산출물의 컨테이너 경로.

    Args:
        room_dir:       방 작업 폴더(host). 컨테이너의 `base_container` 와 같은 곳이다.
        base_container: 그 폴더의 컨테이너 경로(`/workspace/.bpmn/<방>`).
        since_ns:       턴을 시작한 시각(`time.time_ns()`). 그 뒤에 바뀐 것만 본다 —
                        0 이면 지금 있는 문서를 모두 본다.

    이번 턴의 것만 보는 이유는, 그러지 않으면 이어지는 턴마다 같은 파일이 다시 올라가
    화면에 같은 것이 여러 번 뜨기 때문이다.
    """
    allowed = {str(ext).lower().lstrip(".") for ext in extensions}
    if not allowed:
        return []
    try:
        if not room_dir.is_dir():
            return []
    except OSError:
        return []

    found: list[tuple[int, str]] = []
    base = base_container.rstrip("/")
    for path in sorted(room_dir.rglob("*")):
        try:
            if not path.is_file():
                continue
            if path.suffix.lower().lstrip(".") not in allowed:
                continue
            rel = path.relative_to(room_dir).as_posix()
            if any(marker.strip("/") in f"/{rel}" for marker in _SCAN_EXCLUDED_MARKERS):
                continue
            stat = path.stat()
            if since_ns and stat.st_mtime_ns < since_ns:
                continue
            if stat.st_size <= 0:
                continue
        except OSError:
            continue
        found.append((stat.st_mtime_ns, f"{base}/{rel}"))

    # 만든 순서대로 — 화면에 뜨는 순서가 에이전트가 작업한 순서와 같아야 읽기 쉽다.
    return [container_path for _, container_path in sorted(found)]


__all__ = [
    "BINARY_DOCUMENT_EXTENSIONS",
    "WS_DOCUMENT_RE",
    "find_document_paths",
    "find_room_documents",
]
