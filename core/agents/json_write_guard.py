"""`.json` 파일에 JSON 이 아닌 내용을 쓰려는 `write_file` 호출을 실행하지 않고 되돌려 보낸다.

## 왜 필요한가

프로세스 생성에서 모델(gpt-4.1)이 에이전트 파일 두 개를 한 응답의 병렬 `write_file` 두 개로
쓰는데, 두 번째 파일 내용의 끝 `}` 가 빠진 채 오는 일이 되풀이됐다(VM 체크포인트 5회 중 3회 —
10-29 튜토리얼 estimate_agent, take 5 quote_calc_agent, take B quotation-agent. 모두 두 번째 호출,
`finish_reason=tool_calls`). 같은 대화를 OpenAI 에 그대로 다시 보내 보면 스트리밍 원본 조각에서도,
스트리밍 없이 받은 응답에서도 같은 `}` 가 없다 — 응답 조립이나 출력 한도가 아니라 병렬 호출을
쓰는 모델 쪽에서 생긴다. 한 응답에 하나씩 쓰면(parallel_tool_calls=false) 8/8 모두 온전했다.

깨진 파일은 그대로 저장되고 턴 끝 검증이 그제야 잡아 결과 전달을 보류한다. 그때는 이미 모델의
턴이 끝나 있어, 사용자가 한 번 더 말을 걸어야 고칠 기회가 생긴다. 쓰는 순간에 잡아 오류로 돌려주면
모델이 같은 턴 안에서 그 파일 하나만 다시 쓴다.

`.json` 확장자 파일만 본다 — 이 서비스가 쓰는 `.json`(정의·manifest·에이전트 프로필·스킬 평가)은
모두 엄격한 JSON 으로 읽힌다. 폼(`.form`)은 HTML 이라 보지 않는다.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ToolCallRequest

logger = logging.getLogger(__name__)

_GUARDED_TOOLS = frozenset({"write_file"})


def invalid_json_write(tool_call: dict[str, Any]) -> str | None:
    """`.json` 파일에 쓸 내용이 JSON 으로 읽히지 않으면 모델에 돌려줄 오류 글, 아니면 None."""
    if tool_call.get("name") not in _GUARDED_TOOLS:
        return None
    args = tool_call.get("args") or {}
    path = args.get("file_path")
    content = args.get("content")
    if not isinstance(path, str) or not path.lower().endswith(".json") or not isinstance(content, str):
        return None
    try:
        json.loads(content)
        return None
    except json.JSONDecodeError as exc:
        where = f"{exc.msg}, {exc.lineno}행 {exc.colno}열"
    except ValueError as exc:  # 숫자 범위 등
        where = str(exc)
    return (
        f"Error: '{path}' 에 쓸 내용이 올바른 JSON 이 아니어서 파일을 쓰지 않았습니다({where}). "
        "끝의 '}' 나 ']' 가 빠졌는지 확인하고, 이 파일 하나만 write_file 한 번으로 내용 전체를 다시 쓰세요 "
        "(다른 파일 쓰기와 한 응답에 묶지 마세요). "
        "/ The content is not valid JSON, so nothing was written. Rewrite the whole file in a single write_file call."
    )


def _refusal(request: "ToolCallRequest") -> ToolMessage | None:
    tool_call = request.tool_call
    error = invalid_json_write(tool_call)
    if error is None:
        return None
    logger.warning(
        "write_file 거절 — JSON 이 아닌 내용 | path=%s len=%d",
        (tool_call.get("args") or {}).get("file_path"), len(str((tool_call.get("args") or {}).get("content") or "")),
    )
    return ToolMessage(content=error, name=tool_call.get("name"), tool_call_id=tool_call.get("id"), status="error")


class JsonWriteGuardMiddleware(AgentMiddleware):
    """`.json` 파일의 `write_file` 내용이 JSON 으로 읽히지 않으면 쓰지 않고 오류로 돌려준다."""

    def wrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        return _refusal(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        refusal = _refusal(request)
        if refusal is not None:
            return refusal
        return await handler(request)
