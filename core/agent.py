from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from langgraph.checkpoint.memory import MemorySaver

from .hitl import get_hitl_tools
from .model import _build_model
from .skills import _get_skills
from .subagents import _build_subagents

_BASE_DIR = Path(__file__).parent.parent

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Use the available tools when needed to answer questions accurately. "
    "Reply in Korean unless the user writes in another language."
)


async def build_agent(
    tenant_id: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_tools: list = None,
    checkpointer=None,
    subagents_raw: list = None,
    tenant_mcp: dict | None = None,
    middleware: list = None,
):
    tools = list(extra_tools or []) + get_hitl_tools()
    backend = LocalShellBackend(root_dir=str(_BASE_DIR), virtual_mode=False, inherit_env=True)
    built_subagents = await _build_subagents(
        subagents_raw or [], tenant_id, tenant_mcp, backend=backend
    )

    if built_subagents:
        agent_names = ", ".join(f"`{a['name']}`" for a in built_subagents)
        delegation_prompt = (
            f"\n\n## 서브에이전트 위임 규칙\n"
            f"현재 다음 서브에이전트가 구성되어 있습니다: {agent_names}\n"
            f"사용자 요청은 반드시 적절한 서브에이전트에게 task() 툴로 위임해야 합니다. "
            f"직접 답변하지 마세요."
        )
        final_prompt = system_prompt + delegation_prompt
    else:
        final_prompt = system_prompt

    kwargs = dict(
        model=_build_model(),
        tools=tools,
        system_prompt=final_prompt,
        skills=_get_skills(tenant_id),
        backend=backend,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )
    if built_subagents:
        kwargs["subagents"] = built_subagents
    if middleware:
        kwargs["middleware"] = middleware

    return create_deep_agent(**kwargs)
