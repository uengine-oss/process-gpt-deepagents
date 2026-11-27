from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# MCP 로드 후 임시로 제거할 도구 이름들.
# (bpmn-process-generation 스킬 흐름과 충돌하므로 비활성화)
_DISABLED_MCP_TOOLS: set[str] = {
    # 지금 연결하려는 bpmn-process-generation 스킬로 대체되므로 비활성화.
    "create_consulting_process_workitem",
    "create_pdf2bpmn_workitem",
}


# 요청마다 숨길 MCP 도구 이름(요청 metadata.hidden_tools). 클라이언트가 그 대화에서 쓰지 않을 도구를 이름으로 준다 —
# Process GPT Lite 는 회사 공용 발송 계정으로 메일을 보내는 도구(send_email)를 숨기고, 사람의 메일은 그 사람이 연결한
# 메일함에서 「보낼 메일」 카드의 보내기로만 나가게 한다(Lite mcp/mail.js send_mail_as_me). 채팅 루트·업무 지정·에이전트
# 프로필·서브에이전트의 MCP 도구가 모두 `_load_mcp_tools_grouped` 를 거치므로 여기서 한 번 거른다. 요청이 주지 않으면 빈 집합
# (본판 채팅 · 업무 실행은 그대로).
_HIDDEN_MCP_TOOLS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("hidden_mcp_tools", default=frozenset())
# 요청마다 통째로 숨길 MCP 서버 이름(요청 metadata.hidden_tool_servers) — 도구 이름을 다 알 수 없는 서버(회사 공용 메일함의
# gmail-mcp 등)는 서버째로 뺀다.
_HIDDEN_MCP_SERVERS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("hidden_mcp_servers", default=frozenset())
# 이 요청 사용자의 토큰 — 프롬프트에 싣지 않고, 도구가 인자(`user_jwt`)로 받는 MCP 도구에만 서버가 채워 넣는다.
_REQUEST_USER_JWT: contextvars.ContextVar[str] = contextvars.ContextVar("request_user_jwt", default="")
_HIDDEN_MAX = 50
# 도구 이름 앞에 서버 이름이 붙는 꼴(`<server>_<tool>`, `<server>__<tool>`, `<server>.<tool>` …)도 같은 도구로 본다
_PREFIX_SEPARATORS = ("_", ".", "-", "/", ":")


def _names(metadata: Any, key: str) -> frozenset[str]:
    raw = metadata.get(key) if isinstance(metadata, dict) else None
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(n).strip() for n in raw[:_HIDDEN_MAX] if isinstance(n, str) and str(n).strip())


def hidden_tool_names(metadata: Any) -> frozenset[str]:
    """요청 metadata 의 `hidden_tools`(도구 이름 목록) → 이름 집합. 목록이 아니거나 이름이 아닌 값은 버린다."""
    return _names(metadata, "hidden_tools")


def hidden_server_names(metadata: Any) -> frozenset[str]:
    """요청 metadata 의 `hidden_tool_servers`(MCP 서버 이름 목록) → 이름 집합."""
    return _names(metadata, "hidden_tool_servers")


def set_hidden_tools(names: frozenset[str] | set[str] | list[str] | None) -> contextvars.Token:
    """이 요청(현재 async 컨텍스트)에서 숨길 MCP 도구 이름을 정한다. 돌려받은 토큰으로 되돌린다."""
    return _HIDDEN_MCP_TOOLS.set(frozenset(names or ()))


def reset_hidden_tools(token: contextvars.Token) -> None:
    _HIDDEN_MCP_TOOLS.reset(token)


def set_hidden_servers(names: frozenset[str] | set[str] | list[str] | None) -> contextvars.Token:
    return _HIDDEN_MCP_SERVERS.set(frozenset(names or ()))


def reset_hidden_servers(token: contextvars.Token) -> None:
    _HIDDEN_MCP_SERVERS.reset(token)


def set_request_user_jwt(jwt: str | None) -> contextvars.Token:
    return _REQUEST_USER_JWT.set(str(jwt or ""))


