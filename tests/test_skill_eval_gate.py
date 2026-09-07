"""병합 전 검증의 두 축을 검증한다.

1. **시나리오 확보 게이트** — 스킬을 만들 때 test-runner/evaluator 단계가 실제로
   수행됐는지 확인하고, 안 했으면 완료를 거부한다. 이 단계를 건너뛰면 병합 요청이
   왔을 때 "변경 전과 달라졌는지" 를 비교할 기준 시나리오가 없다.
   (전체 대화 기록에서 `task(subagent_type="test-runner")` 호출이 0건이었다.)

2. **병합 전 비교** — base(변경 전)/head(변경 후) 채점 결과에서 *통과하던 단계가
   깨진 것*을 골라내는 로직.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.agents.skill_eval_gate import SkillEvalGateMiddleware
from core.skills import eval_tools
from core.skills import tools as skill_tools

TENANT = "tenant-eval-test"
ROOM = "11111111-2222-3333-4444-555555555555"
SKILL = "nda-review"


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """WORKSPACE_ROOT 를 임시 디렉터리로 격리하고 테넌트/대화방 컨텍스트를 세팅한다."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", workspace)

    t1 = skill_tools._current_tenant_id.set(TENANT)
    t2 = skill_tools._current_conversation_id.set(ROOM)
    t3 = skill_tools._current_user_id.set("00000000-0000-0000-0000-0000000000aa")
    yield workspace
    skill_tools._current_tenant_id.reset(t1)
    skill_tools._current_conversation_id.reset(t2)
    skill_tools._current_user_id.reset(t3)


def _skill_dir(ws: Path) -> Path:
    return ws / ".bpmn" / ROOM / "skills" / SKILL


def _write_skill(ws: Path) -> Path:
    d = _skill_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: NDA 검토 스킬\n---\n\n## 절차\n1. 읽는다\n",
        encoding="utf-8",
    )
    return d


def _write_evals(skill_dir: Path, *, assertions_per_case: int = 2, cases: int = 2) -> None:
    (skill_dir / "evals").mkdir(parents=True, exist_ok=True)
    payload = {
        "skill_name": SKILL,
        "evals": [
            {
                "id": i + 1,
                "name": f"case-{i + 1}",
                "prompt": f"NDA 문서 {i + 1} 을 검토해줘",
                "expected_output": "검토 리포트",
                "files": [],
                "assertions": [f"assertion {j + 1}" for j in range(assertions_per_case)],
            }
            for i in range(cases)
        ],
    }
    (skill_dir / "evals" / "evals.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_grading(ws: Path, iteration: int, eval_name: str, variant: str, passed: int, total: int) -> None:
    d = eval_tools.eval_runs_root(SKILL) / f"iteration-{iteration}" / eval_name / variant
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    (d / "grading.json").write_text(json.dumps({
        "expectations": [
            {"text": f"assertion {i + 1}", "passed": i < passed, "evidence": "…"}
            for i in range(total)
        ],
        "summary": {"passed": passed, "failed": total - passed, "total": total,
                    "pass_rate": round(passed / total, 2) if total else 0},
    }, ensure_ascii=False), encoding="utf-8")


def _complete_run(ws: Path, iteration: int = 1, *, with_passed: int = 2, without_passed: int = 0) -> None:
    """두 시나리오 × 두 variant 의 채점 결과를 모두 만든다."""
    for name in ("case-1", "case-2"):
        _write_grading(ws, iteration, name, "with_skill", with_passed, 2)
        _write_grading(ws, iteration, name, "without_skill", without_passed, 2)


# ---------------------------------------------------------------------------
# 게이트 검증기
# ---------------------------------------------------------------------------

def test_evals_json_이_없으면_완료를_거부한다(ws):
    skill_dir = _write_skill(ws)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is False
    assert any("evals.json" in p for p in report["problems"])
    assert report["next_steps"]


def test_assertion_이_부족하면_완료를_거부한다(ws):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir, assertions_per_case=1)
    _complete_run(ws)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is False
    assert any("assertions" in p for p in report["problems"])


