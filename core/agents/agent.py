import os
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.middleware import ToolRetryMiddleware
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
    register_skill_as_tenant,
    write_process_definition,
    update_process_definition,
    validate_process_definition,
    complete_process_generation,
    bpmn_container_base,
    canonical_process_dir_name,
    _current_sandbox as _skill_sandbox,
    _current_user_name as _skill_user_name,
    _current_user_email as _skill_user_email,
    _current_user_id as _skill_user_id,
)
from ..storage.storage import set_tenant_id as _set_storage_tenant_id
from ..bpm.tools import get_bpm_tools, set_tenant_id as _set_bpm_tenant_id

_BASE_DIR = Path(__file__).parent.parent.parent
_SKILLS_DIR = Path(os.getenv("SKILLS_HOST", str(_BASE_DIR / "skills")))
_SKILLS_CONTAINER_MOUNT = "/skills"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Use the available tools when needed to answer questions accurately. "
    "Reply in Korean unless the user writes in another language.\n\n"
    "## 작업 선택 규칙\n"
    "요청이 특정 스킬의 영역에 해당하면(예: 프로세스/워크플로/BPMN 생성 → bpmn-process-generation 스킬) "
    "generic 도구로 우회하지 말고 그 스킬의 SKILL.md 를 read_file 로 읽어 절차를 따른다. "
    "그 스킬이 이미 정의한 작업에 대해서는 조직도/사용자 조회, 토큰·테넌트 ID 요청 등을 별도로 하지 않는다 "
    "(필요한 값은 환경변수로 주입되며, 역할 등은 스킬 절차가 정한다). "
    "스킬과 무관한 단순 조회·일반 대화는 적절한 도구로 바로 처리한다.\n\n"
    "## 단독 스킬 생성 vs 프로세스 생성 판별 (최우선)\n"
    "사용자 문장에 '스킬'/'skill' 을 만들어 달라는 의도가 있으면, **파일/문서가 업로드되어 있어도 판단은 바뀌지 않는다** "
    "— 업로드 문서는 스킬이 참고할 지식일 뿐이며 프로세스 생성으로 바꾸지 않는다. "
    "이때 프로세스 생성 절차(컨설팅·process-definition.json·폼·DMN·에이전트·검증)를 시작하지 말고 "
    "`skill-creator` 서브에이전트에 `task()` 로 위임해 정식 스킬을 만든다. 업로드 문서 내용이 필요하면 "
    "`search_documents`(임베딩됨)로 읽어 스킬의 references/ 로 반영한다. 결과 파일은 반드시 **이번 대화 전용 산출물 "
    "경로(아래 '📁' 섹션의 base) 바로 아래 `skills/<safe-name>/SKILL.md`** 로 `write_file` 한다 "
    "(process-* 폴더는 만들지 않는다 — base 를 빼고 쓰지 말 것). "
    "완료하면 채팅엔 **'스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.'** 한 줄만 남긴다. "
    "아래 'BPMN/프로세스 생성' 절차는 사용자가 명시적으로 프로세스/워크플로/BPMN 을 만들라고 한 경우에만 적용된다.\n\n"
    "## BPMN/프로세스 생성\n"
    "사용자가 새 프로세스/워크플로/BPMN 생성을 요청하면 bpmn-process-generation 스킬의 SKILL.md 와 references/ 를 "
    "read_file 로 읽고 그 절차(컨설팅 → JSON 생성 → 스킬/에이전트/DMN 후보 선택 → 폼/참조정보 → 검증 → 완료)를 그대로 "
    "따른다. 이 환경에서 스킬 문서와 다르거나 스킬 문서에 없는 사항은 다음 세 가지뿐이다:\n"
    "- **업로드 문서는 이미 memento 에 임베딩되어 있다.** `pdf2markdown` 같은 변환 도구나 파일 직접 읽기 대신 "
    "`search_documents` 로 관련 키워드를 2~3회 검색해 본문 청크를 모은다(청크가 하나라도 나오면 그것으로 진행하고, "
    "`list_documents`/`read_document_page` 등 보조 도구의 실패는 무시). 모든 키워드에서 0건일 때만 "
    "'문서를 읽지 못했다'고 안내한다.\n"
    "- **1단계 컨설팅 초안의 `request_human_input` 텍스트는 반드시 자연어 문장 + 번호 목록(`1) … → 2) …`)만 쓴다.** "
    "줄 첫머리 `•`·`-`·`*` 불릿이나 `[제목]` 형태의 대괄호 머리글 줄은 쓰지 않는다 — 그 형식은 2단계(스킬·에이전트·DMN "
    "후보) 전용이며, 1단계에 쓰이면 프론트가 승인/반려 패널이 아니라 다중 선택 체크박스 패널로 오인해 렌더링한다.\n"
    "- **폼 파일 확장자는 `.form`** 이다(스킬 문서의 `.html` 이 아님) — `/workspace/.bpmn/forms/<activity_id>.form` "
    "로 저장한다.\n"
    "## MCP 도구 호출 규칙\n"
    "- **MCP 도구 호출이 에러(ToolException 등)로 실패하면, 그 에러 메시지를 읽고 원인(잘못된 domain 문법, "
    "날짜/시간 형식 등)을 스스로 진단해 인자를 고쳐 즉시 다시 호출한다.** 같은 인자로 무의미하게 반복하지 말고, "
    "에러 내용에 맞춰 값을 보정한다. 실패를 그대로 사용자에게 보고하고 멈추지 말 것 — 합리적인 보정을 몇 차례 "
    "시도해도 계속 실패할 때만 무엇이 실패했는지 사용자에게 알린다.\n"
)


