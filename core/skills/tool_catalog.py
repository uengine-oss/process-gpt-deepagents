"""스킬 전용 도구 지정, 스킬에 묶을 수 있는 도구 카탈로그, 도구별 스킬 연결 현황.

공식 deepagents 구조(Add tools to skills)를 따른다 — 도구를 숨길지는 도구 쪽에서 정한다.
에이전트 `tools` 에 있는 도구는 처음부터 보이고 `include_tools` 가 효과가 없다.
`SkillsMiddleware(tools=...)` 로 넘긴 도구만 그 도구를 `include_tools` 에 적은 스킬을 읽은 뒤에
보인다. 이 서비스에서는 테넌트 관리자가 "스킬 전용"으로 지정한 도구가 SkillsMiddleware 로 가고,
스킬의 `include_tools` 는 그 도구를 열어 줄 스킬만 정한다.

지정은 테넌트 설정 테이블(`configuration`)의 키 `skill_only_tools` 에 `{"tools": [이름, ...]}` 로 둔다.
카탈로그의 도구 이름은 실제 채팅 에이전트가 받는 이름과 같아야 한다 — 다르면 지정해도 숨겨지지
않는다. 그래서 MCP 도구는 채팅 실행과 같은 로더(`load_chat_root_tools`)로, 서비스 도구는 같은
목록(`root_service_tools`)으로 만들고, 이름이 겹치면 create_agent 처럼 마지막 것을 남긴다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

SKILL_ONLY_KEY = "skill_only_tools"

UNBINDABLE_TOOLS = frozenset({
    # deepagents 미들웨어가 늘 넣는 기본 도구. 에이전트 자신의 도구라 스킬 전용으로 지정할 수 없다
    # (공식 문서 "Add tools to skills"). 실제 목록은 테스트로 지킨다.
    "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task", "write_todos",
})

_CATALOG_TTL_OK = 600.0
_CATALOG_TTL_PARTIAL = 30.0
_catalog_cache: dict[tuple[str, str], tuple[float, dict]] = {}

_SKILL_ONLY_TTL = 15.0
_skill_only_cache: dict[str, tuple[float, frozenset[str]]] = {}
# 같은 서버 안에서 지정 읽기-고치기-쓰기가 겹치지 않게 한다(여러 서버 사이의 동시 변경은 막지 못한다).
_skill_only_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 스킬 전용 도구 지정
# ---------------------------------------------------------------------------

def _normalize_names(raw: Any) -> list[str]:
    tools = raw.get("tools") if isinstance(raw, dict) else None
    if not isinstance(tools, list):
        return []
    return sorted({str(t).strip() for t in tools if str(t).strip()} - UNBINDABLE_TOOLS)


def load_skill_only_tools(tenant_id: str, *, use_cache: bool = True) -> frozenset[str]:
    """테넌트 관리자가 스킬 전용으로 지정한 도구 이름. 설정이 없거나 조회에 실패하면 빈 집합."""
    if not tenant_id:
        return frozenset()
    now = time.monotonic()
    cached = _skill_only_cache.get(tenant_id)
    if use_cache and cached and now - cached[0] < _SKILL_ONLY_TTL:
        return cached[1]
    raw: Any = None
    try:
        from core.storage.db import get_supabase

        rows = (
            get_supabase().table("configuration").select("value")
            .eq("key", SKILL_ONLY_KEY).eq("tenant_id", tenant_id).limit(1).execute()
        ).data or []
        raw = rows[0].get("value") if rows else None
    except Exception:
        logger.warning("[skill-tools] 스킬 전용 도구 조회 실패 | tenant=%s", tenant_id, exc_info=True)
    names = frozenset(_normalize_names(raw))
    _skill_only_cache[tenant_id] = (now, names)
    return names


def save_skill_only_tools(tenant_id: str, tools: Any) -> list[str]:
    """스킬 전용 도구 목록을 통째로 저장하고 저장된 이름을 돌려준다.

    Raises:
        ValueError: 목록이 아니거나 에이전트 기본 도구가 들어 있을 때.
    """
    if not isinstance(tools, list):
        raise ValueError("tools 는 도구 이름 목록이어야 합니다")
    builtin = sorted({str(t).strip() for t in tools} & UNBINDABLE_TOOLS)
    if builtin:
        raise ValueError(f"에이전트 기본 도구는 스킬 전용으로 지정할 수 없습니다: {', '.join(builtin)}")
    names = _normalize_names({"tools": tools})
    from core.storage.db import get_supabase

    get_supabase().table("configuration").upsert(
        {"key": SKILL_ONLY_KEY, "tenant_id": tenant_id, "value": {"tools": names}}, on_conflict="key,tenant_id"
    ).execute()
    _skill_only_cache.pop(tenant_id, None)
    return names


def _name_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} 는 도구 이름 목록이어야 합니다")
    return [v.strip() for v in value if v.strip()]


def patch_skill_only_tools(tenant_id: str, add: Any = None, remove: Any = None) -> list[str]:
    """현재 지정에 `add` 를 더하고 `remove` 를 빼서 저장하고, 저장된 이름을 돌려준다.

    화면은 바꾼 도구만 보낸다 — 한 화면이 읽은 뒤 다른 화면·관리자가 바꾼 지정을 덮어쓰지 않게.

    Raises:
        ValueError: 둘 다 비었거나, 목록이 아니거나, 에이전트 기본 도구를 더하려 할 때.
    """
    add_names, remove_names = _name_list(add, "add"), _name_list(remove, "remove")
    if not add_names and not remove_names:
        raise ValueError("add 또는 remove 에 도구 이름이 있어야 합니다")
    builtin = sorted(set(add_names) & UNBINDABLE_TOOLS)
    if builtin:
        raise ValueError(f"에이전트 기본 도구는 스킬 전용으로 지정할 수 없습니다: {', '.join(builtin)}")
    with _skill_only_write_lock:
        current = load_skill_only_tools(tenant_id, use_cache=False)
        return save_skill_only_tools(tenant_id, sorted((current | set(add_names)) - set(remove_names)))


def classify(name: str) -> str:
    return "unbindable" if name in UNBINDABLE_TOOLS else "bindable"


# ---------------------------------------------------------------------------
# 카탈로그
# ---------------------------------------------------------------------------

def _schema_tokens(tool: Any) -> int:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)))
    except Exception:
        return 0


def _config_hash(tenant_mcp: dict) -> str:
    return hashlib.sha256(json.dumps(tenant_mcp, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def _build_raw_catalog(tenant_mcp: dict) -> dict:
    from core.agents.agent import root_service_tools
    from core.llm.mcp import load_chat_root_tools

    errors: dict[str, str] = {}
    mcp_tools, tool_servers = await load_chat_root_tools(tenant_mcp, errors=errors)
    entries: dict[str, dict] = {}
    mcp_ids = {id(t) for t in mcp_tools}
    # 채팅 루트와 같은 순서(MCP 다음 서비스 도구). 이름이 겹치면 create_agent 처럼 마지막이 남는다.
    for tool in [*mcp_tools, *root_service_tools(is_chat=True)]:
        name = getattr(tool, "name", "")
        if not name:
            continue
        server = tool_servers.get(name) if id(tool) in mcp_ids else None
        entries[name] = {
            "name": name,
            "source": "mcp" if server else "service",
            "server": server,
            "description": (getattr(tool, "description", "") or "").strip(),
            "schema_tokens": _schema_tokens(tool),
        }
    for name in sorted(UNBINDABLE_TOOLS - entries.keys()):
        # 에이전트 미들웨어가 넣는 기본 도구. 화면이 "묶을 수 없음"으로 보이도록 목록에 둔다.
        entries[name] = {"name": name, "source": "service", "server": None,
                         "description": "에이전트 기본 도구", "schema_tokens": 0}
    unavailable = [{"server": s, "reason": r} for s, r in sorted(errors.items())]
    return {"tools": list(entries.values()), "unavailable_servers": unavailable}


async def build_catalog(tenant_id: str, *, tenant_mcp: dict | None = None,
                        skill_only: frozenset[str] | None = None) -> dict:
    """`GET /skill-tools/catalog` 응답. MCP 설정 해시로 캐시하고, 스킬 전용 표시는 매번 현재 지정으로 계산한다."""
    from core.llm.mcp import fetch_tenant_mcp

    if tenant_mcp is None:
        tenant_mcp = await asyncio.to_thread(fetch_tenant_mcp, tenant_id)
    if skill_only is None:
        skill_only = await asyncio.to_thread(load_skill_only_tools, tenant_id, use_cache=False)
    key = (tenant_id, _config_hash(tenant_mcp))
    now = time.time()
    cached = _catalog_cache.get(key)
    if cached and now < cached[0]:
        raw = cached[1]
    else:
        raw = await _build_raw_catalog(tenant_mcp)
        raw["generated_at"] = now
        ttl = _CATALOG_TTL_PARTIAL if raw["unavailable_servers"] else _CATALOG_TTL_OK
        # 같은 테넌트의 이전 설정 캐시는 버린다.
        for k in [k for k in _catalog_cache if k[0] == tenant_id]:
            _catalog_cache.pop(k, None)
        _catalog_cache[key] = (now + ttl, raw)
    tools = [{**t, "category": classify(t["name"]), "skill_only": t["name"] in skill_only} for t in raw["tools"]]
    listed = {t["name"] for t in tools}
    return {
        "tools": sorted(tools, key=lambda t: (t["source"], t["server"] or "", t["name"])),
        # 지정돼 있지만 지금 카탈로그에 없는 도구(서버 연결 실패·삭제). 화면이 지정을 풀 수 있게 알린다.
        "missing_skill_only_tools": sorted(skill_only - listed),
        "unavailable_servers": raw["unavailable_servers"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(raw["generated_at"])),
    }


# ---------------------------------------------------------------------------
# 바인딩 현황
# ---------------------------------------------------------------------------

def tenant_skill_includes(tenant_id: str) -> dict[str, dict]:
    """이 테넌트 채팅 에이전트에 보이는 스킬의 `{스킬명: {"path", "include_tools"}}`."""
    from core.skills.skills import _get_skills, scan_skill_include_tools

    provider_config_id = ""
    try:
        from core.skills.git_providers.factory import get_provider

        provider_config_id = get_provider(tenant_id).config_id
    except Exception:
        pass
    return scan_skill_include_tools(_get_skills(tenant_id, provider_config_id) or [])


def _csv(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    return []


def direct_server_assignments(tenant_id: str) -> dict[str, list[dict]]:
    """MCP 서버 이름 → 그 서버를 직접 지정한 액티비티·에이전트 프로필.

    그런 업무에서도 스킬에 묶인 도구는 스킬을 읽어야 보인다는 것을 화면이 알리기 위해 쓴다.
    """
    from core.storage.db import get_supabase

    sb = get_supabase()
    result: dict[str, list[dict]] = {}
    try:
        defs = (
            sb.table("proc_def").select("id,name,definition,isdeleted").eq("tenant_id", tenant_id).execute()
        ).data or []
        for row in defs:
            if row.get("isdeleted"):
                continue
            for act in ((row.get("definition") or {}).get("activities") or []):
                for server in _csv(act.get("tools")):
                    result.setdefault(server, []).append({
                        "kind": "activity", "proc_def_id": row.get("id"), "proc_def_name": row.get("name"),
                        "activity_id": act.get("id"), "activity_name": act.get("name"),
                    })
    except Exception:
        logger.warning("[skill-tools] 액티비티 도구 지정 조회 실패 | tenant=%s", tenant_id, exc_info=True)
    try:
        agents = (
            sb.table("users").select("id,username,tools,is_agent").eq("tenant_id", tenant_id).eq("is_agent", True).execute()
        ).data or []
        for row in agents:
            for server in _csv(row.get("tools")):
                result.setdefault(server, []).append({"kind": "agent", "agent_id": row.get("id"), "agent_name": row.get("username")})
    except Exception:
        logger.warning("[skill-tools] 에이전트 도구 지정 조회 실패 | tenant=%s", tenant_id, exc_info=True)
    return result


async def build_bindings(tenant_id: str, *, catalog: dict | None = None) -> dict:
    """`GET /skill-tools/bindings` 응답 — 도구마다 그 도구를 `include_tools` 에 적은 스킬.

    - `bindings[도구]`: `skills`(적은 스킬), `skill_only`(관리자 지정 여부), `server`, `direct_assignments`.
      `skill_only` 가 거짓이면 스킬에 적혀 있어도 처음부터 보인다(공식 동작).
    - `skill_only_without_skill`: 스킬 전용인데 어느 스킬도 적지 않아 어떤 대화에서도 쓸 수 없는 도구.
    - `unknown_tools`: 스킬이 적었지만 카탈로그에 없는 이름.
    """
    skills = await asyncio.to_thread(tenant_skill_includes, tenant_id)
    if catalog is None:
        catalog = await build_catalog(tenant_id)
    by_name = {t["name"]: t for t in catalog["tools"]}
    assignments = await asyncio.to_thread(direct_server_assignments, tenant_id)
    bindings: dict[str, dict] = {}
    unknown: list[dict] = []
    for skill_name, skill in sorted(skills.items()):
        for tool in skill["include_tools"]:
            entry = by_name.get(tool)
            if entry is None:
                unknown.append({"tool": tool, "skill": skill_name})
                continue
            item = bindings.setdefault(tool, {
                "skills": [], "skill_only": entry["skill_only"], "server": entry["server"],
                "direct_assignments": assignments.get(entry["server"], []) if entry["server"] else [],
            })
            item["skills"].append(skill_name)
    orphan = sorted(t["name"] for t in catalog["tools"] if t["skill_only"] and t["name"] not in bindings)
    return {"bindings": bindings, "skill_only_without_skill": orphan, "unknown_tools": unknown}
