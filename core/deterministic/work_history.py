"""작업 이력의 런타임 비의존 정규화(실행 런타임용 축약본).

보상(undo) 코드에 넘길 입력은 "직전 실행이 무엇을 했는가"다. 그 이력은 실행 런타임
마다 다른 모양으로 남으므로, 특정 러너의 이벤트 스키마로 걸러내면 다른 러너가 남긴
이력이 통째로 버려진다.

고착화 코드 **생성**은 completion 서비스(`work_history.py`)가 담당한다. 여기에는
실행 런타임이 필요로 하는 만큼 — 이벤트를 행위 목록으로 읽고, 되돌릴 대상을
가려내는 부분 — 만 둔다. 두 곳의 분류 규칙은 같은 관측 대상을 보므로 같은 결론을
내야 하며, 어긋나면 생성한 코드와 되돌릴 입력이 서로 다른 세계를 가리키게 된다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .signature import is_readonly_sql, looks_like_sql

MCP_CALL = "mcp_call"
SHELL = "shell"  # deterministic_signature.SHELL_KIND 와 같은 값이어야 한다
FILE_WRITE = "file_write"
FILE_READ = "file_read"
SKILL_READ = "skill_read"
DELEGATE = "delegate"
INSPECT = "inspect"
PLAN = "plan"
INTERNAL = "internal"

EFFECT_KINDS = (MCP_CALL, SHELL, FILE_WRITE)

EXCLUDED_TOOLS = {"mem0", "memento", "human_asked", "dmn_rule"}


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    raw_args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    timestamp: str = ""
    failed: bool = False

    @property
    def has_effect(self) -> bool:
        return self.kind in EFFECT_KINDS


_FINISHED_TYPES = {
    "tool_usage_finished", "tool_finished", "tool_end", "tool_call_finished",
    "tool_result", "action_finished", "tool_completed",
}
_STARTED_TYPES = {
    "tool_usage_started", "tool_started", "tool_start", "tool_call",
    "action_started", "tool_use",
}

_TOOL_KEYS = ("tool_name", "tool", "toolName", "name", "function_name", "function", "action")
_ARG_KEYS = ("args", "tool_args", "arguments", "input", "tool_input", "parameters", "params", "kwargs")
_RESULT_KEYS = ("result", "output", "response", "content", "observation")
_NESTED_KEYS = ("data", "payload", "event", "body")

_NAME_TAIL = re.compile(r"[^A-Za-z0-9]+")
_SKILL_PATH = re.compile(r"(^|/)(skills?)/|SKILL\.md$|\.claude/skills/", re.IGNORECASE)

_SHELL_TOOLS = {
    "execute", "bash", "sh", "shell", "run_shell", "run_shell_command", "run_command",
    "execute_command", "execute_shell", "terminal", "command", "subprocess",
    "code_execution", "run_script", "code_interpreter",
}
_SHELL_ARG_KEYS = ("command", "cmd", "script", "shell_command", "bash_command", "code")
_SHELL_SIDE_ARGS = {
    "cwd", "working_dir", "workdir", "directory", "timeout", "timeout_s", "timeout_ms",
    "shell", "env", "stream", "background", "restart", "description",
}
_READ_TOOLS = {
    "read_file", "read", "view", "view_file", "cat", "open_file", "read_text_file",
    "get_file", "fetch_file", "load_file",
}
_WRITE_TOOLS = {
    "write_file", "edit_file", "create_file", "update_file", "append_file", "write",
    "apply_patch", "patch_file", "str_replace", "str_replace_editor", "multi_edit",
    "notebook_edit", "write_process_definition", "update_process_definition", "save_file",
    "delete_file", "remove_file", "move_file", "rename_file", "copy_file", "mkdir",
}
_PATH_ARG_KEYS = ("file_path", "path", "filepath", "filename", "file", "target_file", "notebook_path")
_CONTENT_ARG_KEYS = ("content", "text", "contents", "data", "body", "definition")
_INSPECT_TOOLS = {
    "ls", "list", "list_files", "glob", "grep", "find", "search", "search_files",
    "search_documents", "web_search", "fetch_webpage", "tree", "list_directory",
}
_DELEGATE_TOOLS = {"task", "subagent", "delegate", "spawn_agent", "run_subagent", "agent"}
_PLAN_TOOLS = {
    "write_todos", "todo_write", "update_plan", "plan", "think", "thinking",
    "sequentialthinking", "note",
}


def _loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return value


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _payload(event: dict[str, Any]) -> dict[str, Any] | None:
    data = _loads(event.get("data"))
    for _ in range(3):
        if not isinstance(data, dict):
            return None
        if _first(data, _TOOL_KEYS) is not None:
            return data
        nested = _loads(_first(data, _NESTED_KEYS))
        if not isinstance(nested, dict):
            return None
        data = nested
    return None


def _tool_name(payload: dict[str, Any]) -> str:
    raw = _first(payload, _TOOL_KEYS)
    if isinstance(raw, dict):
        raw = _first(raw, ("name", "tool_name"))
    return str(raw or "").strip()


def _arguments(payload: dict[str, Any]) -> dict[str, Any]:
    raw = _loads(_first(payload, _ARG_KEYS))
    if isinstance(raw, dict):
        return raw
    if raw is None:
        reserved = set(_TOOL_KEYS) | set(_ARG_KEYS) | set(_RESULT_KEYS) | set(_NESTED_KEYS)
        return {k: v for k, v in payload.items() if k not in reserved}
    return {"input": raw}


def _tail_names(tool: str) -> set[str]:
    parts = [p.lower() for p in _NAME_TAIL.split(tool or "") if p]
    candidates = {"_".join(parts)}
    if parts:
        candidates.add(parts[-1])
    if len(parts) >= 2:
        candidates.add("_".join(parts[-2:]))
    return candidates


def _text_arg(args: dict[str, Any], keys: Iterable[str]) -> tuple[str, str] | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    return None


def _sql_values(args: dict[str, Any]) -> list[str]:
    return [v for v in (args or {}).values() if looks_like_sql(v)]


def _looks_like_shell_args(args: dict[str, Any]) -> bool:
    found = _text_arg(args, _SHELL_ARG_KEYS)
    if found is None or _sql_values(args):
        return False
    rest = {k for k in args if k != found[0] and args[k] not in (None, "")}
    return not (rest - _SHELL_SIDE_ARGS)


# 도구 이름의 동사로 부수효과 유무를 가린다. 판정 규칙은 completion 쪽 생성기와
# 같아야 한다 — 어긋나면 만든 코드와 되돌릴 입력이 서로 다른 세계를 가리킨다.
_READ_VERBS = {
    "get", "list", "search", "find", "read", "fetch", "query", "describe",
    "count", "lookup", "show", "retrieve", "view", "inspect", "browse", "preview",
}
_WRITE_VERBS = {
    "create", "insert", "add", "update", "modify", "edit", "patch", "set", "put",
    "delete", "remove", "drop", "clear", "reset", "send", "post", "write", "save",
    "store", "upload", "publish", "submit", "confirm", "approve", "reject",
    "request", "cancel", "apply", "register", "execute", "run", "start", "stop",
    "keep", "record", "assign", "move", "copy", "rename", "generate", "issue",
}


def _classify_by_verb(tool: str) -> str:
    words = {p.lower() for p in _NAME_TAIL.split(tool or "") if p}
    if words & _WRITE_VERBS:
        return MCP_CALL
    if words & _READ_VERBS:
        return INSPECT
    return MCP_CALL


def classify(tool: str, args: dict[str, Any]) -> str:
    """도구 호출 하나의 종류. 이름은 힌트, 최종 판정은 인자 내용이 한다."""
    args = args or {}
    names = _tail_names(tool)
    if not tool or names & EXCLUDED_TOOLS or tool in EXCLUDED_TOOLS:
        return INTERNAL
    if names & _PLAN_TOOLS:
        return PLAN
    if names & _DELEGATE_TOOLS:
        return DELEGATE
    if names & _SHELL_TOOLS or _looks_like_shell_args(args):
        return SHELL
    if names & _WRITE_TOOLS and _text_arg(args, _PATH_ARG_KEYS):
        return FILE_WRITE
    if names & _READ_TOOLS:
        # 인자 없이 결과만 기록한 이벤트도 있다. 읽기 도구는 이름만으로도 부수효과가
        # 없다고 볼 수 있다 — 되돌릴 것도, 재현할 것도 없다.
        path = _text_arg(args, _PATH_ARG_KEYS)
        if path and _SKILL_PATH.search(path[1]):
            return SKILL_READ
        return FILE_READ
    if names & _INSPECT_TOOLS:
        return INSPECT
    sql = _sql_values(args)
    if sql:
        return INSPECT if all(is_readonly_sql(v) for v in sql) else MCP_CALL
    return _classify_by_verb(tool)


def _canonical_args(kind: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    if kind == SHELL:
        found = _text_arg(args, _SHELL_ARG_KEYS)
        cwd = args.get("cwd") or args.get("working_dir") or args.get("workdir") or ""
        return {"command": found[1] if found else "", "cwd": str(cwd or "")}
    if kind in (FILE_WRITE, FILE_READ, SKILL_READ):
        path = _text_arg(args, _PATH_ARG_KEYS)
        canonical: dict[str, Any] = {"path": path[1] if path else ""}
        if kind == FILE_WRITE:
            content = _text_arg(args, _CONTENT_ARG_KEYS)
            raw = args.get("definition")
            canonical["content"] = (
                content[1]
                if content is not None
                else json.dumps(raw, ensure_ascii=False, indent=2) if raw is not None else ""
            )
            for old_key in ("old_string", "old_str", "old"):
                if isinstance(args.get(old_key), str):
                    canonical["old_string"] = args[old_key]
                    break
            for new_key in ("new_string", "new_str", "new"):
                if isinstance(args.get(new_key), str):
                    canonical["new_string"] = args[new_key]
                    break
            canonical["mode"] = "edit" if "old_string" in canonical else "write"
        return canonical
    if kind == DELEGATE:
        return {
            "subagent": str(args.get("subagent_type") or args.get("agent") or tool),
            "description": str(args.get("description") or args.get("prompt") or ""),
        }
    return dict(args)


# 도구가 실패했음을 알리는 표식. 런타임(어댑터)이 붙이는 문구와 도구 자신이 돌려주는
# 구조화된 오류 본문 두 갈래를 모두 본다.
_FAILURE_MARKERS = (
    "ToolException",
    "Error calling tool",
    "failed after",
    "Traceback (most recent call last)",
)
_ERROR_FIELDS = ("error", "err", "exception")


def _payload_says_error(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("isError") is True or node.get("is_error") is True:
        return True
    if node.get("ok") is False or node.get("success") is False:
        return True
    for key in ("result", "status", "state", "outcome"):
        if str(node.get(key) or "").strip().lower() in ("error", "failed", "failure"):
            return True
    if any(node.get(key) for key in _ERROR_FIELDS):
        return True
    return bool(node.get("error_kind"))


def looks_failed(result: Any) -> bool:
    """도구 호출 결과가 실패를 말하고 있는가.

    실패한 호출을 성공한 실행으로 착각해 굳히면, 고착화된 코드가 **실패를 충실히
    재현한다**. 그것도 매번 LLM 없이, 사람이 못 알아채는 채로. 표본 자격을 성공한
    실행으로 좁히는 이유가 여기 있다.

    판정은 특정 런타임의 오류 형식에 기대지 않는다. 어댑터가 덧붙이는 문구와 도구가
    본문으로 돌려주는 구조화된 오류를 모두 본다.
    """
    if result is None:
        return False
    if isinstance(result, dict):
        if _payload_says_error(result):
            return True
        # MCP 콘텐츠 블록은 본문을 text/content 에 문자열로 담는다. 도구가 돌려준
        # 구조화된 오류는 그 안에 들어 있으므로 한 겹 더 열어 본다.
        return any(
            looks_failed(result[key])
            for key in ("text", "content", "data", "body", "output")
            if isinstance(result.get(key), (str, dict, list))
        )
    if isinstance(result, (list, tuple)):
        return any(looks_failed(item) for item in result)
    text = str(result)
    if any(marker in text for marker in _FAILURE_MARKERS):
        return True
    parsed = _loads(text)
    if isinstance(parsed, (dict, list)):
        return looks_failed(parsed)
    return False


def to_action(tool: str, args: dict[str, Any], *, result: Any = None, timestamp: str = "") -> Action:
    kind = classify(tool, args)
    return Action(
        kind=kind,
        tool=tool,
        args=_canonical_args(kind, tool, args),
        raw_args=dict(args or {}),
        result=result,
        timestamp=timestamp or "",
        failed=looks_failed(result),
    )


def normalize_events(events: Iterable[dict[str, Any]] | None) -> list[Action]:
    """events 행 목록을 시간 오름차순 행위 목록으로 바꾼다.

    끝난 이벤트를 우선한다(인자와 결과가 함께 있다). 끝난 이벤트가 하나도 없는
    런타임에 한해 시작 이벤트로 폴백한다 — 둘을 섞으면 같은 호출이 두 번 세어진다.
    """
    rows = sorted(events or [], key=lambda e: str(e.get("timestamp") or ""))
    finished: list[Action] = []
    started: list[Action] = []
    for event in rows:
        event_type = str(event.get("event_type") or "").strip().lower()
        is_finished = event_type in _FINISHED_TYPES or (
            "tool" in event_type and event_type.endswith(("finished", "end", "completed"))
        )
        is_started = event_type in _STARTED_TYPES or (
            "tool" in event_type and event_type.endswith(("started", "start"))
        )
        if not (is_finished or is_started):
            continue
        payload = _payload(event)
        if payload is None:
            continue
        tool = _tool_name(payload)
        if not tool:
            continue
        action = to_action(
            tool,
            _arguments(payload),
            result=_first(payload, _RESULT_KEYS) if is_finished else None,
            timestamp=str(event.get("timestamp") or ""),
        )
        (finished if is_finished else started).append(action)
    return finished or started


def to_log_entries(actions: Iterable[Action]) -> list[dict[str, Any]]:
    """보상 코드에 넘길 이력 표현. completion 쪽 생성 프롬프트와 같은 모양이어야 한다."""
    entries: list[dict[str, Any]] = []
    for action in actions:
        entry: dict[str, Any] = {
            "timestamp": action.timestamp,
            "kind": action.kind,
            "tool_name": action.tool,
        }
        if action.has_effect:
            entry["args"] = action.raw_args
            entry["canonical_args"] = action.args
            entry["log_data"] = {"tool_name": action.tool, "args": action.raw_args}
        else:
            entry["summary"] = {k: v for k, v in action.args.items() if isinstance(v, str)}
        entries.append(entry)
    return entries


def needs_sandbox(actions: Iterable[Action]) -> bool:
    """이 이력을 재현하려면 워크스페이스(셸·파일)가 필요한가."""
    return any(action.kind in (SHELL, FILE_WRITE) for action in actions)
