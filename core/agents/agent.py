import os
from pathlib import Path

from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver

from .tool_retry import build_tool_retry_middleware
from ..sandbox.docker_sandbox import get_or_create_sandbox
from ..chat.hitl import get_hitl_tools, hitl_tools_enabled
from ..llm.model import _build_model
from ..rag.tools import get_memento_tools
import asyncio

from ..skills.eval_tools import get_skill_eval_tools
from ..skills.skills import _get_skills, FilteredSkillsMiddleware
from ..skills.git_providers.factory import get_provider as _get_git_provider
from ..llm.mcp import _load_mcp_tools_grouped
from .skill_eval_gate import SkillEvalGateMiddleware
from .subagents import _build_subagents, _format_profile_traits, _normalize_tool_filters, _parse_csv
from ..skills.tools import (
    checkout_to_workspace,
    extract_document_structure,
    list_git_repos,
    publish_skill,
    register_skill_as_tenant,
    bpmn_container_base,
    canonical_process_dir_name,
    _current_sandbox as _skill_sandbox,
    _current_user_name as _skill_user_name,
    _current_user_email as _skill_user_email,
    _current_user_id as _skill_user_id,
)
from ..storage.storage import set_tenant_id as _set_storage_tenant_id
from ..bpm.tools import get_bpm_tools, set_tenant_id as _set_bpm_tenant_id, set_user_context as _set_bpm_user
from core.strategy.tools import get_strategy_tools
from core.web.tools import get_web_tools

_BASE_DIR = Path(__file__).parent.parent.parent
_SKILLS_DIR = Path(os.getenv("SKILLS_HOST", str(_BASE_DIR / "skills")))
_SKILLS_CONTAINER_MOUNT = "/skills"

_BASE_INTRO = (
    "You are a helpful assistant. "
    "Use the available tools when needed to answer questions accurately. "
    "Reply in Korean unless the user writes in another language.\n\n"
)

# 스킬/도구 후보 중 요청에 가장 적절한 것을 고르는 일반 규칙. 어떤 스킬 세트가 붙어있든
# (제네릭 딥에이전트든, 커스텀 프로필에 지정된 소수의 스킬이든) 공통으로 필요하다.
_SKILL_SELECTION_RULES = (
    "## 작업 선택 규칙\n"
    "요청을 처리할 때, 사용 가능한 **도구와 스킬(skills 목록) 후보 중 그 작업에 가장 적절한 것**을 골라 수행한다. "
    "요청 메시지에 [Instruction] 이 적혀있다면 그 다음 문장을 실행 **지침**으로 간주한다.\n"
    "**먼저 스킬 목록의 각 description 을 이 요청과 실제로 대조해라** — 아래 예시에 없는 요청이어도 "
    "description 이 명백히 일치하면(예: '경영전략을 세우고 싶다', 'OO 회사 전략 수립해줘', "
    "'BSC 전략맵/균형성과표/비즈니스 모델 캔버스 만들어줘' → bsc-strategy-interview 스킬처럼) 그 스킬을 써야 한다. "
    "요청이 특정 스킬의 영역에 명확히 해당하면(예: 프로세스/워크플로/BPMN 생성 → bpmn-process-generation 스킬, "
    "회사·경영전략·BSC·균형성과표·비즈니스 모델 캔버스 수립 → bsc-strategy-interview 스킬), "
    "**generic 도구나 스스로 아는 지식으로 우회 답변하지 말고 그 스킬의 SKILL.md 를 read_file 로 읽어 절차를 따른다.** "
)

# 아래 'BPMN/프로세스 생성' 절차가 실제로 프롬프트에 포함될 때만 의미가 있는 캐비엇이라
# 제네릭 딥에이전트(DEFAULT_SYSTEM_PROMPT)에서만 붙인다.
_SKILL_SELECTION_BPMN_CAVEAT = (
    "아래 'BPMN/프로세스 생성' 절차가 이 프롬프트의 대부분을 차지한다고 해서 이 에이전트가 BPMN 생성 전용이라고 "
    "판단하지 마라 — 스킬 목록에 있는 다른 스킬(예: bsc-strategy-interview)이 더 맞으면 그것부터 읽는다.\n\n"
)