def reset_request_user_jwt(token: contextvars.Token) -> None:
    _REQUEST_USER_JWT.reset(token)


def is_hidden_name(name: str, hidden: frozenset[str]) -> bool:
    """숨길 이름 그대로이거나, 앞에 서버 이름이 붙은 꼴(`gmail-mcp_send_email`)이면 참."""
    if not name or not hidden:
        return False
    if name in hidden:
        return True
    return any(name.endswith(sep + h) for h in hidden for sep in _PREFIX_SEPARATORS)


def hide_tools(tools: list, hidden: frozenset[str] | None = None) -> list:
    """도구 목록에서 숨길 이름을 뺀다(hidden 을 주지 않으면 이 요청에서 정한 이름)."""
    names = _HIDDEN_MCP_TOOLS.get() if hidden is None else hidden
    if not names:
        return tools
    return [t for t in tools if not is_hidden_name(getattr(t, "name", ""), names)]


# 도구가 인자로 받는 사용자 자격증명. 모델이 값을 알 필요가 없도록 모델에 보이는 스키마에서 빼고, 부를 때 서버가 이 요청의
# 값을 넣는다(예: process-gpt-mcp 의 execute_process(user_jwt=…)). 자격증명은 프롬프트에 싣지 않는다.
_CREDENTIAL_ARGS = ("user_jwt",)


def inject_user_credentials(tool: Any, jwt: str) -> Any:
    """`user_jwt` 인자를 받는 MCP 도구를, 그 인자를 모델에게서 숨기고 서버가 채우는 도구로 바꾼다. 해당 없으면 그대로."""
    schema = getattr(tool, "args_schema", None)
    coroutine = getattr(tool, "coroutine", None)
    if not isinstance(schema, dict) or coroutine is None:
        return tool
    props = schema.get("properties") or {}
    keys = [k for k in _CREDENTIAL_ARGS if k in props]
    if not keys:
        return tool
    from typing import Annotated

    from langchain_core.tools import InjectedToolArg, StructuredTool

    shown = {**schema, "properties": {k: v for k, v in props.items() if k not in keys}}
    if isinstance(schema.get("required"), list):
        shown["required"] = [r for r in schema["required"] if r not in keys]

    async def call(runtime: Annotated[object | None, InjectedToolArg()] = None, **arguments: Any):
        return await coroutine(runtime=runtime, **{**arguments, **{k: jwt for k in keys}})

    return StructuredTool(
        name=tool.name, description=tool.description or "", args_schema=shown, coroutine=call,
        response_format=getattr(tool, "response_format", "content_and_artifact"), metadata=getattr(tool, "metadata", None),
    )


def is_mcp_server_enabled(server_config: Any) -> bool:
    """설정 화면의 켜기/끄기. 값이 없으면 켜진 것으로 본다(화면과 같은 기본값)."""
    return isinstance(server_config, dict) and server_config.get("enabled", True) is not False


_USER_PLACEHOLDERS = ("{{user_jwt}}", "{{user_email}}")
# 이 요청의 대화 id — 도구 서버가 결과를 어느 대화에 놓을지 정할 때 쓴다(Lite 의 「보낼 메일」 카드: X-Conversation-Id).
# 값이 없어도(스킬 도구 카탈로그처럼 대화가 없는 호출) 서버를 빼지 않고 빈 값으로 둔다.
_OPTIONAL_PLACEHOLDERS = ("{{conversation_id}}",)


