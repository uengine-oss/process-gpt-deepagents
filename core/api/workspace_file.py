"""core/api/workspace_file.py — 워크스페이스 파일을 경로로 내려받는다.

[왜 필요한가]
에이전트가 만든 산출물(hwpx, docx 등)은 워크스페이스에 파일로 남고,
워크아이템에는 그 **경로**만 적힌다 — 예: /workspace/.bpmn/<instId>/안내문.hwpx

그런데 화면에는 그 경로를 받아 갈 방법이 없었다. 그래서 사용자가 '다운로드' 를
눌러도 찾을 수 없다는 말만 돌아왔다.

여기서는 경로로 직접 내려준다. 대신 워크스페이스 밖으로는 절대 나가지 않는다 —
경로를 실제 위치로 풀어(resolve) 워크스페이스 안에 있는지 확인한 뒤에만 읽는다.
그러지 않으면 ../../ 같은 경로로 서버의 아무 파일이나 읽어 갈 수 있다.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, unquote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


def _roots() -> list[Path]:
    """워크스페이스가 있을 만한 자리들.

    컨테이너에서는 /workspace 이고, 로컬 실행에서는 저장소 안의 workspace/ 다.
    윈도우에서는 '/workspace' 가 C 드라이브 쪽으로 풀려 엉뚱한 곳을 가리킬 수 있어,
    하나로 못 박지 않고 후보를 차례로 본다.
    """
    out: list[Path] = []
    env = os.environ.get("WORKSPACE_ROOT")
    if env:
        out.append(Path(env))
    out.append(Path(__file__).resolve().parents[2] / "workspace")
    out.append(Path("/workspace"))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        try:
            r = p.resolve()
        except OSError:
            continue
        key = str(r)
        if key not in seen and r.is_dir():
            seen.add(key)
            uniq.append(r)
    return uniq


def _resolve(raw: str) -> Path | None:
    """요청한 경로를 워크스페이스 안의 실제 파일로 푼다. 밖이면 None."""
    if not raw:
        return None
    rel = unquote(raw).replace("\\", "/").lstrip("/")
    # 저장된 값이 '/workspace/...' 로 시작하는 경우가 흔하다. 뿌리 이름은 떼어 낸다.
    if rel.startswith("workspace/"):
        rel = rel[len("workspace/") :]

    for root in _roots():
        try:
            target = (root / rel).resolve()
        except OSError:
            continue
        # 워크스페이스 밖으로는 절대 나가지 않는다 — ../.. 로 서버 파일을 읽어 갈 수 없게.
        if root != target and root not in target.parents:
            logger.warning("workspace_file: 워크스페이스 밖 요청 거부 — %s", raw)
            continue
        if target.is_file():
            return target
    return None


def _filename_params(name: str) -> str:
    """Content-Disposition 에 붙일 파일 이름.

    헤더는 latin-1 로만 실려 간다 — '주차장_안내문.hwpx' 를 그대로 적으면
    응답을 만드는 순간 터진다. 그래서 RFC 5987 대로 퍼센트 인코딩한
    filename* 을 쓰고, 그것을 모르는 오래된 브라우저를 위해 아스키로 내린
    filename 을 함께 둔다.
    """
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


async def download_handler(request: Request) -> Response:
    target = _resolve(request.query_params.get("path", ""))
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    mime, _ = mimetypes.guess_type(target.name)
    disposition = "inline" if request.query_params.get("inline") else "attachment"
    return Response(
        content=target.read_bytes(),
        media_type=mime or "application/octet-stream",
        headers={
            "Content-Disposition": f"{disposition}; {_filename_params(target.name)}",
            "Cache-Control": "no-store",
        },
    )


routes = [Route("/api/internal/workspace-file", download_handler, methods=["GET"])]