_WEB_FETCH_RULES = (
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
)

# 스킬/프로세스 생성이라는 "파워유저" 워크플로 전용 섹션. 대화 상대로 커스텀 프로필이 지정된
# 경우(root_profile)에는 배제한다 — 그런 프로필로 BPMN·스킬 생성을 시키는 경우는 상정하지 않는다.
_SKILL_CREATION_DELEGATION = (
    "## 단독 스킬 생성 (프로세스가 아니라 \'스킬\' 자체를 만들어 달라고 할 때) — 최우선 판별\n"
    "**무엇보다 먼저, 이 요청이 \'스킬 생성\'인지 \'프로세스 생성\'인지부터 판별한다.** "
    "사용자 문장에 \'스킬\'/\'skill\' 을 만들어·생성해 달라는 의도가 있으면(예: \'~하는 스킬 만들어줘\', \'~ 스킬 생성\', \'skill 만들어줘\') "
    "**이것은 스킬 생성이다. 프로세스 생성이 아니다.** "
    "**⚠️ 파일/문서(json·pdf·xlsx 등)가 업로드되어 있어도 판단이 바뀌지 않는다** — 업로드 문서는 스킬이 참고할 "
    "**지식/데이터**일 뿐이며, 그것 때문에 프로세스 생성으로 바꾸지 마라. 업로드가 있다는 이유로 프로세스 컨설팅(번호 매긴 업무 흐름 "
    "\'1) … → 2) …\' 제시)이나 승인/반려 질문을 절대 시작하지 마라. \n"
    "이때 **프로세스 생성 절차(컨설팅·process-definition.json·폼·DMN·에이전트·검증)를 시작하지 않는다.** "
    "(process-definition.json·폼·DMN·에이전트·검증은 만들지 않으며 **프로세스 폴더(process-…)도 만들지 않는다.**) "
    "아래 \'BPMN/프로세스 생성\' 절차는 사용자가 명시적으로 프로세스/워크플로/BPMN 을 만들라고 한 경우에만 적용된다.\n\n"

    "### 스킬 생성은 아래 6단계를 순서대로 **직접** 수행한다 (건너뛰기 금지)\n"
    "이 파이프라인 전체를 `general-purpose` 같은 범용 서브에이전트에 통째로 위임하지 마라. "
    "**서브에이전트에는 `task()` 도구가 없어서, 위임하는 순간 아래 3·4단계(테스트 실행·채점)를 영원히 수행할 수 없게 된다.** "
    "`test-runner`·`evaluator` 는 이 루트 에이전트만 호출할 수 있다.\n"
    "1. **초안 작성** — `<base>/skills/<safe-name>/SKILL.md` 에 정식 스킬(YAML frontmatter `name`/`description` + "
    "progressive disclosure SOP 절차)을 `write_file` 한다. 업로드 문서 내용이 필요하면 `search_documents`(임베딩됨)로 읽어 "
    "`references/` 에 반영한다. 참고 문서의 storage 경로가 주어졌다면 그 전에 `task(subagent_type=\'rule-extractor\')` 로 "
    "규칙을 먼저 추출한다.\n"
    "2. **테스트 시나리오 작성** — `<base>/skills/<safe-name>/evals/evals.json` 에 실제 사용자가 말할 법한 시나리오를 "
    "**2건 이상** 쓴다. 각 항목은 `{\"id\",\"name\",\"prompt\",\"expected_output\",\"files\",\"assertions\"}` 이며 "
    "`assertions` 는 객관적으로 검증 가능한 문장 **2건 이상**이다. 그 다음 `plan_skill_eval(skill_path)` 를 호출해 "
    "이번 회차의 iteration 번호와 각 실행의 `outputs_dir` 를 받는다.\n"
    "3. **테스트 실행** — `plan_skill_eval` 이 돌려준 `runs` 항목을 **하나도 빠짐없이, 전부 같은 턴에** "
    "`task(subagent_type=\'test-runner\')` 로 띄운다(스킬을 붙인 `with_skill` 과 대조군 `without_skill` 을 함께). "
    "task 설명에는 받은 `skill_path`·`prompt`·`outputs_dir`·`variant` 를 그대로 적는다.\n"
    "4. **채점** — 실행이 끝나면 시나리오마다 `task(subagent_type=\'evaluator\')` 로 assertion 을 채점해 "
    "`plan_skill_eval` 이 알려준 `grading.json` 경로에 저장하게 한다.\n"
    "5. **완료 게이트** — `complete_skill_creation(skill_path)` 를 호출한다. 이 도구가 성공을 돌려주기 전에는 "
    "**사용자에게 스킬 생성 완료를 알리지 마라.** ERROR 가 나오면 그 안의 `next_steps` 를 그대로 수행하고 다시 호출한다. "
    "(이 도구가 시나리오와 채점 결과를 저장한다. **이 시나리오가 나중에 이 스킬의 병합 요청을 검토할 때 "
    "\'변경 전 vs 변경 후\' 를 비교하는 검증의 입력이 되므로**, 이 단계를 건너뛴 스킬은 병합 전에 기존 동작이 "
    "깨지는지 확인할 수단이 없다.)\n"
    "6. **보고** — 게이트를 통과한 뒤에만 채팅에 **\'스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.\'** 한 줄과 "
    "테스트 통과율(스킬 적용 vs 대조군)을 짧게 남긴다"
    "— 단, **병합 요청(PR)을 올린 경우에는 이 문구를 쓰지 말고** PR 링크와 어디서 병합하는지"
    "(스킬 상세 → 변경 이력 → 병합 요청)를 알려라. 그 경우 저장 버튼을 누를 일이 없다.\n"
    "결과 파일은 반드시 **이번 대화 전용 산출물 경로(아래 \'📁\' 섹션의 base) 바로 아래 `skills/<safe-name>/`** 에 만든다 — "
    "즉 최종 경로는 `<base>/skills/<safe-name>/SKILL.md` 다. 이 경로여야 프론트 산출물 패널이 표시하고 사용자가 저장할 수 있다. "
    "(`/workspace/.bpmn/skills/...` 처럼 base 를 빼고 쓰지 말 것 — 반드시 이 대화의 base 아래여야 한다.) "
    "테스트 실행 산출물은 스킬 폴더가 아니라 `plan_skill_eval` 이 알려주는 `.skill-evals/` 아래에 둔다 — "
    "스킬 폴더에 넣으면 저장되는 스킬 패키지에 섞인다.\n\n"

)