def resolve_user_placeholders(tenant_mcp: dict, user_jwt: str | None, user_email: str | None, conversation_id: str | None = None) -> dict:
    """mcpServers 의 headers·env 에 있는 {{user_jwt}}·{{user_email}} 을 요청 사용자 값으로, {{conversation_id}} 를 이 요청의
    대화 id 로 바꾼다.

    사용자별 자격증명을 쓰는 MCP 서버(예: 사용자 OAuth 토큰을 쥔 게이트웨이)를 테넌트 설정 하나로
    등록할 수 있게 한다. 값이 없어 치환하지 못한 서버는 잘못된 인증으로 호출하지 않도록 뺀다.
    자리표시자가 없는 서버는 그대로 둔다. {{conversation_id}} 는 값이 없으면 빈 값으로 바꾸고 서버를 빼지 않는다.
    """
    servers = (tenant_mcp or {}).get("mcpServers")
    if not isinstance(servers, dict):
        return tenant_mcp
    values = {"{{user_jwt}}": user_jwt or "", "{{user_email}}": user_email or "", "{{conversation_id}}": conversation_id or ""}

    def sub(v):
        if not isinstance(v, str):
            return v
        for k, val in values.items():
            v = v.replace(k, val)
        return v

    out = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            out[name] = cfg
            continue
        blob = json.dumps({k: cfg.get(k) for k in ("headers", "env")}, ensure_ascii=False)
        used = [k for k in _USER_PLACEHOLDERS if k in blob]
        optional = [k for k in _OPTIONAL_PLACEHOLDERS if k in blob]
        if not used and not optional:
            out[name] = cfg
            continue
        if any(not values[k] for k in used):
            logger.info("[MCP] skip %s: needs %s but the request has none", name, used)
            continue
        cfg = dict(cfg)
        for key in ("headers", "env"):
            if isinstance(cfg.get(key), dict):
                cfg[key] = {k: sub(v) for k, v in cfg[key].items()}
        out[name] = cfg
    return {**tenant_mcp, "mcpServers": out}


def _mcp_connection_config(server_config: dict) -> dict:
    """tenant_mcp mcpServers 항목을 langchain-mcp-adapters Connection 형식으로 변환."""
    if "command" in server_config:
        conn: dict = {
            "transport": "stdio",
            "command": server_config["command"],
            "args": list(server_config.get("args") or []),
        }
        if server_config.get("env"):
            conn["env"] = dict(server_config["env"])
        return conn

    transport_map = {
        "http": "streamable_http",
        "streamable_http": "streamable_http",
        "sse": "sse",
        "stdio": "stdio",
        "websocket": "websocket",
    }
    conn = {
        "url": server_config.get("url", ""),
        "transport": transport_map.get(server_config.get("type", "http"), "streamable_http"),
    }
    if server_config.get("headers"):
        conn["headers"] = server_config["headers"]
    return conn