def _build_tool_retry_middleware() -> ToolRetryMiddleware:
    """MCP 도구 호출 실패(ToolException 등) 시 예외로 실행을 중단시키는 대신,
    에러 내용을 ToolMessage 로 모델에 돌려줘 스스로 인자를 보정해 재시도하게 한다.
    (odoo-mcp aggregate_records/search_records 의 domain·datetime 형식 오류 등)
    """
    return ToolRetryMiddleware(max_retries=1, on_failure="continue")


async def build_agent(
    tenant_id: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_tools: list = None,
    checkpointer=None,
    subagents_raw: list = None,
    tenant_mcp: dict | None = None,
    middleware: list = None,
    static_subagents: list = None,
    conversation_id: str = "",
    user_context: dict | None = None,
    agent_mode: str = "",
):
    tools = (
        list(extra_tools or [])
        + ([] if agent_mode == "DRAFT" else get_hitl_tools())
        + get_memento_tools()
        + get_bpm_tools()
        + [
            checkout_to_workspace,
            extract_document_structure,
            list_git_repos,
            publish_skill,
            register_skill_as_tenant,
            write_process_definition,
            update_process_definition,
            validate_process_definition,
            complete_process_generation,
        ]
    )
    backend = get_or_create_sandbox(tenant_id, workspace_host=_BASE_DIR / "workspace", skills_host=_SKILLS_DIR)
    _skill_sandbox.set(backend)
    _skill_user_name.set((user_context or {}).get("user_name") or "")
    _skill_user_email.set((user_context or {}).get("user_email") or "")
    _skill_user_id.set((user_context or {}).get("user_id") or "")
    _set_storage_tenant_id(tenant_id)
    _set_bpm_tenant_id(tenant_id)
    _dynamic, _mcp_connectors, _mcp_tools_meta = await _build_subagents(
        subagents_raw or [], tenant_id, tenant_mcp, backend=backend
    )
    built_subagents = list(static_subagents or []) + _dynamic

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

    # 방별 산출물 디렉터리 주입 + 프로세스별 하위 폴더(한 방에 여러 프로세스 가능).
    # 폴더명은 executor 가 **구체적인 값**을 생성해 주입한다(에이전트가 `<랜덤 id>`·`<new_id>` 같은
    # placeholder 를 그대로 적어 `<`,`>` 가 든 Windows 비호환 폴더를 만들던 문제 방지).
    # HITL resume rebuilds the graph/agent. Keep the folder calculation shared
    # with validation/completion so stale random folders are never re-collected.
    _pfolder = canonical_process_dir_name(conversation_id)
    _bpmn_base = bpmn_container_base(conversation_id)
    final_prompt = final_prompt + (
        f"\n\n## 📁 이번 대화 전용 산출물 경로(필수)\n"
        f"이 대화의 산출물 기준 디렉터리는 **`{_bpmn_base}/`** 다. "
        f"**단독 스킬 생성**(프로세스가 아님)이면 프로세스 폴더 없이 **`{_bpmn_base}/skills/<safe-name>/SKILL.md`** 에 바로 만든다. "
        f"프로세스 생성이면 아래 규칙을 따른다: "
        f"**프로세스 폴더명은 반드시 `{_pfolder}` 를 그대로 사용한다**(즉 `{_bpmn_base}/{_pfolder}/`). "
        f"그 폴더 안에 `process-definition.json`·`forms/`·`skills/`·`agents.json`·`manifest.json` 을 모두 만든다. "
        f"**한 방에 여러 프로세스면** 두 번째부터는 `{_pfolder}-2`, `{_pfolder}-3` 처럼 **뒤에 번호만** 붙인다. "
        f"⚠️ 폴더명에 `<`, `>` 같은 문자나 '랜덤'·'id'·'new_id' 같은 placeholder 단어를 절대 쓰지 말 것(예: `process-<랜덤 id>` ❌). "
        f"SKILL.md/reference 가 `/workspace/.bpmn/...` 라고 해도 위 프로세스 폴더 경로로 바꿔 사용한다. "
        f"기존 파일은 그대로 **덮어쓰기**(덮어쓸지 묻지 말 것). "
        f"`validate_process_definition` 은 그 프로세스의 `process-definition.json` 경로를 인자로 넘겨 호출하고, "
        f"`complete_process_generation` 은 인자 없이 호출하면 이 대화의 모든 `process-*/` 폴더를 자동으로 모아 처리한다."
    )

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
    kwargs["middleware"] = [_build_tool_retry_middleware(), *(middleware or [])]

    return create_deep_agent(**kwargs), _mcp_connectors, _mcp_tools_meta