# 마찬가지로 BPMN/프로세스 생성 전용 — root_profile 이 있을 때는 배제한다.
_BPMN_DELEGATION = (
    "## BPMN/프로세스 생성 — 이 스킬로 위임\n"
    "사용자가 새 프로세스/워크플로/BPMN 을 생성·설계해 달라고 했으면(스킬 생성이 아닌 경우), "
    "**절차·도구 제한·저장 정책을 여기서 재설명하지 않는다 — bpmn-process-generation 스킬의 SKILL.md 를 `read_file` 로 읽고, "
    "이 실행 환경(deepagents)이므로 그 안의 references/12-deepagents-execution.md 섹션까지 반드시 함께 읽어 그대로 따른다.** "
    "그 문서가 5단계 절차·도구 이름 매핑·도구 제한·불변 규칙·멀티 프로세스 처리를 전부 규정한다. "
    "여기서는 그 스킬 문서에 없는, 이 대화에서만 유효한 값(산출물 경로)만 아래에서 동적으로 추가로 준다.\n\n"
)

# MCP 도구 호출 재시도 규칙은 어떤 MCP 도구가 붙어있든(제네릭이든 커스텀 프로필의 tools든)
# 공통으로 필요하다.
_MCP_RETRY_RULES = (
    "## MCP 도구 호출 규칙\n"
    "- **MCP 도구 호출이 에러(ToolException 등)로 실패하면, 그 에러 메시지를 읽고 원인(잘못된 domain 문법, "
    "날짜/시간 형식 등)을 스스로 진단해 인자를 고쳐 즉시 다시 호출한다.** 같은 인자로 무의미하게 반복하지 말고, "
    "에러 내용에 맞춰 값을 보정한다. 실패를 그대로 사용자에게 보고하고 멈추지 말 것 — 합리적인 보정을 몇 차례 "
    "시도해도 계속 실패할 때만 무엇이 실패했는지 사용자에게 알린다.\n"
)