async def _load_mcp_tools_grouped(
    server_names: list[str],
    tenant_mcp: dict,
    tool_filters: dict[str, list[str]] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, list]:
    """지정된 MCP 서버들에서 LangChain BaseTool 목록을 서버별로 로드한다.

    반환값은 실제로 그 도구를 제공한 서버 이름 -> BaseTool 목록. 여러 서버를 한 번에
    로드해도 각 도구가 어느 서버에서 왔는지 유실되지 않도록, 서버별로 개별 조회한다
    (chat_room context 의 connectors 를 정확히 귀속시키기 위해 필요).

    tool_filters 가 주어지면 서버별 허용 도구 이름 목록으로 추가 필터링한다
    (해당 서버 키가 없거나 빈 리스트면 그 서버는 전체 허용).

    errors 를 넘기면 설정이 없거나 불러오기에 실패한 서버의 이유를 서버 이름으로 담는다.

    설정 화면에서 끈 서버(`enabled: false`)는 불러오지 않는다 — 채팅 루트·업무 지정·서브에이전트·
    스킬 도구 카탈로그가 모두 여기를 거친다. 끈 것은 실패가 아니므로 errors 에 담지 않는다.
    """
    mcp_servers = tenant_mcp.get("mcpServers", {})
    connections = {}
    hidden_servers = _HIDDEN_MCP_SERVERS.get()
    for name in server_names:
        if name in hidden_servers:
            logger.info("MCP 서버 요청이 숨김, 건너뜀: %s", name)
        elif name in mcp_servers and not is_mcp_server_enabled(mcp_servers[name]):
            logger.info("MCP 서버 꺼짐, 건너뜀: %s", name)
        elif name in mcp_servers:
            connections[name] = _mcp_connection_config(mcp_servers[name])
        else:
            logger.warning("MCP 서버 설정 없음: %s (사용 가능: %s)", name, list(mcp_servers))
            if errors is not None:
                errors[name] = "MCP 서버 설정 없음"

    if not connections:
        return {}

    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient(connections)

    async def _load_one(name: str) -> tuple[str, list]:
        try:
            tools = await client.get_tools(server_name=name)
            if _DISABLED_MCP_TOOLS:
                before = len(tools)
                tools = [t for t in tools if getattr(t, "name", "") not in _DISABLED_MCP_TOOLS]
                removed = before - len(tools)
                if removed:
                    logger.info("MCP 도구 임시 제거: %d개 %s (server=%s)", removed, sorted(_DISABLED_MCP_TOOLS), name)
            hidden = _HIDDEN_MCP_TOOLS.get()
            if hidden:
                before = len(tools)
                tools = hide_tools(tools, hidden)
                if len(tools) != before:
                    logger.info("MCP 도구 요청이 숨김: %d개 %s (server=%s)", before - len(tools), sorted(hidden), name)
            jwt = _REQUEST_USER_JWT.get()
            if jwt:
                tools = [inject_user_credentials(t, jwt) for t in tools]
            allowed = (tool_filters or {}).get(name)
            if allowed:
                before = len(tools)
                tools = [t for t in tools if getattr(t, "name", "") in allowed]
                if len(tools) != before:
                    logger.info("MCP 도구 허용목록 적용: %s → %d개 (server=%s)", allowed, len(tools), name)
            return name, tools
        except Exception as e:
            logger.error("MCP tools 로드 실패 (%s): %s", name, e)
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"[:500]
            return name, []

    results = await asyncio.gather(*(_load_one(name) for name in connections))
    grouped = {name: tools for name, tools in results if tools}
    logger.info(
        "MCP tools 로드 완료: %s → %d개", list(connections),
        sum(len(t) for t in grouped.values()),
    )
    return grouped


def ensure_process_gpt_mcp(tenant_mcp: dict, tenant_id: str = "") -> dict:
    """SUPABASE_* 환경 변수가 있으면 process-gpt-mcp MCP를 mcpServers에 주입한다.

    이미 process-gpt-mcp가 있거나 SUPABASE_URL/KEY가 없으면 원본을 그대로 반환한다.
    """
    if not isinstance(tenant_mcp, dict):
        tenant_mcp = {}
    mcp_servers = dict(tenant_mcp.get("mcpServers") or {})
    if "process-gpt-mcp" in mcp_servers:
        return tenant_mcp

    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_anon = (
        os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SERVICE_ROLE_KEY")
        or ""
    ).strip()
    if not (sb_url and sb_anon):
        return tenant_mcp

    wa_env: dict[str, str] = {"SUPABASE_URL": sb_url, "SUPABASE_ANON_KEY": sb_anon}
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if jwt_secret:
        wa_env["SUPABASE_JWT_SECRET"] = jwt_secret

    mcp_servers["process-gpt-mcp"] = {
        "command": "uvx",
        "args": ["process-gpt-mcp"],
        "env": wa_env,
    }
    logger.info("process-gpt-mcp MCP 주입 | tenant_id=%s", tenant_id)
    return {**tenant_mcp, "mcpServers": mcp_servers}


_LOCAL_BPM_TOOLS = frozenset({
    "get_current_user",
    "get_process_list",
    "get_process_detail",
    "get_form_fields",
    "get_instance_list",
    "get_todolist",
    "get_organization",
})


