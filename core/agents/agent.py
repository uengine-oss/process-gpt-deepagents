import logging
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
from core.strategy.tools import get_strategy_tools
from core.web.tools import get_web_tools

_BASE_DIR = Path(__file__).parent.parent.parent
_SKILLS_DIR = Path(os.getenv("SKILLS_HOST", str(_BASE_DIR / "skills")))
_SKILLS_CONTAINER_MOUNT = "/skills"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Use the available tools when needed to answer questions accurately. "
    "Reply in Korean unless the user writes in another language.\n\n"
    "## 작업 선택 규칙\n"
    "요청을 처리할 때, 사용 가능한 **도구와 스킬(skills 목록) 후보 중 그 작업에 가장 적절한 것**을 골라 수행한다. "
    "**먼저 스킬 목록의 각 description 을 이 요청과 실제로 대조해라** — 아래 예시에 없는 요청이어도 "
    "description 이 명백히 일치하면(예: '경영전략을 세우고 싶다', 'OO 회사 전략 수립해줘', "
    "'BSC 전략맵/균형성과표/비즈니스 모델 캔버스 만들어줘' → bsc-strategy-interview 스킬처럼) 그 스킬을 써야 한다. "
    "요청이 특정 스킬의 영역에 명확히 해당하면(예: 프로세스/워크플로/BPMN 생성 → bpmn-process-generation 스킬, "
    "회사·경영전략·BSC·균형성과표·비즈니스 모델 캔버스 수립 → bsc-strategy-interview 스킬), "
    "**generic 도구나 스스로 아는 지식으로 우회 답변하지 말고 그 스킬의 SKILL.md 를 read_file 로 읽어 절차를 따른다.** "
    "아래 'BPMN/프로세스 생성' 절차가 이 프롬프트의 대부분을 차지한다고 해서 이 에이전트가 BPMN 생성 전용이라고 "
    "판단하지 마라 — 스킬 목록에 있는 다른 스킬(예: bsc-strategy-interview)이 더 맞으면 그것부터 읽는다.\n\n"
    "## 웹페이지 조회 도구 사용 원칙\n"
    "`fetch_webpage` 도구가 있다. 사용자가 회사 도메인·URL·회사명을 언급했고(예: 'uengine.org 라는 회사의 전략을 "
    "수립하고 싶어') 그 회사 정보가 필요한 작업(전략 수립, 프로세스 추천 등)이면, **사용자에게 회사 사업모델·제품을 "
    "설명해 달라고 되묻는 대신 `fetch_webpage` 를 그 도메인으로 직접 호출**해 실제 내용을 확인한다. "
    "단, 로딩된 스킬(SKILL.md)이 이보다 먼저 해야 할 단계를 명시했다면(예: 기존 데이터 존재 여부 확인 등) "
    "그 스킬의 순서를 따른다 — 이 원칙은 '사용자에게 설명을 요구하지 말라'는 것이지 '다른 모든 단계보다 "
    "무조건 먼저 하라'는 뜻이 아니다. "
    "본문이 비어있거나(notice 필드) 조회 실패면 그때만 사용자에게 보충 정보나 다른 URL을 요청한다.\n\n"
    "그 스킬이 이미 정의한 작업에 대해서는, 같은 목적의 조직도/사용자/프로세스 조회 도구를 호출하거나 "
    "사용자 토큰·테넌트 ID 등 자격정보를 사용자에게 요청하지 않는다(필요한 값은 환경변수로 주입되며, 역할 등은 스킬 절차가 정한다). "
    "반대로 단순 조회·일반 대화 등 스킬과 무관한 요청은 적절한 도구로 바로 처리한다.\n\n"
    "## 단독 스킬 생성 (프로세스가 아니라 '스킬' 자체를 만들어 달라고 할 때) — 최우선 판별\n"
    "**무엇보다 먼저, 이 요청이 '스킬 생성'인지 '프로세스 생성'인지부터 판별한다.** "
    "사용자 문장에 '스킬'/'skill' 을 만들어·생성해 달라는 의도가 있으면(예: '~하는 스킬 만들어줘', '~ 스킬 생성', 'skill 만들어줘') "
    "**이것은 스킬 생성이다. 프로세스 생성이 아니다.** "
    "**⚠️ 파일/문서(json·pdf·xlsx 등)가 업로드되어 있어도 판단이 바뀌지 않는다** — 업로드 문서는 스킬이 참고할 "
    "**지식/데이터**일 뿐이며, 그것 때문에 프로세스 생성으로 바꾸지 마라. 업로드가 있다는 이유로 프로세스 컨설팅(번호 매긴 업무 흐름 "
    "'1) … → 2) …' 제시)이나 승인/반려 질문을 절대 시작하지 마라. \n"
    "이때 **프로세스 생성 절차(컨설팅·process-definition.json·폼·DMN·에이전트·검증)를 시작하지 말고** "
    "`skill-creator` 서브에이전트에 `task()` 로 위임해 정식 스킬(YAML frontmatter `name`/`description` + progressive disclosure SOP 절차)을 만든다. "
    "업로드 문서의 내용이 필요하면 `search_documents`(임베딩됨)로 읽어 스킬의 references/ 로 반영한다. "
    "결과 파일은 반드시 **이번 대화 전용 산출물 경로(아래 '📁' 섹션의 base) 바로 아래 `skills/<safe-name>/SKILL.md`** "
    "(및 부속 파일: scripts/·references/ 등)로 `write_file` 한다 — 즉 최종 경로는 `<base>/skills/<safe-name>/SKILL.md` 이며 "
    "**프로세스 폴더(process-…)는 만들지 않는다.** 이 경로여야 프론트 산출물 패널이 표시하고 사용자가 저장할 수 있다. "
    "(`/workspace/.bpmn/skills/...` 처럼 base 를 빼고 쓰지 말 것 — 반드시 이 대화의 base 아래여야 한다.) "
    "완료하면 채팅엔 **'스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.'** 한 줄만 남긴다"
    "(process-definition.json·폼·DMN·에이전트·검증은 만들지 않는다). "
    "아래 'BPMN/프로세스 생성' 절차는 사용자가 명시적으로 프로세스/워크플로/BPMN 을 만들라고 한 경우에만 적용된다.\n\n"
    "## BPMN/프로세스 생성 — 이 스킬로 위임\n"
    "사용자가 새 프로세스/워크플로/BPMN 을 생성·설계해 달라고 했으면(스킬 생성이 아닌 경우), "
    "**절차·도구 제한·저장 정책을 여기서 재설명하지 않는다 — bpmn-process-generation 스킬의 SKILL.md 를 `read_file` 로 읽고, "
    "이 실행 환경(deepagents)이므로 그 안의 references/12-deepagents-execution.md 섹션까지 반드시 함께 읽어 그대로 따른다.** "
    "그 문서가 5단계 절차·도구 이름 매핑·도구 제한·불변 규칙·멀티 프로세스 처리를 전부 규정한다. "
    "여기서는 그 스킬 문서에 없는, 이 대화에서만 유효한 값(산출물 경로)만 아래에서 동적으로 추가로 준다.\n\n"
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