def test_시나리오가_하나뿐이면_완료를_거부한다(ws):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir, cases=1)
    _write_grading(ws, 1, "case-1", "with_skill", 2, 2)
    _write_grading(ws, 1, "case-1", "without_skill", 0, 2)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is False
    assert any("시나리오" in p for p in report["problems"])


def test_테스트를_돌리지_않으면_완료를_거부하고_다음_할일을_알려준다(ws):
    """스킵이 실제로 일어난 상태 — SKILL.md 와 evals.json 은 있는데 실행 결과가 없다."""
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is False
    assert report["missing"], "실행되지 않은 run 이 보고되어야 한다"
    joined = " ".join(report["next_steps"])
    assert "test-runner" in joined and "evaluator" in joined


def test_대조군만_없어도_완료를_거부한다(ws):
    """with_skill 만 돌리고 대조군을 빼면 스킬 효과를 알 수 없으므로 통과시키지 않는다."""
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    for name in ("case-1", "case-2"):
        _write_grading(ws, 1, name, "with_skill", 2, 2)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is False
    assert any(m["variant"] == "without_skill" for m in report["missing"])


def test_모든_산출물이_있으면_통과하고_통과율을_계산한다(ws):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    _complete_run(ws, with_passed=2, without_passed=1)

    report = eval_tools.validate_eval_artifacts(skill_dir)

    assert report["ok"] is True, report["problems"]
    assert len(report["results"]) == 4  # 2 케이스 × 2 variant
    with_rate = eval_tools._variant_pass_rate(report["results"], ("with_skill",))
    base_rate = eval_tools._variant_pass_rate(report["results"], ("without_skill",))
    assert with_rate == 1.0
    assert base_rate == 0.5


# ---------------------------------------------------------------------------
# plan_skill_eval
# ---------------------------------------------------------------------------

def test_plan_skill_eval_은_두_variant_의_실행_경로를_모두_돌려준다(ws):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)

    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill_dir)}))

    assert out["iteration"] == 1
    assert len(out["runs"]) == 4
    variants = {r["variant"] for r in out["runs"]}
    assert variants == {"with_skill", "without_skill"}
    # 대조군에는 스킬 경로를 주지 않아야 한다(엿보면 비교가 무의미해진다).
    assert all(r["skill_path"] == "none" for r in out["runs"] if r["variant"] == "without_skill")
    # 실행 산출물은 스킬 폴더 밖(.skill-evals)이어야 저장되는 스킬 패키지를 오염시키지 않는다.
    assert all("/.skill-evals/" in r["outputs_dir"] for r in out["runs"])
    assert all(f"/skills/{SKILL}/" not in r["outputs_dir"] for r in out["runs"])


def test_plan_skill_eval_은_evals_json_이_없으면_무엇을_써야_하는지_알려준다(ws):
    skill_dir = _write_skill(ws)

    out = eval_tools.plan_skill_eval.invoke({"skill_path": str(skill_dir)})

    assert out.startswith("ERROR:")
    assert "evals.json" in out


def test_채점이_다_끝난_회차가_있으면_재실행을_시키지_않는다(ws):
    """DB 저장 실패 등으로 complete 이 한 번 튕겼을 때 전체 실행을 다시 돌리면 낭비다."""
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    _complete_run(ws, iteration=1)

    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill_dir)}))

    assert out["already_complete"] is True
    assert out["iteration"] == 1
    assert out["runs"] == []
    assert "complete_skill_creation" in out["next"]


def test_이전_회차가_미완이면_다음_iteration_으로_올라간다(ws):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    # case-1 만 돌다 말았다.
    _write_grading(ws, 1, "case-1", "with_skill", 2, 2)
    _write_grading(ws, 1, "case-1", "without_skill", 0, 2)

    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill_dir)}))

    assert out["iteration"] == 2
    assert len(out["runs"]) == 4


# ---------------------------------------------------------------------------
# complete_skill_creation (DB 는 스텁)
# ---------------------------------------------------------------------------