async def load_chat_root_tools(
    tenant_mcp: dict, errors: dict[str, str] | None = None
) -> tuple[list, dict[str, str]]:
    """채팅 모드 루트 오케스트레이터용 MCP 도구를 로드한다.

    tenants.mcp.mcpServers 에 등록된 모든 서버의 도구를 로드하며,
    로컬 BPM 도구와 중복되는 항목은 제외한다.

    Returns:
        (tools, tool_servers) — tool_servers 는 도구 이름 -> 실제로 그 도구를
        제공한 MCP 서버 이름 매핑이다(여러 서버가 등록돼 있어도 도구별로
        정확한 서버를 chat_room context 의 connectors 에 귀속시키기 위함).
    """
    server_names = list((tenant_mcp.get("mcpServers") or {}).keys())
    if not server_names:
        return [], {}
    try:
        grouped = await _load_mcp_tools_grouped(server_names, tenant_mcp, errors=errors)
        tools: list = []
        tool_servers: dict[str, str] = {}
        before = 0
        for server, srv_tools in grouped.items():
            before += len(srv_tools)
            for t in srv_tools:
                if t.name in _LOCAL_BPM_TOOLS:
                    continue
                tools.append(t)
                tool_servers[t.name] = server
        if before != len(tools):
            logger.info(
                "MCP 도구 중복 제거: %d개 → %d개 (로컬 BPM 도구 %d개 제외)",
                before, len(tools), before - len(tools),
            )
        logger.info("채팅 루트 MCP 도구: %d개 (%s)", len(tools), server_names)
        return tools, tool_servers
    except Exception:
        logger.warning("채팅 루트 MCP 도구 로드 실패", exc_info=True)
        return [], {}


# ---------------------------------------------------------------------------
# 공통 MCP 서버 — 테넌트마다 등록하지 않아도 모든 테넌트의 에이전트에 붙는 서버
# ---------------------------------------------------------------------------
# 운영자가 환경 변수로 준다. 모양은 tenants.mcp 의 mcpServers 와 같다(이름 → {url, type, headers, enabled, …}).
#   COMMON_MCP_SERVERS       JSON 문자열
#   COMMON_MCP_SERVERS_FILE  그 JSON 을 담은 파일 경로(ConfigMap 을 마운트한 자리)
# 둘 다 있으면 합치고, 이름이 겹치면 COMMON_MCP_SERVERS 쪽이 이긴다. {"mcpServers": {...}} 로 감싸도 된다.
# 한 번만 파싱한다 — 문자열은 값이 바뀔 때만, 파일은 수정 시각·크기가 바뀔 때만 다시 읽는다.
# 잘못된 JSON 은 내용 없이 경고 한 줄만 남기고 무시한다(헤더에 비밀이 들어 있을 수 있다).

COMMON_MCP_ENV = "COMMON_MCP_SERVERS"
COMMON_MCP_FILE_ENV = "COMMON_MCP_SERVERS_FILE"

_common_lock = threading.Lock()
_common_env_cache: tuple[str, dict[str, dict]] | None = None
_common_file_cache: tuple[tuple, dict[str, dict]] | None = None

# 이 키가 없는 테넌트 항목은 연결을 새로 정하지 않는다 — 같은 이름의 공통 항목 위에 덮어쓴다.
_CONNECTION_KEYS = ("url", "command")