# 이미 있는 프로세스를 대화 안에서 실행하는 순서.
#
# 이 규칙이 없으면 에이전트는 "실행 화면에서 진행하세요" 하고 링크를 던지거나, 역할을
# 한꺼번에 물어 무엇을 답해야 하는지 알기 어렵게 만든다. work-assistant-agent 도 같은
# 순서를 쓴다 — 어느 에이전트와 이야기하든 같아야 사용자가 다시 배우지 않는다.
#
# MCP(process-gpt-mcp)의 instructions 에도 같은 내용이 있지만 이쪽은 그것을 프롬프트로
# 들이지 않는다. 그래서 여기에 따로 적는다.
_PROCESS_EXECUTION_RULES = (
    "\n\n## 기존 프로세스 실행 요청 처리\n"
    "- 이미 있는 프로세스를 실행해 달라고 하면 **정의·실행 화면으로 보내지 말고 지금 대화 안에서 끝낸다.**\n"
    "- 순서: get_process_list → get_process_detail → (역할 배정) → get_form_fields → execute_process\n"
    "- get_process_detail 결과의 start_activities 가 **시작 단계**다. get_form_fields 와 execute_process 는 "
    "이 시작 단계의 form_key · activity_id 로만 부른다. 뒤 단계(시스템 작업·검토 등)의 폼은 조회하지 않는다.\n"
    "- definition.roles 순서대로 **한 번에 하나씩** 묻는다. 질문은 반드시 "
    '"{순번}번 {역할명} 역할은 누구로 설정하시겠습니까?" 형식을 쓴다. '
    '사용자가 이미 정해 준 역할(예: "역할은 모두 나로")은 다시 묻지 않는다.\n'
    "- 담당자 후보가 필요하면 get_organization 으로 조회해 선택지를 함께 보인다.\n"
    "- 모든 역할이 정해진 뒤에 시작 단계의 get_form_fields 를 호출한다. 폼 값은 화면이 입력란으로 "
    "그려 주므로 항목을 하나씩 되묻지 않고, 입력 폼을 채워 제출해 달라고만 말하고 턴을 마친다.\n"
    "- 단, 사용자가 요청에 첫 폼 값을 이미 다 주었으면(예: '요청 내용은 ○○') 폼을 다시 받지 않는다. "
    "get_form_fields 로 필드 키만 확인하고 그 값으로 바로 execute_process 를 호출한다.\n"
    "- 사용자가 폼을 제출하면 모아 둔 역할 배정을 함께 넣어 execute_process 를 바로 호출한다.\n"
    "- **execute_process 는 요청 한 건당 정확히 한 번**이다. 인스턴스가 시작되면 이후 단계는 엔진이 "
    "알아서 진행하므로, 뒤 단계의 activity_id 로 execute_process 를 다시 부르지 않는다(부르면 별개의 인스턴스가 "
    "생긴다). 결과를 확인하려고 다시 부르지도 않는다. already_started 응답이면 새로 실행하지 않은 것이다.\n"
    "- execute_process 결과에 process_instance_id 가 오기 전에는 **절대 '실행했습니다' 라고 말하지 않는다.** "
    "도구를 부르지 않았거나 오류가 났으면 그대로 알린다.\n"
    "- 실행에 성공하면 링크나 인스턴스 ID 를 안내하지 않는다. 화면이 '프로세스가 실행되었습니다' 카드를 "
    "보여 주고, 카드를 누르면 인스턴스 대화로 간다. 무엇을 시작했는지 한두 문장으로만 알리고 끝낸다.\n"
)

DEFAULT_SYSTEM_PROMPT = (
    _BASE_INTRO
    + _SKILL_SELECTION_RULES
    + _SKILL_SELECTION_BPMN_CAVEAT
    + _WEB_FETCH_RULES
    + _SKILL_CREATION_DELEGATION
    + _BPMN_DELEGATION
    + _PROCESS_EXECUTION_RULES
    + _MCP_RETRY_RULES
)

