"""고치기 차례의 「고쳤습니다」를 실제 정의 변화와 맞춰 본다.

튜토리얼 VM 실측(방 c26c4108…): 흐름을 고쳐 달라는 요청에 딥 에이전트가 「이미 규정대로 프로세스 흐름을
수정했습니다」라고 답했지만 그 차례에 도구 호출이 하나도 없었고, 앞 차례의 수정은 요소의 `source` 필드만
바꿔 흐름(Sequence)이 그대로였다. 사용자는 저장한 뒤에야 처음과 같은 흐름을 보았다.

결과를 넘길 때(complete_process_generation 성공) 정의 폴더의 지문을 방에 남겨 두고, 다음 차례가
  1) 고쳐 달라는 요청에
  2) 「수정했습니다」처럼 고쳤다고 답했는데
  3) 작업 폴더 파일만 다루는 도구만 썼고(DB 를 바꾸는 MCP 도구 등이 없음)
  4) 정의·산출물 지문이 넘긴 때와 같으면
결과 전달을 보류하고 그렇게 알린다. 넷 중 하나라도 아니면 아무것도 하지 않는다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

# 방 폴더에 두는 「마지막으로 넘긴 지문」. 확장자가 없어 작업 폴더 재전송·문서 수집에 걸리지 않는다.
DELIVERED_MARK = ".process-delivered"

# 정의 파일 밖에서 지문에 넣지 않는 것(검증 기록·보고서 등 매 차례 다시 쓰이는 것).
_SKIP_NAMES = frozenset({"validation-report.md"})

# 이 도구들만 썼으면 바뀐 것은 모두 작업 폴더에 남는다. 그 밖의 도구(MCP·셸·하위 에이전트)를 쓴 차례는 보지 않는다.
FILE_ONLY_TOOLS = frozenset({
    "read_file", "ls", "glob", "grep", "edit_file", "write_file", "write_todos",
    "write_process_definition", "update_process_definition", "validate_process_definition",
    "read_document_page", "search_documents", "list_documents", "summarize_document",
    "extract_document_structure",
})

_CHANGE_REQUEST = re.compile(
    r"고쳐|고치|수정|바꿔|바꾸|변경|반영|추가해|빼\s*줘|빼\s*주|삭제|제거|옮겨"
    r"|\b(?:fix|change|update|modify|correct|adjust|revise|move|remove|add)\b",
    re.IGNORECASE,
)
_CHANGE_CLAIM = re.compile(
    r"(?:수정|변경|반영|교정|교체|추가|삭제|제거|연결|재구성|보완)\s*"
    r"(?:했|하였|해\s*(?:두었|뒀|놓았|드렸)|되었|됐|되어\s*있|을\s*완료|를\s*완료)"
    r"|고쳤|고쳐\s*(?:두었|뒀|놓았|드렸)|바꿨|바꾸었|바꿔\s*(?:두었|뒀|놓았|드렸)"
    r"|\b(?:I(?:'ve|\s+have)|has\s+been|have\s+been)\s+(?:already\s+)?"
    r"(?:updated|fixed|changed|modified|corrected|adjusted|revised|applied)\b",
    re.IGNORECASE,
)
# 프론트가 사용자 글 뒤에 붙이는 시스템 블록 — 요청 판단에서 뺀다(지시문에 「수정」 같은 말이 들어 있다).
_SYSTEM_BLOCK = re.compile(r"\n?\[(?:ExecutionPolicy|InputData|OpenUIForm)\]")
_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")

NOTICE = {
    "ko": (
        "고쳤다고 답했지만 이번 차례에 프로세스 정의와 산출물 파일이 바뀌지 않아 결과 전달을 보류했어요.\n"
        "- 흐름(연결선)은 process-definition.json 의 Sequence(source/target)로만 바뀝니다.\n"
        "고칠 단계와 연결을 다시 알려주시면 정의 파일을 실제로 고칠게요."
    ),
    "en": (
        "I said it was fixed, but the process definition and files didn't change in this turn, "
        "so I'm holding the result back.\n"
        "- The flow only changes through the Sequence elements (source/target) in process-definition.json.\n"
        "Tell me again which step or connection to change and I'll actually edit the definition."
    ),
}


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("", "none", "null", "{}", "[]")
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def definition_shape(definition: dict) -> dict:
    """정의의 내용만 남긴 꼴 — 파일 형식(elements[]/flattened)·정렬·빈 기본값·요소의 `source` 는 뺀다.

    `source` 는 흐름이 아니다(흐름은 Sequence). 정규화가 들어오는 연결이 없는 요소에 한해 source 로
    Sequence 를 채우므로, 실제로 흐름을 바꾼 source 수정은 그 Sequence 로 지문에 드러난다.
    """
    from . import tools

    normalized, _ = tools._normalize_to_processgpt_schema(copy.deepcopy(definition))
    if not isinstance(normalized, dict):
        return {"raw": normalized}
    elements = []
    for element in normalized.get("elements") or []:
        if not isinstance(element, dict):
            continue
        is_sequence = element.get("elementType") == "Sequence"
        item = {}
        for key, value in element.items():
            if key == "process" or _is_empty(value):
                continue
            if key == "source" and not is_sequence:
                continue
            if key == "id" and is_sequence:
                continue  # 연결선 id 는 저장 때마다 다시 지어진다
            item[key] = value
        elements.append(item)
    elements.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False, default=str))
    top = {
        key: value for key, value in normalized.items()
        if key not in ("elements", "processDefinitionId", "process")
        and not _is_empty(value)
        and not (key == "isHorizontal" and value is True)
    }
    return {"top": top, "elements": elements}


def room_signature(room: Path, work_dirs: list[Path]) -> str:
    """정의 폴더들의 지문. 정의 파일은 내용 꼴로, 나머지 파일(폼·에이전트·manifest·스킬)은 바이트로 잰다."""
    digest = hashlib.sha256()
    for work in sorted(work_dirs, key=lambda p: p.as_posix()):
        try:
            rel_dir = work.relative_to(room).as_posix()
        except ValueError:
            rel_dir = work.name
        digest.update(f"dir:{rel_dir}\n".encode("utf-8"))
        definition_file = work / "process-definition.json"
        try:
            shape = definition_shape(json.loads(definition_file.read_text(encoding="utf-8")))
            digest.update(json.dumps(shape, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            digest.update(b"definition:" + (definition_file.read_bytes() if definition_file.is_file() else b""))
        for path in sorted(work.rglob("*")):
            if not path.is_file() or path == definition_file or path.name in _SKIP_NAMES:
                continue
            rel = path.relative_to(work)
            if any(part.startswith(".") for part in rel.parts):
                continue
            digest.update(f"\nfile:{rel.as_posix()}:".encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def record_delivered(room: Path, work_dirs: list[Path]) -> None:
    """결과를 넘긴 때의 지문을 방에 남긴다."""
    (room / DELIVERED_MARK).write_text(
        json.dumps({"signature": room_signature(room, work_dirs)}, ensure_ascii=False),
        encoding="utf-8",
    )


def delivered_signature(room: Path) -> str:
    try:
        return str(json.loads((room / DELIVERED_MARK).read_text(encoding="utf-8")).get("signature") or "")
    except Exception:
        return ""


def _reply_lang(reply: str) -> str:
    return "en" if (not _HANGUL.search(reply) and _LATIN.search(reply)) else "ko"


def unapplied_change_notice(
    room: Path,
    conversation_id: str,
    *,
    request: str,
    reply: str,
    tools_used,
) -> str | None:
    """고쳤다는 답인데 정의·산출물이 넘긴 때와 같으면 사용자에게 보일 보류 안내, 아니면 None."""
    from . import tools

    reply = str(reply or "")
    asked = _SYSTEM_BLOCK.split(str(request or ""), maxsplit=1)[0]
    if not _CHANGE_CLAIM.search(reply) or not _CHANGE_REQUEST.search(asked):
        return None
    if any(str(name) not in FILE_ONLY_TOOLS for name in (tools_used or ())):
        return None
    delivered = delivered_signature(room)
    if not delivered:
        return None
    work_dirs = tools.definition_work_dirs(room, conversation_id)
    if not work_dirs or room_signature(room, work_dirs) != delivered:
        return None
    return NOTICE[_reply_lang(reply)]
