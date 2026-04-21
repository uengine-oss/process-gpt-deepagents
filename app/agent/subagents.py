"""Load agent definitions from Supabase and build deepagents subagent configs."""

from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

from app.database import get_supabase
from app.execution_context import format_execution_clock_section
from app.runtime_config import normalize_litellm_slash_model_name
from app.sandbox.tools import get_sandbox_tools
from app.skills.resolution_tools import get_skill_resolution_tools

# Tier 2: MCP tools are attached to every subagent via DEFAULT_MCP_SERVER_NAMES
# (when tenant_mcp includes that server). computer-use remains virtual (sandbox replaces shell).
# NOTE: 서브에이전트는 기본적으로 claude-skills + (mem0/memento/dmn_rule)만 가진다.
# work-assistant MCP는 "루트(딥 에이전트) 전용"으로 사용하며 서브에이전트에는 기본 부착하지 않는다.
DEFAULT_MCP_SERVER_NAMES: tuple[str, ...] = ("claude-skills",)
SKILLS_ROOT_DIR = (os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")
VIRTUAL_TOOL_NAMES: set[str] = {"computer-use"}
ALWAYS_ATTACH_LOCAL_TOOL_NAMES: tuple[str, ...] = ("mem0", "memento", "dmn_rule")
_BLOCKED_SUBAGENT_MCP_SERVERS: set[str] = {"work-assistant"}

logger = logging.getLogger(__name__)

_OPENAI_FORBIDDEN_MESSAGE_NAME_CHARS = re.compile(r'[\s<|\\/>"]+')


def _openai_safe_agent_name(display_name: str, *, fallback: str) -> str:
    """Return OpenAI ChatCompletions-safe token for message/agent names.

    OpenAI requires messages[*].name to match: ^[^\\s<|\\\\/>]+$
    """
    s = (display_name or "").strip()
    if not s:
        s = fallback
    s = _OPENAI_FORBIDDEN_MESSAGE_NAME_CHARS.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or fallback


def _wrap_tools_langchain(tools: list[Any]) -> list[Any]:
    """Match :func:`executor.langchain_tools._ensure_langchain_tools` for freshly built local tools."""
    if not tools:
        return tools
    try:
        from app.executor.langchain_tools import _ensure_langchain_tools

        return _ensure_langchain_tools(tools)
    except Exception:
        logger.debug("LangChain tool wrap skipped for tenant context tools", exc_info=True)
        return tools


def _clone_tool_for_user_id(base_tool: Any, user_id: str) -> Any:
    """Reuse the same underlying client/memory, but bind user_id per subagent.

    IMPORTANT: This avoids per-subagent initialization (e.g. DB connections) while still
    keeping `mem0`/`dmn_rule` scoped to each subagent's user_id.
    """
    t = copy.copy(base_tool)
    uid = (user_id or "").strip()
    # Mem0Tool / DMNRuleTool in processgpt_agent_utils keep user binding in private attrs.
    if uid:
        for attr in ("_user_id", "_namespace", "user_id", "namespace"):
            if hasattr(t, attr):
                try:
                    setattr(t, attr, uid)
                except Exception:
                    pass
    return t


def _sandbox_tool_names() -> set[str]:
    """Return the tool names provided by sandbox_tools.get_sandbox_tools()."""
    try:
        return {getattr(t, "name", type(t).__name__) for t in get_sandbox_tools()}
    except Exception:
        # Defensive: never block subagent config due to sandbox tool introspection.
        return set()


def _baseline_mcp_tools(server_to_tools: dict[str, list[Any]]) -> list[Any]:
    """Match DEFAULT_MCP_SERVER_NAMES to server_to_tools keys case-insensitively."""
    wanted = {b.lower().replace("_", "-") for b in DEFAULT_MCP_SERVER_NAMES}
    out: list[Any] = []
    for skey, tools in server_to_tools.items():
        normalized = str(skey).lower().replace("_", "-")
        if normalized in wanted or any(normalized.endswith(w) for w in wanted):
            out.extend(tools)
    return out


def _skill_resolution_tool_names() -> set[str]:
    try:
        return {getattr(t, "name", type(t).__name__) for t in get_skill_resolution_tools()}
    except Exception:
        return set()


def load_agents_from_db(
    agent_ids: list[str],
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Query the users table for the given agent IDs (is_agent=true)."""
    if not agent_ids:
        return []

    resp = (
        get_supabase()
        .table("users")
        .select("*")
        .in_("id", agent_ids)
        .eq("tenant_id", tenant_id)
        .eq("is_agent", True)
        .execute()
    )
    agents = resp.data or []
    logger.info("Loaded %d agent(s) from DB for tenant=%s", len(agents), tenant_id)
    return agents


def load_tenant_agents_for_chat(
    tenant_id: str,
    *,
    limit: int = 64,
) -> list[dict[str, Any]]:
    """List agent rows (is_agent=true) for HTTP /chat when no explicit agent_ids are given."""
    tid = (tenant_id or "").strip()
    if not tid:
        return []
    lim = max(1, min(int(limit), 256))
    resp = (
        get_supabase()
        .table("users")
        .select("*")
        .eq("tenant_id", tid)
        .eq("is_agent", True)
        .limit(lim)
        .execute()
    )
    agents = resp.data or []
    logger.info("Loaded %d chat agent row(s) for tenant=%s", len(agents), tid)
    return agents


def _parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated string, stripping whitespace."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def parse_skills_csv(value: str | None) -> list[str]:
    """Public helper for parsing users.skills CSV values."""
    return _parse_csv(value)


# Skill-folder style slugs in user text (e.g. global-investment-analyzer).
_SKILL_SLUG_IN_TEXT = re.compile(
    r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b",
    flags=re.IGNORECASE,
)


def hinted_skill_slugs_from_query(
    text: str,
    *,
    allowed_skill_names: list[str] | None = None,
) -> list[str]:
    """Extract skill name candidates from the work-item text (kebab-case + allowed list matches).

    Used to steer the skill-discovery subagent and find_helpful_skills allowlists
    when the user names a skill directly or it appears in assigned agent skills.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    lower_blob = raw.lower()

    for name in allowed_skill_names or []:
        n = (name or "").strip()
        if not n:
            continue
        key = n.lower()
        if key in lower_blob or key.replace("_", "-") in lower_blob:
            if n not in seen:
                seen.add(n)
                out.append(n)

    for m in _SKILL_SLUG_IN_TEXT.finditer(raw):
        slug = m.group(0).strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)

    return out


def _build_description(agent_row: dict[str, Any], name: str) -> str:
    """Build subagent description from row description, persona, and goal."""
    base = agent_row.get("description") or f"Agent: {name}"
    parts = [base.strip()]
    persona = (agent_row.get("persona") or "").strip()
    if persona:
        parts.append(f"Persona: {persona}")
    goal = (agent_row.get("goal") or "").strip()
    if goal:
        parts.append(f"Goal: {goal}")
    return "\n\n".join(parts)


def _build_system_prompt(
    agent_row: dict[str, Any],
    query: str = "",
    tenant_id: str | None = None,
) -> str:
    """서브에이전트용 시스템 프롬프트(한글, 중복/충돌 정리 버전)."""
    parts: list[str] = []

    name = (
        agent_row.get("alias")
        or agent_row.get("username")
        or agent_row.get("name")
        or str(agent_row.get("id"))
    )
    role = agent_row.get("role") or "역할 미지정"
    base_desc = (agent_row.get("description") or "").strip()
    persona = (agent_row.get("persona") or "").strip()
    goal = (agent_row.get("goal") or "").strip()

    q = (query or "").strip()
    t_id = (tenant_id or "").strip() or "(unknown)"
    agent_skills = _parse_csv(agent_row.get("skills"))
    declared_tools = _parse_csv(agent_row.get("tools"))

    parts.append("=== 섹션 1: 역할/도메인 ===")
    parts.append(f"- 이름: {name}")
    parts.append(f"- 역할: {role}")
    if base_desc:
        parts.append(f"- 설명: {base_desc}")
    if persona:
        parts.append(f"- 페르소나: {persona}")
    if goal:
        parts.append(f"- 목표: {goal}")

    parts.append("\n" + format_execution_clock_section())

    if q:
        parts.append("\n=== 섹션 2: 현재 워크아이템 컨텍스트 ===")
        parts.append(
            "아래는 현재 워크아이템 쿼리([Description]/[Instruction]/[InputData])입니다. "
            "당신의 역할과 직접 관련된 부분을 중심으로 행동을 결정하세요.\n"
        )
        parts.append(q)

    parts.append("\n=== 섹션 3: 테넌트/스킬 경로 ===")
    parts.append(f"- tenant_id: {t_id}")
    parts.append(f"- 스킬 루트(마운트 볼륨): \"{SKILLS_ROOT_DIR}/{{tenant_id}}/\"")
    parts.append("- 이 tenant_id를 사용해 스킬/문서 경로를 해석하세요(다른 tenant 경로 접근 금지).")

    parts.append("\n=== 섹션 4: 실행 원칙(중요) ===")
    parts.append(
        "- 이 작업은 **실행 우선**입니다. 가능한 경우 샌드박스에서 실제 실행을 수행하고, 결과(stdout/stderr, exit code)를 보고하세요.\n"
        "- 스킬 스크립트가 있어도 기본은 **요청+InputData+스킬 문서를 반영한 태스크 전용 스크립트 합성 후 실행**입니다.\n"
        "- 원본 스킬 스크립트의 직접 실행은 (a) 목적이 요청과 정확히 일치하고 (b) 입력이 이미 준비되어 있고 (c) 파라미터 변환이 불필요할 때만 허용됩니다.\n"
        "- 오케스트레이터가 이번 `task()`를 **하나의 독립 산출**(예: 인벤토리 표, 파싱 JSON, 업로드된 파일 URL)로 정의했다면 범위를 넓히지 말고 그 산출만 완수하세요.\n"
    )

    parts.append("\n=== 섹션 4a: 지식/규칙/문서 도구(mem0/memento/dmn_rule) ===")
    parts.append(
        "- `mem0`: 개인화/누적 지식(선호, 과거 실행 정보, 사용자가 저장한 자격 증명 등). "
        "사람에게 묻기 전에 먼저 조회하는 것을 권장합니다.\n"
        "- `dmn_rule`: 정책/결재/자격/라우팅 등 규칙 기반 의사결정이 필요한 경우 사용합니다.\n"
        "- `memento`: **RAG**. 워크아이템·InputData·지시문에 **문서/파일 URL·스토리지 경로**가 있으면 추측하지 말고 "
        "`memento`로 **적극 조회**해 근거(발췌·출처)를 확보하세요. 오케스트레이터가 `write_todos`로 배정한 \"문서 확인\" 단계는 여기서 수행합니다.\n"
        "- pdf/xlsx/docx/pptx/csv/zip 등 **바이너리·복합 문서**는 `memento`만으로 부족할 수 있으므로, "
        "할당 스킬(SKILL.md)·`read_file`/샌드박스(다운로드·파싱·추출 스크립트)를 조합해 **실제 내용을 읽은 뒤** 결론을 내세요.\n"
        "- 위 도구들이 유의미하면 MCP/무거운 실행 전에 먼저 호출해 파라미터/리스크 결정을 보정하세요.\n"
    )

    parts.append("\n=== 섹션 4b: 스킬 해석 3티어(엄격한 순서) ===")
    parts.append(
        "- 티어1(볼륨/FS): `skill_volume_probe` → `ls/read_file/glob` → 필요 시 `sandbox_copy_mounted_skill`\n"
        "- 티어2(claude-skills MCP): 티어1에서 파일 누락/빈 복사 등으로 실패했을 때만 MCP로 탐색/문서 확인\n"
        "- 티어3(샌드박스 폴백): 티어1·2로 실행 가능한 스킬을 못 찾았지만 코드 실행이 필요한 경우, "
        "`sandbox_write_and_run`로 태스크 전용 스크립트를 작성·실행\n"
        "- 티어 전환 시 `report_skill_resolution_tier(1|2|3, started|success|failed, detail)`를 호출해 기록하세요.\n"
    )

    if agent_skills:
        parts.append("\n=== 섹션 4c: 할당 스킬 ===")
        parts.append(f"- 할당 스킬: {', '.join(agent_skills)}")
        parts.append(
            "- 기본 문서 경로 템플릿(경로를 발명하지 마세요): "
            f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/{{skill_name}}/SKILL.md\""
        )
    else:
        parts.append("\n=== 섹션 4c: 할당 스킬 ===")
        parts.append("- 할당 스킬: (명시 없음)")
        parts.append(
            f"- \"{SKILLS_ROOT_DIR}/{{tenant_id}}/\" 아래를 탐색해 가장 관련 높은 스킬 폴더를 선택하세요."
        )

    parts.append("\n=== 섹션 5: 보고 형식 ===")
    parts.append(
        "- 오케스트레이터가 합성할 수 있게 **간결하고 검증 가능한 결과**를 반환하세요.\n"
        "- 실행이 있었다면: 사용한 도구/명령, exit code, stdout/stderr 핵심, 생성/수정된 파일(있다면) 요약을 포함하세요.\n"
        "- 조회/조사 성격이면: source, endpoint_or_series, as_of, value, unit 같은 증거 필드를 포함하세요.\n"
        "- 실패 시에도 도구 출력(에러 메시지)을 숨기지 말고, 최소 1회 이상 오류 기반 보정 재시도 후 상태를 보고하세요.\n"
    )

    if declared_tools:
        parts.append("\n=== 섹션 6: 선언된 도구(참고) ===")
        parts.append("- " + ", ".join(declared_tools))

    return "\n".join(parts)


def build_subagent_configs(
    agent_rows: list[dict[str, Any]],
    all_tools: list[Any] | None = None,
    query: str = "",
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build deepagents subagent dicts from DB rows.

    all_tools defaults to [] (no MCP/process-gpt tool loading). query is the todolist
    user request, included in each subagent system prompt. Assigned skill names are
    passed in the prompt; subagents load skill content at runtime from mounted
    filesystem paths (tenant-scoped).
    """
    if all_tools is None:
        all_tools = []
    # NOTE:
    # 도구(MCP 클라이언트 포함)는 루트에서 1회 로드 후 서브에이전트가 재사용한다.
    # 서브에이전트에는 "요구사항에 맞는 최소 도구셋"만 필터링해서 붙인다.
    all_tools = list(all_tools) + list(get_sandbox_tools()) + list(get_skill_resolution_tools())
    tools_map: dict[str, Any] = {getattr(t, "name", str(t)): t for t in all_tools}
    server_to_tools: dict[str, list[Any]] = {}
    for t in all_tools:
        server = getattr(t, "_mcp_server", None)
        if server:
            server_to_tools.setdefault(server, []).append(t)
    sandbox_names = _sandbox_tool_names()
    skill_resolution_names = _skill_resolution_tool_names()

    subagents: list[dict[str, Any]] = []
    for row in agent_rows:
        display_name = row.get("alias") or row.get("username") or str(row.get("id"))
        # IMPORTANT: deepagents/langchain may propagate agent identifiers into messages[*].name.
        # When routing through LiteLLM->OpenAI, invalid names (spaces) cause 400.
        fallback = str(row.get("id") or "agent")
        name = _openai_safe_agent_name(str(display_name), fallback=fallback)
        description = _build_description(row, display_name)
        system_prompt = _build_system_prompt(row, query=query, tenant_id=tenant_id)

        agent_type = str(row.get("agent_type") or "").strip()
        is_a2a = agent_type.lower() == "a2a"

        # users.tools 컬럼 해석:
        # - 일반 서브에이전트: MCP 서버명 목록
        # - a2a 서브에이전트: "도구명" 목록 (서버명이 아님)
        declared_tool_names = _parse_csv(row.get("tools"))
        declared_skill_names = _parse_csv(row.get("skills"))
        # computer-use 같은 가상 툴 이름은 무시한다.
        agent_tool_or_server_names = [t for t in declared_tool_names if t not in VIRTUAL_TOOL_NAMES]
        # 기본 정책:
        # - 일반 서브에이전트: users.tools에 명시된 MCP 서버 + 기본 claude-skills
        # - a2a 서브에이전트: users.tools에 명시된 MCP 서버만 (그 외 모든 도구 부착 금지)
        wanted_servers_norm: set[str] = set()
        baseline_servers = [] if is_a2a else list(DEFAULT_MCP_SERVER_NAMES)
        for s in [*agent_tool_or_server_names, *baseline_servers]:
            key = str(s or "").strip()
            if not key:
                continue
            norm = key.lower().replace("_", "-")
            if norm in _BLOCKED_SUBAGENT_MCP_SERVERS:
                continue
            wanted_servers_norm.add(norm)

        # IMPORTANT:
        # Subagents reuse already-loaded tool instances (root-loaded).
        # job_id separation is handled by executor-side instrumentation/contextvars, not by copying tools.
        filtered_tools: list[Any] = []
        seen_ids: set[int] = set()

        def _server_matches(tool_server: str, wanted_norm: set[str]) -> bool:
            # TaggingSafeToolLoader 가 붙이는 _mcp_server 키는 테넌트 설정에 따라 alias가 있을 수 있어
            # canonical/endswith 매칭으로 흡수한다.
            norm = str(tool_server or "").strip().lower().replace("_", "-")
            if not norm:
                return False
            if norm in wanted_norm:
                return True
            return any(norm.endswith(w) for w in wanted_norm)

        # 1차: MCP 서버 필터링 (users.tools + 기본 claude-skills)
        for server_key, tools in server_to_tools.items():
            if not _server_matches(str(server_key), wanted_servers_norm):
                continue
            for t in tools:
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))

        # a2a 에이전트는 users.tools에 있는 도구만 사용한다.
        # 따라서 아래 "기본 도구" 부착 단계(로컬 3개/샌드박스/스킬 해석)는 모두 스킵한다.
        if is_a2a:
            # a2a의 users.tools는 "도구명"이므로 tools_map에서 직접 매칭한다.
            filtered_tools = []
            seen_ids = set()
            for tn in agent_tool_or_server_names:
                t = tools_map.get(tn)
                if t is None:
                    continue
                # work-assistant는 루트 전용 정책 유지(서브에이전트에는 차단).
                srv = str(getattr(t, "_mcp_server", "") or "").strip().lower().replace("_", "-")
                if srv and srv in _BLOCKED_SUBAGENT_MCP_SERVERS:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))

            subagent: dict[str, Any] = {
                "name": name,
                "description": description,
                "system_prompt": system_prompt,
                "tools": filtered_tools,
                "capabilities": {
                    "skills": list(declared_skill_names),
                    "tools": list(declared_tool_names),
                    "agent_type": agent_type,
                },
            }
            model_raw = row.get("model")
            model: str | None = None
            if model_raw is not None and str(model_raw).strip():
                model = normalize_litellm_slash_model_name(str(model_raw).strip())
            if model:
                subagent["model"] = model
                try:
                    caps = subagent.get("capabilities")
                    if isinstance(caps, dict):
                        caps["model"] = model
                except Exception:
                    pass
            subagents.append(subagent)
            logger.info(
                "Built subagent config (a2a strict): name=%s tools=%d model=%s",
                name,
                len(filtered_tools),
                model or "(inherit)",
            )
            continue

        # 2b차: 테넌트 컨텍스트 지식/기억/규칙 도구(mem0/memento/dmn_rule)는
        #      agent_rows[*].tools 에 명시되지 않아도 "항상" 서브에이전트에 부착한다.
        #
        # CRITICAL 원칙:
        # - 로컬 도구는 루트에서 1회 로드된 인스턴스를 재사용한다. (서브에이전트마다 새로 로드 금지)
        # - 서브에이전트별 user_id만 바꿔서 동일 인스턴스(내부 클라이언트/스토어)를 재사용한다.
        agent_uid = str(row.get("id") or "").strip()
        local_added = 0
        if agent_uid:
            for tn in ALWAYS_ATTACH_LOCAL_TOOL_NAMES:
                base = tools_map.get(tn)
                if base is None:
                    continue
                # 얕은 복사로 user_id만 바인딩(내부 클라이언트/스토어는 공유)
                t = _clone_tool_for_user_id(base, agent_uid)
                t = _wrap_tools_langchain([t])[0]
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    local_added += 1
        if local_added > 0:
            logger.info(
                "Attached per-subagent tenant context tools %s to agent '%s' (agent_uid=%s, added=%d, total_tools=%d)",
                ", ".join(ALWAYS_ATTACH_LOCAL_TOOL_NAMES),
                name,
                agent_uid,
                local_added,
                len(filtered_tools),
            )

        # 3차: 샌드박스 도구는 "모든" 서브 에이전트에 기본 포함한다.
        sandbox_added = 0
        if sandbox_names:
            for tn in sorted(sandbox_names):
                t = tools_map.get(tn)
                if t is None:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    sandbox_added += 1
        if sandbox_added > 0:
            logger.info(
                "Attached default sandbox tools to agent '%s' (added=%d, total_tools=%d)",
                name,
                sandbox_added,
                len(filtered_tools),
            )

        # 4차: 스킬 해석/볼륨 탐색 도구(find_helpful_skills 등과 별개)도 기본 포함한다.
        # (요구사항에 제거 요청이 없으므로 기존 동작 유지)
        sr_added = 0
        if skill_resolution_names:
            for tn in sorted(skill_resolution_names):
                t = tools_map.get(tn)
                if t is None:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    sr_added += 1
        if sr_added > 0:
            logger.info(
                "Attached skill resolution tools to agent '%s' (added=%d, total_tools=%d)",
                name,
                sr_added,
                len(filtered_tools),
            )

        subagent: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "tools": filtered_tools,
            # Capability metadata: routing must NOT depend on agent name/role.
            # Use only DB-stored capabilities (skills/tools/model) + attached tool names.
            "capabilities": {
                "skills": list(declared_skill_names),
                "tools": list(declared_tool_names),
                "agent_type": agent_type,
            },
        }

        model_raw = row.get("model")
        model: str | None = None
        if model_raw is not None and str(model_raw).strip():
            model = normalize_litellm_slash_model_name(str(model_raw).strip())
        if model:
            subagent["model"] = model
            try:
                caps = subagent.get("capabilities")
                if isinstance(caps, dict):
                    caps["model"] = model
            except Exception:
                pass

        subagents.append(subagent)
        logger.info(
            "Built subagent config: name=%s tools=%d model=%s",
            name,
            len(filtered_tools),
            model or "(inherit)",
        )

    return subagents