def _parse_common_servers(raw: str, source: str) -> dict[str, dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("[MCP] %s is not valid JSON; common MCP servers from it are ignored", source)
        return {}
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    if not isinstance(data, dict):
        logger.warning("[MCP] %s must be a JSON object (server name -> config); ignored", source)
        return {}
    servers: dict[str, dict] = {}
    for name, cfg in data.items():
        if isinstance(cfg, dict) and str(name).strip():
            servers[str(name)] = cfg
        else:
            logger.warning("[MCP] %s: entry %r is not an object; ignored", source, name)
    return servers


def _common_from_env() -> dict[str, dict]:
    global _common_env_cache
    raw = os.environ.get(COMMON_MCP_ENV, "").strip()
    if not raw:
        return {}
    cached = _common_env_cache
    if cached is not None and cached[0] == raw:
        return cached[1]
    servers = _parse_common_servers(raw, COMMON_MCP_ENV)
    _common_env_cache = (raw, servers)
    return servers


def _common_from_file() -> dict[str, dict]:
    global _common_file_cache
    path = os.environ.get(COMMON_MCP_FILE_ENV, "").strip()
    if not path:
        return {}
    try:
        st = os.stat(path)  # ConfigMap 은 심볼릭 링크를 바꿔 갱신한다 — stat 은 링크를 따라간다.
        key: tuple = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    cached = _common_file_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    servers: dict[str, dict] = {}
    if key[1] is None:
        logger.warning("[MCP] %s=%s: file not found; common MCP servers from it are ignored", COMMON_MCP_FILE_ENV, path)
    else:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            logger.warning("[MCP] %s=%s: file unreadable; common MCP servers from it are ignored", COMMON_MCP_FILE_ENV, path)
        else:
            if raw.strip():
                servers = _parse_common_servers(raw, f"{COMMON_MCP_FILE_ENV} ({path})")
    _common_file_cache = (key, servers)
    return servers


def common_mcp_servers() -> dict[str, dict]:
    """운영자가 정한 공통 MCP 서버 `{이름: 설정}`. 캐시를 그대로 주므로 고치지 말 것."""
    with _common_lock:
        from_file = _common_from_file()
        from_env = _common_from_env()
    if not from_file:
        return from_env
    if not from_env:
        return from_file
    return {**from_file, **from_env}


def enabled_common_mcp_server_names() -> list[str]:
    """켜져 있는 공통 MCP 서버 이름(`GET /mcp/common`). 주소·헤더·환경 변수는 담지 않는다."""
    return sorted(name for name, cfg in common_mcp_servers().items() if is_mcp_server_enabled(cfg))


def merge_common_mcp_servers(tenant_mcp: dict) -> dict:
    """공통 MCP 서버를 테넌트 설정의 mcpServers 에 더한 새 설정을 돌려준다.

    - 같은 이름의 테넌트 항목이 이긴다. url·command 가 있는 항목은 공통 항목을 통째로 바꾼다.
    - url·command 가 없는 테넌트 항목은 공통 항목 위에 덮어쓴다. 그래서 `{"enabled": false}` 한 줄로
      그 테넌트만 공통 서버를 끈다(`is_mcp_server_enabled` 가 거른다).
    - 공통 항목은 사본으로 넣는다 — 뒤에서 설정을 고쳐도 캐시는 그대로다.
    - 공통 서버가 앞, 테넌트 항목이 뒤에 온다(도구 이름이 겹치면 뒤의 것이 남는다).
    """
    common = common_mcp_servers()
    if not common:
        return tenant_mcp
    if not isinstance(tenant_mcp, dict):
        tenant_mcp = {}
    own = tenant_mcp.get("mcpServers")
    own = own if isinstance(own, dict) else {}
    merged: dict[str, Any] = {name: copy.deepcopy(cfg) for name, cfg in common.items() if name not in own}
    for name, cfg in own.items():
        base = common.get(name)
        if base is not None and isinstance(cfg, dict) and not any(k in cfg for k in _CONNECTION_KEYS):
            merged[name] = {**copy.deepcopy(base), **cfg}
        else:
            merged[name] = cfg
    return {**tenant_mcp, "mcpServers": merged}


def fetch_tenant_mcp(tenant_id: str) -> dict:
    """테넌트의 MCP 설정(`tenants.mcp`)에 공통 MCP 서버와 process-gpt-mcp 기본 주입까지 더한 값을 돌려준다.

    채팅 실행(executor)과 스킬 도구 카탈로그가 같은 설정으로 도구를 불러오게 한곳에 둔다.
    조회에 실패하면 공통 서버·기본 주입만 한 설정을 돌려준다.
    `{{user_jwt}}`·`{{user_email}}` 은 아직 그대로다 — 부르는 쪽이 `resolve_user_placeholders` 로 바꾼다.
    """
    tenant_mcp: dict = {}
    if tenant_id:
        try:
            from core.storage.db import get_supabase

            result = get_supabase().table("tenants").select("mcp").eq("id", tenant_id).single().execute()
            fetched = (result.data or {}).get("mcp")
            if isinstance(fetched, dict):
                tenant_mcp = fetched
        except Exception:
            logger.warning("tenant_mcp DB 조회 실패 | tenant_id=%s", tenant_id, exc_info=True)
    return ensure_process_gpt_mcp(merge_common_mcp_servers(tenant_mcp), tenant_id)