logger = logging.getLogger(__name__)

# OpenAI 는 요청당 tools 배열을 128개로 제한한다(초과 시 400 array_above_max_length).
# 테넌트가 MCP 서버를 여러 개 등록하면 이 한도를 쉽게 넘는다.
# (예: wms + erp-supabase + supabase-mcp + ontology-studio-search = MCP 도구만 86개)
#
# create_deep_agent 가 여기서 넘긴 목록 위에 자체 도구(파일시스템·task 등 약 9개)를
# 더 붙이므로, 그 몫만큼 여유를 두고 잘라야 최종 배열이 한도 안에 들어온다.
_MAX_TOOLS = int(os.environ.get("MAX_TOOLS", "110"))


def _cap_tools(tools: list) -> list:
    """도구 수를 모델 한도 안으로 줄인다.

    앞쪽에 붙는 MCP 도구부터 잘라낸다. 프로세스 생성·HITL·BPM 같은 내장 도구가
    뒤쪽에 있고 그것들이 없으면 기능 자체가 죽기 때문이다.
    자르면 어떤 도구가 빠졌는지 로그로 남긴다 — 조용히 사라지면 원인을 못 찾는다.
    """
    if len(tools) <= _MAX_TOOLS:
        return tools

    dropped = len(tools) - _MAX_TOOLS
    removed = tools[:dropped]
    logger.warning(
        "도구 %d개가 모델 한도(%d)를 넘어 앞쪽 MCP 도구 %d개를 제외한다: %s",
        len(tools), _MAX_TOOLS, dropped,
        [getattr(t, "name", str(t)) for t in removed],
    )
    return tools[dropped:]


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
    # BSC 전략 도구(save_strategy_objective/save_strategy_kpi/save_canvas_block/link_kpi_to_process)와
    # 웹페이지 조회 도구(fetch_webpage — Claude Code의 WebFetch에 대응)는
    # HITL 도구와 달리 DRAFT 모드에서도 제외하지 않고 항상 제공한다.
    tools += get_strategy_tools()
    tools += get_web_tools()
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

    tools = _cap_tools(tools)

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