WEB_FETCH_PROMPT = (
    "\n\n"
    "## 웹페이지 조회 도구 사용 원칙\n"
    "`fetch_webpage` 도구가 있다. 사용자가 회사 도메인·URL·회사명을 언급했고(예: 'uengine.org 라는 회사의 전략을 "
    "수립하고 싶어') 그 회사 정보가 필요한 작업(전략 수립, 프로세스 추천 등)이면, **사용자에게 회사 사업모델·제품을 "
    "설명해 달라고 되묻는 대신 `fetch_webpage` 를 그 도메인으로 직접 호출**해 실제 내용을 확인한다. "
    "단, 로딩된 스킬(SKILL.md)이 이보다 먼저 해야 할 단계를 명시했다면(예: 기존 데이터 존재 여부 확인 등) "
    "그 스킬의 순서를 따른다 — 이 원칙은 '사용자에게 설명을 요구하지 말라'는 것이지 '다른 모든 단계보다 "
    "무조건 먼저 하라'는 뜻이 아니다. "
    "본문이 비어있거나(notice 필드) 조회 실패면 그때만 사용자에게 보충 정보나 다른 URL을 요청한다."
)


# (도구 재시도 미들웨어는 .tool_retry 로 옮겨졌다. 그쪽은 HITL 의 GraphInterrupt 를
#  삼키지 않는 판까지 갖췄으므로 여기의 옛 판을 남겨 두지 않는다.)

# 대화 상대가 사용자 커스텀 에이전트 프로필(root_profile)일 때 쓰는 축약 프롬프트.
# 스킬 라우팅·MCP 재시도처럼 어떤 에이전트든 필요한 규칙만 남기고, BPMN/스킬 생성 같은
# "파워유저" 전용 워크플로(및 그에 딸린 산출물 경로 지시)는 뺀다 — 커스텀 프로필로
# 대화하는 사용자가 그런 작업을 요청하는 경우는 상정하지 않는다.
CUSTOM_PROFILE_SYSTEM_PROMPT = (
    _BASE_INTRO
    + _SKILL_SELECTION_RULES
    # 프로세스 실행은 파워유저 전용이 아니다. 커스텀 프로필과 이야기하다가도 시킬 수 있다.
    + _PROCESS_EXECUTION_RULES
    + "\n\n"
    + _MCP_RETRY_RULES
)


