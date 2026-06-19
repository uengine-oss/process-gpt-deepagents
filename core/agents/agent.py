import os
from pathlib import Path

from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver

from ..sandbox.docker_sandbox import get_or_create_sandbox
from ..chat.hitl import get_hitl_tools
from ..llm.model import _build_model
from ..rag.tools import get_memento_tools
import asyncio

from ..skills.skills import _get_skills
from ..skills.git_providers.factory import get_provider as _get_git_provider
from .subagents import _build_subagents
from ..skills.tools import (
    checkout_to_workspace,
    extract_document_structure,
    list_git_repos,
    publish_skill,
    _current_sandbox as _skill_sandbox,
    _current_user_name as _skill_user_name,
    _current_user_email as _skill_user_email,
)
from ..storage.storage import set_tenant_id as _set_storage_tenant_id

_BASE_DIR = Path(__file__).parent.parent.parent
_SKILLS_DIR = Path(os.getenv("SKILLS_HOST", str(_BASE_DIR / "skills")))
_SKILLS_CONTAINER_MOUNT = "/skills"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Use the available tools when needed to answer questions accurately. "
    "Reply in Korean unless the user writes in another language."
    "\n\n## 코드·스킬 작업 규칙\n"
    "스킬이나 Git 레포지토리의 코드를 읽거나 수정·실행해야 할 때는 "
    "**반드시 `checkout_to_workspace`를 먼저 호출**해 /workspace/ 로 가져온 뒤 작업하세요.\n"
    "- 스킬명이 /skills/ 에 있으면 자동으로 복사됩니다.\n"
    "- 없으면 설정된 Git 레포를 자동으로 clone합니다.\n"
    "이 판단은 모델이 자율적으로 내려야 합니다. "
    "어떤 경로를 써야 하는지 사용자에게 묻지 마세요."
)


async def build_agent(
    tenant_id: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_tools: list = None,
    checkpointer=None,
    subagents_raw: list = None,
    tenant_mcp: dict | None = None,
    middleware: list = None,
    static_subagents: list = None,
    user_context: dict | None = None,
):
    tools = (
        list(extra_tools or [])
        + get_hitl_tools()
        + get_memento_tools()
        + [checkout_to_workspace, extract_document_structure, list_git_repos, publish_skill]
    )
    backend = get_or_create_sandbox(tenant_id, workspace_host=_BASE_DIR / "workspace", skills_host=_SKILLS_DIR)
    _skill_sandbox.set(backend)
    _skill_user_name.set((user_context or {}).get("user_name") or "")
    _skill_user_email.set((user_context or {}).get("user_email") or "")
    _set_storage_tenant_id(tenant_id)
    built_subagents = list(static_subagents or []) + await _build_subagents(
        subagents_raw or [], tenant_id, tenant_mcp, backend=backend
    )

    if user_context:
        parts = ["\n\n## 현재 사용자 정보"]
        if user_context.get("user_name"):
            parts.append(f"- 이름: {user_context['user_name']}")
        if user_context.get("user_email"):
            parts.append(f"- 이메일: {user_context['user_email']}")
        if user_context.get("user_jwt"):
            parts.append(f"- JWT: {user_context['user_jwt']}")
        system_prompt = system_prompt + "\n".join(parts)

    if built_subagents:
        agent_names = ", ".join(f"`{a['name']}`" for a in built_subagents)
        delegation_prompt = (
            f"\n\n## 서브에이전트 위임 규칙\n"
            f"현재 다음 서브에이전트가 구성되어 있습니다: {agent_names}\n"
            f"요청이 서브에이전트의 역할·전문 영역에 해당하면 task() 툴로 위임하세요. "
            f"서브에이전트의 역할과 무관한 일반 요청은 직접 처리하세요."
        )
        final_prompt = system_prompt + delegation_prompt
    else:
        final_prompt = system_prompt

    provider_config_id = ""
    if tenant_id:
        try:
            _git_provider = await asyncio.to_thread(_get_git_provider, tenant_id)
            provider_config_id = _git_provider.config_id
        except Exception:
            pass

    host_skills = _get_skills(tenant_id, provider_config_id) or []
    container_skills = []
    for p in host_skills:
        try:
            rel = Path(p).relative_to(_SKILLS_DIR)
            container_skills.append(f"{_SKILLS_CONTAINER_MOUNT}/{rel}")
        except ValueError:
            container_skills.append(p)

    kwargs = dict(
        model=_build_model(),
        tools=tools,
        system_prompt=final_prompt,
        skills=container_skills or None,
        backend=backend,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )
    if built_subagents:
        kwargs["subagents"] = built_subagents
    if middleware:
        kwargs["middleware"] = middleware

    return create_deep_agent(**kwargs)
