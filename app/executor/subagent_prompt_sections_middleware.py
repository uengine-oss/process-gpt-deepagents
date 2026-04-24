"""Subagent prompt sections middleware.

DeepAgents does not automatically propagate root middleware into subagent graphs.
Attach this middleware to each subagent spec (`sa["middleware"]`) to keep delegated
model calls small and purpose-specific.
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


def create_subagent_prompt_sections_middleware(
    *,
    sections: Mapping[str, str],
    include_execution_clock: bool = True,
) -> Any | None:
    """Inject a compact, step-aware system prompt for subagents."""
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

    def _scan_recent_messages_for_hints(request: Any) -> dict[str, bool]:
        """Heuristic: detect which extra rule blocks are useful for this step.

        We avoid relying on internal deepagents step metadata. Instead, we scan the last
        few message contents for tool names / markers.
        """
        msgs = getattr(request, "messages", None)
        if not isinstance(msgs, list) or not msgs:
            return {"doc": True, "sandbox": True}
        tail = msgs[-12:]
        blob = ""
        for m in tail:
            c = getattr(m, "content", None)
            if isinstance(c, str) and c:
                blob += "\n" + c
                continue
            # content can be list[dict] in some transports
            if isinstance(c, list):
                for it in c:
                    if isinstance(it, dict) and isinstance(it.get("text"), str):
                        blob += "\n" + it["text"]
        low = blob.lower()
        saw_sandbox = "sandbox_" in low or "exit_code" in low or "stderr" in low
        saw_docs = "read_skill_document" in low or "list_skills" in low or "skill_volume_probe" in low
        return {"doc": saw_docs, "sandbox": saw_sandbox}

    class SubagentPromptSectionsMiddleware(AgentMiddleware):
        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            hints = _scan_recent_messages_for_hints(request)
            keys = [
                "sub_base_identity",
                "sub_priorities",
                "sub_exec_policy",
                "sub_tenant",
                "sub_reporting",
            ]
            if hints.get("doc"):
                keys = [*keys, "sub_skill_tiers", "sub_doc_required"]
            if hints.get("sandbox"):
                keys = [*keys, "sub_sandbox_rules"]
            if include_execution_clock:
                keys = ["clock", *keys]
            # Keep language requirement near the top.
            keys = ["lang", *keys]

            text = _join(keys)
            if not text:
                return await handler(request)
            try:
                new_system_message = SystemMessage(content=[{"type": "text", "text": text}])  # type: ignore[arg-type]
                return await handler(request.override(system_message=new_system_message))
            except Exception:
                logger.exception("subagent prompt sections middleware failed; continuing", exc_info=True)
                return await handler(request)

    return SubagentPromptSectionsMiddleware()

