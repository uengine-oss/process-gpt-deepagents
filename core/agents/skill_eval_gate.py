"""core/agents/skill_eval_gate.py — 스킬 생성 테스트 단계 스킵 방지 미들웨어.

## 왜 필요한가

딥 에이전트 채팅에서 스킬을 만들 때 `test-runner` / `evaluator` 서브에이전트에 작업을
위임해 테스트를 돌리는 단계가 실제 대화 기록 전체에서 **한 번도 수행되지 않았다**
(`task(subagent_type="test-runner")` 호출 0건, `iteration-*` 산출물 0건).

프롬프트에 "반드시 해라" 를 적는 것만으로는 이 단계가 지켜지지 않는다는 것이 이미
데이터로 증명됐다. 그래서 결정론적 장치를 하나 더 둔다:

  - `complete_skill_creation` 도구가 **완료를 거부**한다(테스트 산출물 검증 게이트).
  - 이 미들웨어가 **턴 종료 자체를 되돌린다** — 스킬 SKILL.md 를 쓴 턴에서 게이트를
    통과하지 않은 채 모델이 답변을 끝내려 하면, 남은 단계를 알려주고 모델 노드로
    되돌린다(`jump_to="model"`).

두 장치 모두 "테스트를 건너뛴 종료" 를 정상 종료로 인정하지 않게 만드는 것이 목적이다.
무한 루프를 만들지 않도록 개입 횟수는 `max_nudges` 로 제한하고, 그 뒤에는 통과시킨다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# 이 미들웨어가 끼워 넣은 메시지 표식 — 개입 횟수를 셀 때 쓴다.
_NUDGE_MARKER = "[skill-eval-gate]"

# SKILL.md 를 쓴 폴더가 곧 스킬 루트다.
#
# 처음에는 `<...>/skills/<name>/SKILL.md` 만 인식했는데, 그 경로 규칙은 프롬프트가
# 시키는 것일 뿐 모델이 늘 지키지 않는다 — 실제로 에이전트가
# `/workspace/.bpmn/<room>/<name>/SKILL.md` (중간 `skills/` 없음)에 쓰는 바람에
# 게이트가 스킬 생성 턴으로 보지 못했고, 테스트 없이 "스킬을 생성했어요" 로 끝났다.
# 이 미들웨어의 존재 이유가 "프롬프트를 안 지켜도 막는 것" 이므로, 경로 규칙 준수를
# 전제하지 않는다. 쓰기(write/edit)로 만들어진 SKILL.md 는 어디에 있든 스킬 저작이다.
_SKILL_MD_RE = re.compile(r"^(?P<dir>.+)/SKILL\.md$", re.IGNORECASE)

# 게이트를 통과했다고 인정하는 도구.
_GATE_TOOLS = ("complete_skill_creation", "complete_skill_regression")

# 사용자가 명시적으로 테스트를 빼달라고 한 경우엔 개입하지 않는다.
_OPT_OUT_PATTERNS = (
    "테스트 없이",
    "테스트는 생략",
    "테스트 생략",
    "테스트 안 해도",
    "테스트 안해도",
    "평가 없이",
    "skip test",
    "no test",
    "without test",
)


def _tool_calls(msg: Any) -> list[dict]:
    calls = getattr(msg, "tool_calls", None)
    return list(calls) if isinstance(calls, list) else []


def _last_written_skill_dir(messages: list[Any]) -> str:
    """이 스레드에서 SKILL.md 를 쓴 마지막 스킬 폴더 경로. 없으면 빈 문자열."""
    found = ""
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for call in _tool_calls(msg):
            if call.get("name") not in ("write_file", "edit_file"):
                continue
            args = call.get("args") or {}
            path = str(args.get("file_path") or args.get("path") or "").replace("\\", "/")
            m = _SKILL_MD_RE.match(path)
            if m:
                found = m.group("dir")
    return found


# 스킬 저작으로 볼 만한 도구. 읽기만 한 턴(질문·설명)에는 개입하지 않기 위한 조건이다.
_AUTHORING_TOOLS = ("write_file", "edit_file", "task")


def _authored_something(messages: list[Any]) -> bool:
    return any(
        call.get("name") in _AUTHORING_TOOLS
        for msg in messages
        if isinstance(msg, AIMessage)
        for call in _tool_calls(msg)
    )


def _skill_dir_in_workspace() -> str:
    """이 대화 작업 폴더에 만들어져 있는 스킬 폴더(SKILL.md 가 있는 곳).

    루트 메시지만 봐서는 스킬 생성을 놓친다 — `task()` 로 위임하면 SKILL.md 를 쓴
    write_file 은 **서브에이전트의 메시지**에 남고 루트 스레드에는 task 호출만 보인다.
    실측에서 이 경로로 만들어진 스킬이 게이트를 그대로 통과했다. 그래서 메시지에서
    못 찾으면 작업 폴더를 직접 본다 — 누가 썼든 결과물은 같은 자리에 남는다.
    """
    try:
        from ..skills.tools import _bpmn_work_dir

        base = _bpmn_work_dir()
    except Exception:  # 작업 폴더를 알 수 없으면 판단하지 않는다.
        return ""
    if not base.is_dir():
        return ""

    found: list[Any] = []
    for path in base.glob("**/SKILL.md"):
        # 평가 실행 산출물 안에 복사된 사본은 스킬 원본이 아니다.
        if any(part in (".skill-evals", "outputs") for part in path.parts):
            continue
        found.append(path)
    if not found:
        return ""
    newest = max(found, key=lambda p: p.stat().st_mtime)
    return str(newest.parent)


def _gate_passed(messages: list[Any]) -> bool:
    """게이트 도구가 성공(ERROR 아님)으로 반환된 적이 있는가."""
    for msg in messages:
        if not isinstance(msg, ToolMessage) or msg.name not in _GATE_TOOLS:
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if not content.lstrip().startswith("ERROR"):
            return True
    return False


def _nudge_count(messages: list[Any]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m, HumanMessage)
        and isinstance(m.content, str)
        and m.content.startswith(_NUDGE_MARKER)
    )


def _user_opted_out(messages: list[Any]) -> bool:
    """사용자가 테스트를 빼달라고 말했는지 본다(가장 최근 사용자 발화들만)."""
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if content.startswith(_NUDGE_MARKER):
            continue
        lowered = content.lower()
        return any(p in content or p in lowered for p in _OPT_OUT_PATTERNS)
    return False


class SkillEvalGateMiddleware(AgentMiddleware):
    """스킬을 만들어 놓고 테스트 없이 턴을 끝내려 하면 모델 노드로 되돌린다.

    개입 조건(모두 만족할 때만):
      1. 이 스레드에서 SKILL.md 를 write/edit 했거나(경로 규칙과 무관),
         무언가를 저작한 턴이면서 이 대화 작업 폴더에 SKILL.md 가 있다(서브에이전트 위임).
      2. 마지막 AI 메시지에 도구 호출이 없다(= 턴을 끝내려 한다).
      3. `complete_skill_creation`/`complete_skill_regression` 이 성공한 적이 없다.
      4. 사용자가 테스트 생략을 요청하지 않았다.
      5. 개입 횟수가 `max_nudges` 미만이다.
    """

    def __init__(self, max_nudges: int = 3) -> None:
        super().__init__()
        self._max_nudges = max_nudges

    # ------------------------------------------------------------------
    def _check(self, state: Any) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        if not messages:
            return None

        last = messages[-1]
        if not isinstance(last, AIMessage) or _tool_calls(last):
            return None  # 아직 도구를 부르는 중 — 턴이 끝나지 않았다.

        skill_dir = _last_written_skill_dir(messages)
        if not skill_dir and _authored_something(messages):
            skill_dir = _skill_dir_in_workspace()
        if not skill_dir:
            return None  # 스킬 생성 턴이 아니다.

        if _gate_passed(messages):
            return None

        if _user_opted_out(messages):
            return None

        used = _nudge_count(messages)
        if used >= self._max_nudges:
            logger.warning(
                "skill-eval-gate: 개입 한도(%d) 도달 — 테스트 없이 종료를 허용한다 skill_dir=%s",
                self._max_nudges, skill_dir,
            )
            return None

        detail = self._pending_work(skill_dir)
        logger.info("skill-eval-gate: 테스트 미완료 종료 차단 (%d/%d) skill_dir=%s",
                    used + 1, self._max_nudges, skill_dir)
        return {
            "messages": [HumanMessage(content=(
                f"{_NUDGE_MARKER} 아직 이 스킬의 테스트가 끝나지 않아 완료로 볼 수 없습니다. "
                f"사용자에게 완료를 알리지 말고, 아래 남은 단계를 지금 수행하세요.\n"
                f"대상 스킬: {skill_dir}\n{detail}\n"
                f"마지막에 `complete_skill_creation(\"{skill_dir}\")` 가 성공해야 이 턴을 끝낼 수 있습니다."
            ))],
            "jump_to": "model",
        }

    def _pending_work(self, skill_dir: str) -> str:
        """게이트 검증기를 그대로 돌려 무엇이 남았는지 구체적으로 알려준다."""
        try:
            from ..skills.eval_tools import validate_eval_artifacts, _resolve_skill_dir

            report = validate_eval_artifacts(_resolve_skill_dir(skill_dir))
            return json.dumps(
                {
                    "problems": report.get("problems", []),
                    "next_steps": report.get("next_steps", []),
                    "missing_runs": report.get("missing", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception:  # 검증기 자체가 실패해도 개입은 유지한다.
            logger.exception("skill-eval-gate: 남은 작업 계산 실패 skill_dir=%s", skill_dir)
            return (
                "남은 단계: (1) evals/evals.json 에 시나리오 작성 → "
                "(2) plan_skill_eval 호출 → (3) test-runner 로 with_skill/without_skill 실행 → "
                "(4) evaluator 로 채점 → (5) complete_skill_creation."
            )

    # ------------------------------------------------------------------
    # `can_jump_to` 가 없으면 langchain 이 이 미들웨어 노드에 조건부 엣지 대신 고정 엣지를
    # 깔아버려서 `jump_to` 를 무시한다 — 이 데코레이터가 있어야 실제로 모델로 되돌아간다.
    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._check(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._check(state)
