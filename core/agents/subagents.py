import asyncio
import json
import logging
import re

from deepagents import SubAgent

from .tool_retry import build_tool_retry_middleware
from ..llm.mcp import _load_mcp_tools_grouped
from ..skills.skills import FilteredSkillsMiddleware, _get_skills
from ..skills.git_providers.factory import get_provider as _get_git_provider

logger = logging.getLogger(__name__)


def _parse_csv(value) -> list[str]:
    """쉼표로 조인된 문자열 또는 리스트를 문자열 리스트로 변환."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _format_profile_traits(agent: dict) -> list[str]:
    """프로필 dict(description/role/goal/persona)를 프롬프트용 텍스트 라인으로 변환한다."""
    parts = []
    if desc := (agent.get("description") or "").strip():
        parts.append(desc)
    for key in ("role", "goal", "persona"):
        if val := (agent.get(key) or "").strip():
            parts.append(f"{key.capitalize()}: {val}")
    return parts


def _normalize_tool_filters(value) -> dict:
    """JSON 문자열 또는 dict로 들어오는 tool_filters를 dict로 정규화한다."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _build_subagent_system_prompt(agent: dict, tenant_id: str = "") -> str:
    username = agent.get("username") or agent.get("name", "에이전트")
    parts = [f"You are {username}."] + _format_profile_traits(agent)
    if tenant_id:
        parts.append(f"- 테넌트 ID: {tenant_id}")
    parts.append("Reply in Korean unless the user writes in another language.")
    return "\n".join(parts)


async def _build_subagents(
    agents: list,
    tenant_id: str = "",
    tenant_mcp: dict | None = None,
    backend=None,
) -> tuple[list[SubAgent], list[str], list[dict]]:
    """서브에이전트 목록, 연결된 MCP 서버 이름, 에이전트별 MCP 도구 메타를 반환한다."""
    subagents: list[SubAgent] = []
    mcp_connectors: list[str] = []
    mcp_tools_meta: list[dict] = []
    seen_names: set[str] = set()

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        username = (agent.get("username") or agent.get("name") or "").strip()
        alias = (agent.get("alias") or "").strip()
        description = (agent.get("description") or "").strip()

        if not (username or alias):
            logger.warning("subagent 건너뜀: username/alias 없음 (%s)", agent)
            continue
        if not description:
            description = agent.get("goal") or ""

        # username 우선, 없으면 alias → lowercase slug
        base = username if username else alias
        name = re.sub(r"[^\w-]", "-", base).lower().strip("-")
        base_name, idx = name, 1
        while name in seen_names:
            name = f"{base_name}-{idx}"
            idx += 1
        seen_names.add(name)

        mcp_server_names = _parse_csv(agent.get("tools"))
        tool_filters = _normalize_tool_filters(agent.get("tool_filters"))
        tools = []
        if mcp_server_names and tenant_mcp:
            grouped = await _load_mcp_tools_grouped(mcp_server_names, tenant_mcp, tool_filters=tool_filters)
            for srv, srv_tools in grouped.items():
                tools.extend(srv_tools)
                if srv not in mcp_connectors:
                    mcp_connectors.append(srv)
                tool_names = [
                    getattr(t, "name", "") for t in srv_tools
                    if getattr(t, "name", "")
                ]
                if tool_names:
                    mcp_tools_meta.append({
                        "agent": name,
                        "servers": [srv],
                        "tools": tool_names,
                    })

        skill_names = _parse_csv(agent.get("skills"))
        extra_middleware = []
        if skill_names and backend:
            provider_config_id = ""
            if tenant_id:
                try:
                    _git_provider = await asyncio.to_thread(_get_git_provider, tenant_id)
                    provider_config_id = _git_provider.config_id
                except Exception:
                    pass
            collection_paths = _get_skills(tenant_id, provider_config_id)
            logger.info("서브에이전트 '%s' 스킬 설정: names=%s collection=%s", name, skill_names, collection_paths)
            if collection_paths:
                extra_middleware.append(
                    FilteredSkillsMiddleware(
                        backend=backend,
                        sources=collection_paths,
                        allowed_skills=skill_names,
                    )
                )
        elif not skill_names:
            logger.info("서브에이전트 '%s': skills 설정 없음 (스킬 없이 빌드)", name)

        if tools:
            # MCP 도구(예: odoo-mcp aggregate_records/search_records) 호출이 ToolException
            # 등으로 실패해도 실행을 중단시키지 않고, 에러를 ToolMessage 로 모델에 돌려줘
            # 인자를 스스로 보정해 재시도하게 한다.
            extra_middleware.append(build_tool_retry_middleware())

        subagent: SubAgent = SubAgent(
            name=name,
            description=description,
            system_prompt=_build_subagent_system_prompt(agent, tenant_id),
        )
        if tools:
            subagent["tools"] = tools
        if extra_middleware:
            subagent["middleware"] = extra_middleware
        if model := agent.get("model"):
            subagent["model"] = model

        subagents.append(subagent)

    return subagents, mcp_connectors, mcp_tools_meta
