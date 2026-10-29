"""결과 전달을 보류한 턴의 사유를 모델의 대화 기록(체크포인트)에 남긴다.

## 왜 필요한가

턴 끝 검증이 결함을 잡으면 executor 는 사용자에게 갈 답을 보류 안내(「프로세스 정의가 검증을
통과하지 못해 결과 전달을 보류했어요 …」)로 바꾼다. 그러나 바뀌는 것은 화면에 나갈 글뿐이고,
그래프 체크포인트에는 모델이 마지막에 쓴 자기 답(「…성공적으로 생성되었습니다」)이 그대로 남는다.
다음 턴은 체크포인트가 있으면 새 사용자 메시지 하나만 이어 붙이므로(`build_graph_inputs` 의
`has_checkpoint`), 모델은 보류도 결함도 모른 채 「네, 그렇게 고쳐 주세요」를 받는다.

VM take B(2026-10-29, 방 e3fd49bc…)에서 실제로 그랬다. 보류에 답하자 모델은 도구를 하나도 부르지
않고 「프로세스가 이미 … 성공적으로 생성되어 있습니다」라고 답했고(모델 호출 1회), 4초 뒤 같은 보류가
다시 나갔다. 깨진 `agents/quotation-agent.json` 은 끝까지 그대로였다.

그래서 보류한 턴이 끝날 때 보류 사유와 결함 목록을 사람 메시지 하나로 이 대화의 기록에 덧붙인다.
다음 턴의 모델 입력은 [… 자기 답, 이 보류 안내, 사용자의 새 말] 순서가 된다. 보류하지 않은 턴은
아무것도 덧붙이지 않는다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

HOLD_SOURCE = "delivery_hold"
HOLD_HEADER = "[결과 전달 보류 — 직전 차례]"

_MAX_DEFECTS = 20

_KIND_REASON = {
    "validation": "턴 끝 검증에서 작업 폴더의 산출물에 결함이 남아",
    "integration": "산출물을 하나로 통합(complete_process_generation)하다 오류가 나",
    "unapplied": "고쳤다고 답했지만 정의·산출물 파일이 마지막으로 넘긴 때와 그대로여서",
}


def _defect_line(directory: str, defect: Any) -> str:
    if not isinstance(defect, dict):
        return f"- {str(defect).strip()}"
    head = " · ".join(
        str(v).strip()
        for v in (defect.get("severity"), defect.get("type"), defect.get("file"))
        if str(v or "").strip()
    )
    detail = str(defect.get("detail") or "").strip()
    where = f"{directory}: " if directory else ""
    return f"- {where}{'[' + head + '] ' if head else ''}{detail}".rstrip()


def defect_lines(failures: list[dict] | None) -> list[str]:
    """`[{"dir": .., "defects": [..]}]` 를 모델이 읽을 결함 줄로 편다(잘라 내지 않고 최대 20줄)."""
    lines: list[str] = []
    for failure in failures or []:
        if not isinstance(failure, dict):
            continue
        directory = str(failure.get("dir") or "").strip()
        for defect in failure.get("defects") or []:
            lines.append(_defect_line(directory, defect))
    if len(lines) > _MAX_DEFECTS:
        lines = lines[:_MAX_DEFECTS] + [f"- … 외 {len(lines) - _MAX_DEFECTS}건"]
    return lines


def integration_failures(result: str) -> list[dict]:
    """complete_process_generation 의 "ERROR: {...}" 결과에서 결함 목록을 꺼낸다(없으면 원문 한 줄)."""
    body = result[len("ERROR:"):].strip() if result.startswith("ERROR:") else result.strip()
    try:
        obj = json.loads(body)
    except Exception:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("validation_errors"), list) and obj["validation_errors"]:
        return [f for f in obj["validation_errors"] if isinstance(f, dict)]
    message = obj.get("message") if isinstance(obj, dict) else body
    return [{"dir": "", "defects": [{"type": "integration_error", "detail": str(message or body)[:1500]}]}]


def hold_note(kind: str, notice: str, failures: list[dict] | None = None) -> HumanMessage:
    """다음 턴의 모델이 읽을 보류 안내 — 사용자가 본 글, 남은 결함, 이번 차례에 할 일."""
    reason = _KIND_REASON.get(kind, "결함이 남아")
    parts = [
        HOLD_HEADER,
        f"직전 차례가 끝날 때 서비스가 {reason} 결과 전달을 보류했다. 사용자는 너의 마지막 답 대신 아래 "
        "보류 안내를 받았다 — 그 답에 쓴 완료·생성·수정 문장은 사용자에게 전달되지 않았고, 산출물은 아직 완성되지 않았다.",
        "",
        "사용자가 본 보류 안내:",
        *[f"> {line}" if line else ">" for line in str(notice or "").strip().splitlines()],
    ]
    lines = defect_lines(failures)
    if lines:
        parts += ["", "남은 결함:", *lines]
    parts += [
        "",
        "이번 차례에 할 일: 사용자의 다음 말에 답하기 전에 위 결함이 있는 파일을 도구로 고쳐 작업 폴더에 반영한다. "
        "올바른 JSON 이 아니라는 파일은 read_file 로 확인한 뒤 write_file 한 번으로 내용 전체를 다시 쓴다(파일 하나에 호출 하나). "
        "파일을 고치기 전에는 완료·생성·수정했다고 말하지 않는다. 차례가 끝나면 같은 검증이 다시 돌고, 결함이 남아 있으면 또 보류된다.",
    ]
    return HumanMessage(
        content="\n".join(parts),
        additional_kwargs={"lc_source": HOLD_SOURCE, "hold_kind": kind},
    )


async def record_delivery_hold(graph: Any, thread_id: str, hold: dict[str, Any], notice: str) -> bool:
    """보류 안내를 이 대화의 체크포인트에 덧붙인다. 실패해도 턴은 그대로 끝난다(경고만)."""
    if not thread_id or not hold:
        return False
    failures = hold.get("failures")
    if not failures and hold.get("error"):
        failures = integration_failures(str(hold["error"]))
    note = hold_note(str(hold.get("kind") or ""), notice, failures)
    config = {"configurable": {"thread_id": thread_id}}
    try:
        try:
            await graph.aupdate_state(config, {"messages": [note]})
        except Exception as exc:
            # 마지막으로 쓴 노드를 정하지 못하면(Ambiguous update) 모델 노드가 쓴 것으로 둔다.
            if "as_node" not in str(exc):
                raise
            await graph.aupdate_state(config, {"messages": [note]}, as_node="model")
    except Exception:
        logger.warning("보류 사유를 대화 기록에 남기지 못함 | thread_id=%s kind=%s", thread_id, hold.get("kind"), exc_info=True)
        return False
    logger.info(
        "보류 사유를 대화 기록에 남김 | thread_id=%s kind=%s defects=%d",
        thread_id, hold.get("kind"), len(defect_lines(failures)),
    )
    return True