class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._filters = {}
        self._payload = None
        self._op = None
        self._order = None

    def select(self, *_a, **_kw):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, *_a, **_kw):
        return self

    def execute(self):
        rows = self._store.setdefault(self._name, [])
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", f"{self._name}-{len(rows) + len(created) + 1}")
                row.setdefault("created_at", f"2026-09-04T00:00:{len(rows) + len(created):02d}Z")
                created.append(row)
            rows.extend(created)
            return type("R", (), {"data": created})()
        if self._op == "update":
            matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
            for r in matched:
                r.update(self._payload)
            return type("R", (), {"data": matched})()
        matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._order:
            column, desc = self._order
            matched.sort(key=lambda r: r.get(column) or 0, reverse=bool(desc))
        return type("R", (), {"data": matched})()


class _FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name):
        return _FakeTable(self.store, name)


@pytest.fixture
def fake_db(monkeypatch):
    sb = _FakeSupabase()
    monkeypatch.setattr(eval_tools, "_sb", lambda: sb)
    return sb


def test_complete_skill_creation_은_테스트_전에는_에러와_남은_일을_돌려준다(ws, fake_db):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)

    out = eval_tools.complete_skill_creation.invoke({"skill_path": str(skill_dir)})

    assert out.startswith("ERROR:")
    body = json.loads(out[len("ERROR:"):])
    assert body["missing_runs"]
    assert "완료를 알리지 말고" in body["message"]
    assert not fake_db.store, "실패한 실행은 DB 에 기록되면 안 된다"


def test_complete_skill_creation_은_시나리오와_결과를_기준선으로_저장한다(ws, fake_db):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    _complete_run(ws, with_passed=2, without_passed=1)

    out = json.loads(eval_tools.complete_skill_creation.invoke({"skill_path": str(skill_dir)}))

    assert out["trigger"] == "creation"
    assert out["pass_rate"] == 1.0
    assert out["baseline_pass_rate"] == 0.5
    assert len(fake_db.store["resource_eval_suites"]) == 1
    assert len(fake_db.store["resource_eval_cases"]) == 2
    assert len(fake_db.store["resource_eval_runs"]) == 1
    assert len(fake_db.store["resource_eval_results"]) == 4
    # 시나리오가 그대로 보관돼야 회귀 테스트에서 재사용할 수 있다.
    stored = {c["eval_name"]: c for c in fake_db.store["resource_eval_cases"]}
    assert stored["case-1"]["prompt"] == "NDA 문서 1 을 검토해줘"
    assert len(stored["case-1"]["assertions"]) == 2


def test_complete_skill_creation_은_SKILL_md_해시를_남겨_버전을_구분한다(ws, fake_db):
    skill_dir = _write_skill(ws)
    _write_evals(skill_dir)
    _complete_run(ws)

    eval_tools.complete_skill_creation.invoke({"skill_path": str(skill_dir)})

    run = fake_db.store["resource_eval_runs"][0]
    assert run["head_digest"] and len(run["head_digest"]) == 64
    assert run["resource_type"] == "skill"
    assert run["trigger"] == "creation"


# ---------------------------------------------------------------------------
# 미들웨어 백스톱
# ---------------------------------------------------------------------------

_SKILL_MD_PATH = f"/workspace/.bpmn/{ROOM}/skills/{SKILL}/SKILL.md"


def _wrote_skill_md() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "write_file",
            "args": {"file_path": _SKILL_MD_PATH, "content": "---"},
            "id": "call_1",
        }],
    )


def _final_answer(text: str = "스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.") -> AIMessage:
    return AIMessage(content=text)


def test_테스트_없이_턴을_끝내려_하면_모델로_되돌린다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
        ToolMessage(content="ok", name="write_file", tool_call_id="call_1"),
        _final_answer(),
    ]}

    out = mw.after_model(state, None)

    assert out is not None
    assert out["jump_to"] == "model"
    nudge = out["messages"][0].content
    assert "테스트가 끝나지 않아" in nudge
    assert "complete_skill_creation" in nudge


