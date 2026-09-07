"""
채팅에서 만든 문서를 받을 수 있어야 한다.

무엇이 잘못돼 있었나
    스킬이 만든 문서는 도커 샌드박스 안에만 있고, 채팅 답변에는 컨테이너 경로만
    실렸다.

        다운로드할 문서를 만들었습니다.
        파일 경로: /workspace/.bpmn/<방>/Team Overview.docx

    사용자에게 이 경로는 아무 뜻이 없다. 만들어졌다는 말만 있고 받을 길이 없다.
    폼(worklist) 산출물은 이미 실체화하고 있었지만(`if form_id:`) 채팅은 그
    갈래를 타지 않았다.

여기서 고정하는 것
    경로가 **내려받기 링크**로 바뀌는 것. 웹과 앱 모두 답변을 마크다운으로
    그리므로 링크 하나면 양쪽에서 받을 수 있다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat.file_links import (  # noqa: E402
    apply_links,
    file_name_of,
    find_document_paths,
    materialize,
)


def _resolver(known: dict):
    """경로 → 주소. 모르는 경로에는 주소를 주지 않는다."""
    asked: list[str] = []

    async def resolve(path: str):
        asked.append(path)
        return known.get(path)

    resolve.asked = asked  # type: ignore[attr-defined]
    return resolve


def _run(text, known):
    return asyncio.run(materialize(text, _resolver(known)))


def test_경로가_내려받기_링크로_바뀐다():
    path = "/workspace/.bpmn/room-1/Team Overview.docx"
    text = f"다운로드할 문서를 만들었습니다.\n파일 경로: {path}"

    out = _run(text, {path: "https://deep.example/api/internal/download/tok"})

    assert "[Team Overview.docx](https://deep.example/api/internal/download/tok)" in out
    assert path not in out, "컨테이너 경로가 그대로 남으면 사용자는 받을 길이 없다"


def test_여러_형식을_모두_다룬다():
    known = {
        "/workspace/a.pdf": "u1",
        "/workspace/b.xlsx": "u2",
        "/workspace/c.pptx": "u3",
        "/workspace/d.doc": "u4",
    }
    text = "만든 것: " + ", ".join(known)

    out = _run(text, known)

    for name in ("a.pdf", "b.xlsx", "c.pptx", "d.doc"):
        assert f"[{name}](" in out


def test_같은_파일이_여러_번_나와도_한_번만_묻는다():
    path = "/workspace/report.docx"
    text = f"{path} 를 만들었습니다. 다시: {path}"
    resolve = _resolver({path: "u"})

    out = asyncio.run(materialize(text, resolve))

    assert out.count("[report.docx](u)") == 2
    assert resolve.asked == [path], "같은 파일을 두 번 꺼낼 이유가 없다"


def test_주소를_못_만들면_경로를_그대로_둔다():
    """지어낸 주소로 링크를 만들면 눌렀을 때 아무것도 안 나온다 — 더 나쁘다."""
    text = "파일 경로: /workspace/없는파일.docx"

    assert _run(text, {}) == text


def test_일부만_주소가_있으면_그것만_바꾼다():
    text = "A: /workspace/a.docx  B: /workspace/b.docx"

    out = _run(text, {"/workspace/a.docx": "ua"})

    assert "[a.docx](ua)" in out
    assert "/workspace/b.docx" in out


def test_문서_경로가_없으면_손대지_않는다():
    assert _run("그냥 답변입니다.", {}) == "그냥 답변입니다."
    assert _run("", {}) == ""
    assert _run(None, {}) == ""


def test_hwpx_는_건드리지_않는다():
    """
    한글 문서는 미리보기·편집이 되는 별도 경로가 이미 맡고 있다.
    여기서도 링크로 바꾸면 같은 파일이 두 번 나온다.
    """
    text = "파일 경로: /workspace/보고서.hwpx"

    assert _run(text, {"/workspace/보고서.hwpx": "u"}) == text


def test_workspace_밖의_경로는_보지_않는다():
    text = "파일 경로: /etc/passwd.docx"

    assert find_document_paths(text) == []


def test_긴_경로부터_바꾼다():
    """
    짧은 경로가 긴 경로의 일부일 때 순서가 어긋나면 이미 만든 링크 안쪽이
    다시 치환되어 깨진다.
    """
    short = "/workspace/a.docx"
    long = "/workspace/a.docx.docx"
    text = f"{long} 그리고 {short}"

    out = apply_links(text, {short: "[S]", long: "[L]"})

    assert out == "[L] 그리고 [S]"


def test_괄호나_따옴표는_경로에_넣지_않는다():
    """마크다운 링크 안에 들어간 경로를 다시 잡으면 링크가 깨진다."""
    text = "이미 링크: [x](/workspace/a.docx) 끝"

    assert find_document_paths(text) == ["/workspace/a.docx"]


def test_파일_이름을_링크_글자로_쓴다():
    assert file_name_of("/workspace/.bpmn/r/Team Overview.docx") == "Team Overview.docx"
    assert file_name_of("/workspace/") == "workspace"


# ---------------------------------------------------------------------------
# 링크가 가리킬 주소
#
# 이 서버는 클러스터 안에서 8888 로 돌고 바깥에서는 게이트웨이가 넘겨 준다.
# 안쪽 주소를 실으면 휴대폰에서도 브라우저에서도 열리지 않는다.
# ---------------------------------------------------------------------------

from core.chat.file_links import public_download_base  # noqa: E402


def test_조직_주소로_링크를_만든다():
    base = public_download_base("uengine", env={})

    assert base == "https://uengine.process-gpt.io/process-gpt-deepagents"


def test_설치마다_다른_도메인을_넣을_수_있다():
    base = public_download_base(
        "acme", env={"DEEPAGENTS_PUBLIC_BASE_URL": "https://{tenant}.example.com/da"}
    )

    assert base == "https://acme.example.com/da"


def test_이미_쓰이던_SELF_BASE_URL_을_존중한다():
    base = public_download_base("uengine", env={"SELF_BASE_URL": "https://fixed.example/"})

    assert base == "https://fixed.example"


def test_조직을_모르면_안쪽_주소로_둔다():
    """지어낸 도메인으로 링크를 만드는 것보다 낫다."""
    assert public_download_base("", env={}) == "http://localhost:8888"
    assert public_download_base(None, env={"PORT": "9000"}) == "http://localhost:9000"


def test_안쪽_주소가_링크에_실리지_않는다():
    """이것이 실제로 사람들을 막던 지점이다."""
    base = public_download_base("uengine", env={})

    assert "localhost" not in base


# ---------------------------------------------------------------------------
# 에이전트가 이미 링크로 감싸 내놓는 경우
#
# 실기기에서 나온 실제 답변이 이랬다.
#     다운로드: [MobileCheck.docx](sandbox:/workspace/.bpmn/방/MobileCheck.docx)
# 경로 글자만 갈아끼우면 링크 안에 링크가 생기고, 마크다운은 첫 `)` 에서 주소를
# 끊는다. 파랗게 보이지만 눌러도 아무 데도 가지 않는다.
# ---------------------------------------------------------------------------


def test_이미_감싼_링크는_통째로_바꾼다():
    path = "/workspace/.bpmn/room/MobileCheck.docx"
    text = f"다운로드: [MobileCheck.docx](sandbox:{path})"

    out = _run(text, {path: "https://uengine.process-gpt.io/d/tok"})

    assert out == "다운로드: [MobileCheck.docx](https://uengine.process-gpt.io/d/tok)"
    assert "sandbox:" not in out
    assert "](http" in out and out.count("](") == 1, "링크 안에 링크가 남았다"


def test_감싼_글자가_달라도_바꾼다():
    path = "/workspace/a.docx"
    text = f"[여기서 받으세요]({path})"

    out = _run(text, {path: "u"})

    assert out == "[a.docx](u)"


def test_맨_경로에_붙은_sandbox_접두사도_지운다():
    path = "/workspace/a.docx"

    out = _run(f"파일: sandbox:{path}", {path: "u"})

    assert out == "파일: [a.docx](u)"
    assert "sandbox:" not in out
