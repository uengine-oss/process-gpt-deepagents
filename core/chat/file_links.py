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


# 링크가 가리킬 주소의 기본 모양.
#
# 이 서버는 클러스터 안에서 8888 포트로 돌고, 바깥에서는 게이트웨이가
# `/process-gpt-deepagents/**` 로 넘겨 준다. 그래서 `http://localhost:8888` 같은
# 안쪽 주소를 링크에 실으면 **휴대폰에서도 브라우저에서도 열리지 않는다.**
#
# 조직마다 도메인 앞부분이 다르므로 조직 이름을 끼워 넣는다.
DEFAULT_PUBLIC_BASE = "https://{tenant}.process-gpt.io/process-gpt-deepagents"


def public_download_base(tenant_id: Optional[str], env: Optional[dict] = None) -> str:
    """
    내려받기 링크에 쓸 바깥 주소.

    고르는 순서
      1. `DEEPAGENTS_PUBLIC_BASE_URL` — 도메인이 다른 설치를 위해. `{tenant}` 를
         쓰면 조직 이름으로 바뀐다.
      2. `SELF_BASE_URL` — 이미 쓰이던 값. 있으면 존중한다.
      3. 기본 모양(위)
    조직을 모르면 안쪽 주소로 둔다 — 지어낸 도메인보다 낫다.
    """
    import os

    source = env if env is not None else os.environ
    tenant = (tenant_id or "").strip()

    template = (source.get("DEEPAGENTS_PUBLIC_BASE_URL") or "").strip()
    if template:
        return template.replace("{tenant}", tenant).rstrip("/")

    explicit = (source.get("SELF_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    if tenant:
        return DEFAULT_PUBLIC_BASE.replace("{tenant}", tenant)

    port = (source.get("PORT") or "8888").strip()
    return f"http://localhost:{port}"


def apply_links(text: str, links: dict) -> str:
    """
    경로를 링크로 바꾼다.

    긴 경로부터 바꾼다. 짧은 경로가 긴 경로의 일부일 때 순서가 어긋나면 이미
    만든 링크 안쪽이 다시 치환되어 깨진다.

    에이전트가 경로를 이미 마크다운 링크로 감싸 내놓는 경우가 있다.

        [MobileCheck.docx](sandbox:/workspace/.bpmn/방/MobileCheck.docx)

    이때 경로 글자만 갈아끼우면 링크 안에 링크가 생긴다. 마크다운은 첫 번째
    `)` 에서 주소를 끊으므로 눌러도 아무 데도 가지 않는다 — 파랗게 보이기만
    한다. 그래서 감싼 링크가 있으면 **통째로** 바꾼다.
    """
    out = text
    for path in sorted(links, key=len, reverse=True):
        target = re.escape(path)
        # 감싸고 있는 마크다운 링크까지 함께. `sandbox:` 같은 접두사도 지운다.
        wrapped = re.compile(r"\[[^\]\n]*\]\(\s*[a-z]*:?" + target + r"\s*\)")
        out = wrapped.sub(lambda _m, v=links[path]: v, out)
        # 남은 맨 경로. 앞에 붙은 `sandbox:` 도 함께 지운다.
        bare = re.compile(r"(?:sandbox:)?" + target)
        out = bare.sub(lambda _m, v=links[path]: v, out)
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