def test_게이트를_통과했으면_개입하지_않는다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
        ToolMessage(content="ok", name="write_file", tool_call_id="call_1"),
        ToolMessage(content='{"pass_rate": 1.0}', name="complete_skill_creation", tool_call_id="call_2"),
        _final_answer(),
    ]}

    assert mw.after_model(state, None) is None


def test_게이트가_에러를_돌려준_것은_통과로_치지_않는다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
        ToolMessage(content='ERROR: {"problems": []}', name="complete_skill_creation", tool_call_id="call_2"),
        _final_answer(),
    ]}

    out = mw.after_model(state, None)

    assert out is not None and out["jump_to"] == "model"


def test_스킬_생성이_아닌_대화에는_개입하지_않는다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="오늘 날씨 어때?"),
        _final_answer("맑습니다."),
    ]}

    assert mw.after_model(state, None) is None


def test_아직_도구를_부르는_중이면_개입하지_않는다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
    ]}

    assert mw.after_model(state, None) is None


def test_사용자가_테스트_생략을_요청하면_개입하지_않는다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 테스트 없이 빨리 만들어줘"),
        _wrote_skill_md(),
        _final_answer(),
    ]}

    assert mw.after_model(state, None) is None


def test_개입_횟수는_제한된다(ws):
    """게이트가 계속 실패해도 무한 루프에 빠지지 않아야 한다."""
    mw = SkillEvalGateMiddleware(max_nudges=2)
    messages = [HumanMessage(content="NDA 검토 스킬 만들어줘"), _wrote_skill_md()]

    for _ in range(2):
        messages.append(_final_answer())
        out = mw.after_model({"messages": messages}, None)
        assert out is not None
        messages.extend(out["messages"])

    messages.append(_final_answer())
    assert mw.after_model({"messages": messages}, None) is None


@pytest.mark.asyncio
async def test_비동기_경로도_같은_판단을_한다(ws):
    mw = SkillEvalGateMiddleware()
    state = {"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
        _final_answer(),
    ]}

    out = await mw.aafter_model(state, None)

    assert out is not None and out["jump_to"] == "model"


# ---------------------------------------------------------------------------
# 그래프 레벨: 미들웨어가 실제로 턴을 되돌리는가
# ---------------------------------------------------------------------------

def test_그래프에서_턴_종료가_실제로_모델로_되돌아간다(ws):
    """`jump_to` 가 langchain 그래프에서 실제 재호출로 이어지는지 확인한다.

    `hook_config(can_jump_to=["model"])` 가 빠지면 langchain 은 이 미들웨어 노드에
    고정 엣지를 깔아 `jump_to` 를 조용히 무시한다 — 그러면 게이트가 무력화되므로
    단위 테스트가 아니라 그래프를 실제로 돌려 확인한다.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    calls: list[int] = []

    class _CountingModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(len(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = _CountingModel(messages=iter([
        AIMessage(content="스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요."),
        AIMessage(content="테스트를 먼저 돌리겠습니다."),
        AIMessage(content="테스트를 먼저 돌리겠습니다."),
        AIMessage(content="테스트를 먼저 돌리겠습니다."),
    ]))

    agent = create_agent(model=model, tools=[], middleware=[SkillEvalGateMiddleware(max_nudges=1)])

    result = agent.invoke({"messages": [
        HumanMessage(content="NDA 검토 스킬 만들어줘"),
        _wrote_skill_md(),
        ToolMessage(content="ok", name="write_file", tool_call_id="call_1"),
    ]})

    assert len(calls) == 2, f"게이트가 모델을 다시 호출하지 않았다 (calls={len(calls)})"
    nudges = [
        m for m in result["messages"]
        if isinstance(m, HumanMessage) and str(m.content).startswith("[skill-eval-gate]")
    ]
    assert len(nudges) == 1
    assert result["messages"][-1].content == "테스트를 먼저 돌리겠습니다."
