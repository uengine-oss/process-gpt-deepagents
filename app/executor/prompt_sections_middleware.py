"""Prompt sections middleware.

Goal: avoid injecting the full "global rulebook" system prompt on every model call.
Instead, compose only the sections needed for the current step.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

try:
    from langchain.agents.middleware.types import AgentMiddleware
except Exception:  # pragma: no cover
    AgentMiddleware = None  # type: ignore[misc,assignment]

from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


def create_prompt_sections_middleware(
    *,
    sections: Mapping[str, str],
    is_chat: bool,
    route_gate: dict[str, Any] | None,
    planning_gate: dict[str, Any] | None,
    include_execution_clock: bool = True,
) -> Any | None:
    """Return middleware that injects only needed system-prompt sections per model call.

    Sections are passed as a dict (key -> section text). This middleware never mutates them.
    """
    if AgentMiddleware is None:
        return None

    sec = dict(sections or {})

    def _join(keys: list[str]) -> str:
        out: list[str] = []
        for k in keys:
            t = (sec.get(k) or "").strip()
            if t:
                out.append(t)
        return "\n\n".join(out).strip()

    def _route() -> str:
        if isinstance(route_gate, dict):
            r0 = str(route_gate.get("route") or "").strip().lower().replace("-", "_")
            if r0:
                return r0
        return ""

    def _saw_task() -> bool:
        if isinstance(planning_gate, dict):
            return bool(planning_gate.get("saw_task_delegation"))
        return False

    class PromptSectionsMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            # Step selection:
            # - Default: execution/tool loop -> minimal context + output requirements
            # - When delegating (task()) or route says needs_task: include multi-agent rules.
            route = _route()
            need_multi = (route == "needs_task") or _saw_task()
            include_clock = bool(include_execution_clock)

            if is_chat and route == "ask_user_first" and not need_multi:
                # Do not inject chat_output here: it nudges plain-text "final answers" and fights HITL.
                keys = ["base_identity", "lang", "sec1", "sec1b", "http_chat_rules_ask_user"]
            elif is_chat and not need_multi:
                keys = ["base_identity", "lang", "sec1", "sec1b", "chat_output", "http_chat_rules_generic"]
            elif need_multi:
                if is_chat:
                    keys = [
                        "base_identity",
                        "base_task",
                        "base_assets",
                        "base_mapping",
                        "base_hitl",
                        "lang",
                        "sec1",
                        "sec1a",
                        "sec1b",
                        "sec2",
                        "sec3",
                        "sec4",
                        "sec5",
                        "sec5b",
                        "chat_output",
                        "http_chat_rules_generic",
                    ]
                else:
                    keys = [
                        "base_identity",
                        "base_task",
                        "base_assets",
                        "base_structured_outputs",
                        "base_mapping",
                        "base_hitl",
                        "lang",
                        "sec1",
                        "sec1a",
                        "sec1b",
                        "sec2",
                        "sec3",
                        "sec4",
                        "sec5",
                        "sec5b",
                        "sec6",
                    ]
            else:
                # process mode execution loop
                keys = [
                    "base_identity",
                    "base_task",
                    "base_assets",
                    "base_structured_outputs",
                    "base_mapping",
                    "base_hitl",
                    "lang",
                    "sec1",
                    "sec1a",
                    "sec1b",
                    "sec6",
                ]
            if include_clock:
                # Put clock near the top to avoid being truncated in some proxies.
                keys = ["clock", *keys]

            text = _join(keys)
            if not text:
                return await handler(request)

            try:
                new_system_message = SystemMessage(content=[{"type": "text", "text": text}])  # type: ignore[arg-type]
                return await handler(request.override(system_message=new_system_message))
            except Exception:
                logger.exception("prompt sections middleware failed; continuing", exc_info=True)
                return await handler(request)

    return PromptSectionsMiddleware()