async def build_agent(
    tenant_id: str = "",
    system_prompt: str | None = None,
    extra_tools: list = None,
    checkpointer=None,
    subagents_raw: list = None,
    tenant_mcp: dict | None = None,
    middleware: list = None,
    static_subagents: list = None,
    conversation_id: str = "",
    user_context: dict | None = None,
    agent_mode: str = "",
    is_chat: bool = True,
    activity_tools: list[str] | None = None,
    activity_skills: list[str] | None = None,
    root_profile: dict | None = None,
):
    if system_prompt is None:
        system_prompt = CUSTOM_PROFILE_SYSTEM_PROMPT if root_profile else DEFAULT_SYSTEM_PROMPT

    tools = (
        list(extra_tools or [])
        + (get_hitl_tools() if hitl_tools_enabled(agent_mode=agent_mode, is_chat=is_chat) else [])
        + get_memento_tools()
        # 프로세스를 새로 시작하는 도구는 채팅에서만 준다 — 워크아이템을 처리하던 에이전트가
        # 업무 도중 다른 인스턴스를 만들어 버리지 않게.
        + [t for t in get_bpm_tools() if is_chat or getattr(t, "name", "") != "execute_process"]
        + [
            checkout_to_workspace,
            extract_document_structure,
            list_git_repos,
            publish_skill,
            register_skill_as_tenant,
            # 프로세스 정의 전용 도구(write/update/validate/complete_process_generation)는
            # 에이전트에 바인딩하지 않는다. 에이전트는 산출물 파일만 만들고, 검증·통합은
            # 턴 종료 시 executor 가 결정적으로 수행한다(core/skills/tools.py 의 동일 함수 재사용).
        ]
    )
    # BSC 전략 도구(save_strategy_objective/save_strategy_kpi/save_canvas_block/link_kpi_to_process)와
    # 웹페이지 조회 도구(fetch_webpage — Claude Code의 WebFetch에 대응)는
    # HITL 도구와 달리 DRAFT 모드에서도 제외하지 않고 항상 제공한다.
    # tools += get_strategy_tools()
    # tools += get_web_tools()
    # 스킬 평가/회귀 도구는 채팅 대화 전반에 붙인다. 처음에는 "커스텀 프로필 대화는 스킬을
    # 만들지 않는다" 고 보고 root_profile 일 때 제외했는데, 실측에서 정반대였다 — 정의체계도
    # 메인 채팅은 담당 에이전트가 루트로 승격돼 **항상 root_profile 이 잡히고**, 스킬은 바로
    # 거기서 만들어진다. 그 결과 스킬을 만드는 자리에서만 게이트와 도구가 빠져 있었다.
    # 워크아이템 실행(is_chat=False)에는 여전히 붙이지 않는다.
    if is_chat:
        tools += get_skill_eval_tools()

    backend = get_or_create_sandbox(tenant_id, workspace_host=_BASE_DIR / "workspace", skills_host=_SKILLS_DIR)
    _skill_sandbox.set(backend)
    _skill_user_name.set((user_context or {}).get("user_name") or "")
    _skill_user_email.set((user_context or {}).get("user_email") or "")
    _skill_user_id.set((user_context or {}).get("user_id") or "")
    _set_storage_tenant_id(tenant_id)
    _set_bpm_tenant_id(tenant_id)
    _set_bpm_user(user_context)
    _dynamic, _mcp_connectors, _mcp_tools_meta = await _build_subagents(
        subagents_raw or [], tenant_id, tenant_mcp, backend=backend
    )
    built_subagents = list(static_subagents or []) + _dynamic

    # 이 대화의 담당 에이전트 프로필(root_profile)에 지정된 스킬은, 서브에이전트 프로필 없이
    # tools/skills 만 지정된 액티비티와 동일하게 activity_skills 목록에 합류시켜 아래
    # FilteredSkillsMiddleware 경로를 그대로 재사용한다.
    if root_profile:
        _rp_skill_names = _parse_csv(root_profile.get("skills"))
        if _rp_skill_names:
            activity_skills = list(dict.fromkeys(list(activity_skills or []) + _rp_skill_names))

    # 활동(activity)에 서브에이전트 프로필 없이 tools/skills 만 지정된 경우, 별도의
    # 폴백 서브에이전트를 만드는 대신 루트 딥에이전트에 도구/스킬을 직접 부착한다.
    if activity_tools and tenant_mcp:
        _activity_grouped = await _load_mcp_tools_grouped(activity_tools, tenant_mcp)
        for _srv, _srv_tools in _activity_grouped.items():
            tools.extend(_srv_tools)
            if _srv not in _mcp_connectors:
                _mcp_connectors.append(_srv)
            _tool_names = [
                getattr(t, "name", "") for t in _srv_tools
                if getattr(t, "name", "")
            ]
            if _tool_names:
                _mcp_tools_meta.append({
                    "agent": "__root__",
                    "servers": [_srv],
                    "tools": _tool_names,
                })

    # `fetch_webpage` 안내는 그 도구가 실제로 바인딩됐을 때만 붙인다.
    # (도구 없이 안내만 남으면 모델이 없는 도구를 호출하려다 턴을 낭비한다.)
    if any(getattr(t, "name", "") == "fetch_webpage" for t in tools):
        system_prompt = system_prompt + WEB_FETCH_PROMPT

    # root_profile 에 지정된 MCP 도구 서버도 동일하게 루트에 직접 부착한다(tool_filters 반영).
    if root_profile and tenant_mcp:
        _rp_tool_names = _parse_csv(root_profile.get("tools"))
        if _rp_tool_names:
            _rp_tool_filters = _normalize_tool_filters(root_profile.get("tool_filters"))
            _rp_grouped = await _load_mcp_tools_grouped(_rp_tool_names, tenant_mcp, tool_filters=_rp_tool_filters)
            for _srv, _srv_tools in _rp_grouped.items():
                tools.extend(_srv_tools)
                if _srv not in _mcp_connectors:
                    _mcp_connectors.append(_srv)
                _tool_names = [
                    getattr(t, "name", "") for t in _srv_tools
                    if getattr(t, "name", "")
                ]
                if _tool_names:
                    _mcp_tools_meta.append({
                        "agent": "__root__",
                        "servers": [_srv],
                        "tools": _tool_names,
                    })

    _user_ctx = user_context or {}
    _info_parts = []
    if tenant_id:
        _info_parts.append(f"- 테넌트 ID: {tenant_id}")
    if _user_ctx.get("user_id"):
        _info_parts.append(f"- 사용자 ID: {_user_ctx['user_id']}")
    if _user_ctx.get("user_name"):
        _info_parts.append(f"- 이름: {_user_ctx['user_name']}")
    if _user_ctx.get("user_email"):
        _info_parts.append(f"- 이메일: {_user_ctx['user_email']}")
    if _user_ctx.get("user_jwt"):
        _info_parts.append(f"- JWT: {_user_ctx['user_jwt']}")
    if _info_parts:
        system_prompt = system_prompt + "\n\n## 현재 사용자 정보\n" + "\n".join(_info_parts)

    if root_profile:
        _rp_lines = _format_profile_traits(root_profile)
        if _rp_lines:
            system_prompt = system_prompt + "\n\n## 이 대화의 담당 에이전트 프로필\n" + "\n".join(_rp_lines)

    if built_subagents:
        agent_names = ", ".join(f"`{a['name']}`" for a in built_subagents)
        delegation_prompt = (
            f"\n\n## 서브에이전트 위임 규칙\n"
            f"현재 다음 서브에이전트가 구성되어 있습니다: {agent_names}\n"
            f"요청이 서브에이전트의 역할·전문 영역에 해당하면 task() 툴로 위임하세요. "
            f"서브에이전트의 역할과 무관한 일반 요청은 직접 처리하세요. "
            f"task() 결과(ToolMessage)가 이미 사용자에게 보여줄 완결된 답변이면, "
            f"그 내용을 다시 요약하거나 반복해서 재서술하지 말고 그대로(또는 아주 짧은 인사말만 덧붙여) 전달하세요. "
            f"같은 내용을 두 번 쓰는 답변을 절대 만들지 마세요."
        )
        final_prompt = system_prompt + delegation_prompt
    else:
        final_prompt = system_prompt

    if activity_tools or activity_skills:
        _act_lines = ["\n\n## 액티비티 지정 도구/스킬"]
        if activity_tools:
            _act_lines.append(f"- 이 액티비티에 지정된 MCP 도구 서버: {', '.join(activity_tools)}")
        if activity_skills:
            _act_lines.append(f"- 이 액티비티에 지정된 스킬: {', '.join(activity_skills)}")
        _act_lines.append("- 위 도구/스킬을 우선 사용해 업무를 수행하세요.")
        final_prompt = final_prompt + "\n".join(_act_lines)

    # 방별 산출물 디렉터리 주입 + 프로세스별 하위 폴더(한 방에 여러 프로세스 가능).
    # 폴더명은 executor 가 **구체적인 값**을 생성해 주입한다(에이전트가 `<랜덤 id>`·`<new_id>` 같은
    # placeholder 를 그대로 적어 `<`,`>` 가 든 Windows 비호환 폴더를 만들던 문제 방지).
    # HITL resume rebuilds the graph/agent. Keep the folder calculation shared
    # with validation/completion so stale random folders are never re-collected.
    # 기본 디렉터리 안내는 root_profile(커스텀 프로필)에도 항상 붙인다 — 그 프로필에 붙은
    # 스킬/도구가 파일을 읽고 쓸 수 있으므로 어디에 저장해야 하는지는 여전히 필요하다.
    # 프로세스 폴더 규칙·process-definition.json·validate/complete 호출 같은 BPMN 생성
    # 절차 전용 세부사항만, 그 절차를 위임하는 섹션이 없는 root_profile 에서는 뺀다.
    _bpmn_base = bpmn_container_base(conversation_id)
    if root_profile:
        final_prompt = final_prompt + (
            f"\n\n## 📁 이번 대화 전용 산출물 경로\n"
            f"파일을 생성·저장하는 도구나 스킬을 사용할 때는 반드시 이 대화 전용 디렉터리 "
            f"**`{_bpmn_base}/`** 아래에 저장한다. 기존 파일은 그대로 **덮어쓰기**(덮어쓸지 묻지 말 것)."
        )
    else:
        _pfolder = canonical_process_dir_name(conversation_id)
        final_prompt = final_prompt + (
            f"\n\n## 📁 이번 대화 전용 산출물 경로(필수)\n"
            f"이 대화의 산출물 기준 디렉터리는 **`{_bpmn_base}/`** 다. "
            f"**단독 스킬 생성**(프로세스가 아님)이면 프로세스 폴더 없이 **`{_bpmn_base}/skills/<safe-name>/SKILL.md`** 에 바로 만든다. "
            f"프로세스 생성이면 아래 규칙을 따른다: "
            f"**프로세스 폴더명은 반드시 `{_pfolder}` 를 그대로 사용한다**(즉 `{_bpmn_base}/{_pfolder}/`). "
            f"그 폴더 안에 `process-definition.json`·`forms/<activity_id>.form`·`skills/<safe-name>/SKILL.md`·"
            f"`agents/<agent_id>.json`(에이전트 1명 = 파일 1개)·`manifest.json` 을 만든다. "
            f"**한 방에 여러 프로세스면** 두 번째부터는 `{_pfolder}-2`, `{_pfolder}-3` 처럼 **뒤에 번호만** 붙인다. "
            f"⚠️ 폴더명에 `<`, `>` 같은 문자나 '랜덤'·'id'·'new_id' 같은 placeholder 단어를 절대 쓰지 말 것(예: `process-<랜덤 id>` ❌). "
            f"SKILL.md/reference 가 `/workspace/.bpmn/...` 라고 해도 위 프로세스 폴더 경로로 바꿔 사용한다. "
            f"기존 파일은 그대로 **덮어쓰기**(덮어쓸지 묻지 말 것). "
            f"⚠️ 검증·통합·저장 도구는 이 세션에 **없다**. `validate_process_definition`·"
            f"`complete_process_generation`·`write_process_definition`·`update_process_definition`·"
            f"`save_process_definition` 을 호출하려 하지 마라(존재하지 않는 도구다). "
            f"파일 입출력은 `write_file`/`edit_file`/`read_file` 만 쓴다. "
            f"산출물 파일을 다 만들고 턴을 끝내면 **시스템이 자동으로** 정의를 검증·보정하고, "
            f"폼/스킬/에이전트/DMN 을 프로세스에 통합해 프론트 산출물 패널로 전달한다. "
            f"실제 저장은 사용자가 화면에서 '저장' 버튼을 눌러 한다."
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

    extra_middleware = list(middleware or [])
    # 활동 지정 스킬이 있으면 전체 스킬 목록 대신 필터링된 목록만 노출한다
    # (SkillsMiddleware 중복 부착을 피하기 위해 skills= 자체는 비운다).
    skills_kwarg = container_skills or None
    if activity_skills and container_skills:
        extra_middleware.append(
            FilteredSkillsMiddleware(
                backend=backend,
                sources=container_skills,
                allowed_skills=activity_skills,
            )
        )
        skills_kwarg = None

    kwargs = dict(
        model=_build_model(),
        tools=tools,
        system_prompt=final_prompt,
        skills=skills_kwarg,
        backend=backend,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )
    if built_subagents:
        kwargs["subagents"] = built_subagents
    # 스킬 생성 테스트 게이트: SKILL.md 만 쓰고 테스트 없이 턴을 끝내려 하면 모델 노드로
    # 되돌린다. 채팅 대화 전반에 붙인다(위 도구 부착과 같은 이유 — 스킬은 root_profile 이
    # 잡힌 메인 채팅에서 만들어진다). 스킬을 만들지 않은 턴에는 아무 일도 하지 않는다.
    if is_chat:
        extra_middleware.append(SkillEvalGateMiddleware())

    # 도구 결과의 `file` 블록 하나가 대화 이력에 남으면 이후 모든 모델 호출이 400 으로
    # 실패해 대화방을 쓸 수 없게 된다. 모델에 넘기는 사본에서 텍스트 안내로 바꾼다.
    from ..llm.tool_content_sanitizer import ToolContentBlockSanitizerMiddleware

    kwargs["middleware"] = [
        build_tool_retry_middleware(),
        ToolContentBlockSanitizerMiddleware(),
        *extra_middleware,
    ]

    return create_deep_agent(**kwargs), _mcp_connectors, _mcp_tools_meta
