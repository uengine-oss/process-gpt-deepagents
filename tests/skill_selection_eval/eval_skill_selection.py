"""지식 스킬 선택 평가 — 딥에이전트가 그 스킬의 SKILL.md 를 읽는가.

deepagents 의 스킬 선택은 「스킬 목록의 description 대조 → SKILL.md read_file」이다
(core/agents/agent.py `_SKILL_SELECTION_RULES`, deepagents SkillsMiddleware). 이 채점기는 같은 것을 흉내 낸다:

- 시스템 프롬프트 = 실제 `DEFAULT_SYSTEM_PROMPT` + 실제 SkillsMiddleware 가 만드는 스킬 목록
  (저장소의 skills/anthropics-skills + system-skills/process-gpt-system 을 /skills/ 아래로 복사해 그대로 읽음)
- 도구 = deepagents 파일 도구 + write_todos + 루트 서비스 도구(`root_service_tools`) + Lite MCP 도구(lite_tools.json)
- 모델 = Process GPT Lite server/.env 의 PRO_LLM_*(로컬 deepagents :8888 은 건드리지 않는다)
- 한 문항마다 모델이 읽기 도구(read_file·ls·glob·grep·write_todos)만 부르는 동안 최대 4번 돌리고,
  그 사이 read_file 로 읽은 SKILL.md 를 모은다. 다른 도구를 부르거나 글로 답하면 멈춘다.

판정: 맞는 문항(pos)은 대상 스킬의 SKILL.md 를 읽었으면 통과, 아닌 문항(neg)은 읽었으면 잘못 읽음.
통과 기준(설계 7.4): 맞는 문항 읽은 비율 ≥ 0.90, 아닌 문항 읽은 비율 ≤ 0.05.

쓰는 법(작업 트리 루트에서):
  PY=/Users/uengine/process-gpt/services/deepagents/.venv/bin/python
  $PY tests/skill_selection_eval/eval_skill_selection.py check
  $PY tests/skill_selection_eval/eval_skill_selection.py run --split dev [--skills a,b] [--repeat 3]
  $PY tests/skill_selection_eval/eval_skill_selection.py run --split holdout      # 집계만 출력
  $PY tests/skill_selection_eval/eval_skill_selection.py overlap                  # description ↔ 문항·금지 문장 겹침

보류 세트(holdout)는 고치는 사람이 문항을 보지 않는다 — 이 도구는 holdout 의 문장을 출력하지 않고 범주 집계만 낸다.
holdout 을 5번 채점하면 20% 를 새 문항으로 바꿀 때다(holdout_runs.json 에 센다, 설계 7.1).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CASES = HERE / "cases"
RESULTS = HERE / "results"
SPECS = HERE / "specs.json"
FORBIDDEN = HERE / "forbidden.json"
LITE_TOOLS = HERE / "lite_tools.json"
HOLDOUT_RUNS = HERE / "holdout_runs.json"
DEFAULT_ENV = Path("/Users/uengine/uengine-platform/process-gpt-lite-skills/server/.env")

SYSTEM_COLLECTION = REPO / "system-skills" / "process-gpt-system"
GLOBAL_COLLECTION = REPO / "skills" / "anthropics-skills"

EXPLORE = {"read_file", "ls", "glob", "grep", "write_todos"}
MAX_STEPS = 4

# Lite 가 delegate 할 때 메시지 뒤에 붙이는 시스템 사실(형식만 같게, server/lib/provenance.js workspaceHint).
WORKSPACE_HINT = (
    "\n\n(파일 위치 규칙: 새로 만드는 파일은 이 대화 전용 산출물 폴더(/workspace/.bpmn/eval-room/) 아래에 저장하세요. "
    "참고 자료는 사용자가 첨부했거나 공유받은 파일(/workspace/uploads/eval-user/ 아래), 이 대화 폴더, "
    "사용자에게 공유된 폴더(folder_ 도구)에서만 찾으세요.)"
)
OFFICE_KIND = {"docs": "워드", "sheets": "엑셀", "slides": "파워포인트", "pdf": "PDF", "hwp": "한글"}
CRAFT_TITLE = {"cadcraft": "CADCraft", "vectorcraft": "VectorCraft", "photocraft": "PhotoCraft",
               "soundcraft": "SoundCraft", "filmcraft": "FilmCraft"}


# ---------------------------------------------------------------------------
# 문항
# ---------------------------------------------------------------------------

def load_cases(split: str, skills: list[str] | None = None) -> list[dict]:
    out = []
    for f in sorted((CASES / split).glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        if skills and doc["skill"] not in skills:
            continue
        for c in doc["cases"]:
            out.append({**c, "skill": doc["skill"], "split": split})
    return out


def open_fact(open_state: dict | None) -> str:
    """열린 문서 사실 한 문장(Lite craftLive.js liveHint 의 첫 문장과 같은 꼴)."""
    if not open_state:
        return ""
    app, file = open_state.get("app"), open_state.get("file") or "문서"
    if app == "ieumoffice":
        kind = OFFICE_KIND.get(open_state.get("editor") or "", "문서")
        return f"\n\n(사용자가 지금 이음오피스 {kind} 편집기에서 '{file}' 을 열어 두었어요.)"
    return f"\n\n(사용자가 지금 {CRAFT_TITLE.get(app, app)} 에서 '{file}' 을 열어 두었어요.)"


def message_of(case: dict) -> str:
    return case["text"] + open_fact(case.get("open")) + WORKSPACE_HINT


# ---------------------------------------------------------------------------
# 겹침 검사(금지 문장, 문항끼리, description ↔ 문항)
# ---------------------------------------------------------------------------

def norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", str(t)).lower()
    return re.sub(r"[\s\W_]+", "", t)


def grams(t: str, n: int = 3) -> set[str]:
    t = norm(t)
    return {t[i:i + n] for i in range(max(len(t) - n + 1, 1))} if t else set()


def jaccard(a: str, b: str, n: int = 3) -> float:
    x, y = grams(a, n), grams(b, n)
    return len(x & y) / len(x | y) if x and y else 0.0


def longest_common(a: str, b: str) -> str:
    a, b = norm(a), norm(b)
    best, prev = "", [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j]:i]
        prev = cur
    return best


def forbidden_sentences() -> list[str]:
    return json.loads(FORBIDDEN.read_text(encoding="utf-8"))["sentences"]


def forbidden_hits(text: str, sentences: list[str], *, jac: float = 0.5) -> list[str]:
    hits = []
    t = norm(text)
    for s in sentences:
        ns = norm(s)
        if len(ns) >= 6 and ns in t:
            hits.append(s)
        elif len(ns) >= 6 and jaccard(text, s) >= jac:
            hits.append(s)
    return hits


def skill_frontmatter(path: Path) -> dict:
    import yaml

    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    return yaml.safe_load(m.group(1)) if m else {}


def eval_skill_names() -> list[str]:
    return list(json.loads(SPECS.read_text(encoding="utf-8"))["skills"])


def cmd_check(_args) -> int:
    sentences = forbidden_sentences()
    names = eval_skill_names()
    problems = 0
    texts: dict[str, tuple[str, str]] = {}
    for split in ("dev", "holdout"):
        for name in names:
            f = CASES / split / f"{name}.json"
            if not f.exists():
                print(f"[없음] {f}")
                problems += 1
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
            labels = collections.Counter(c["label"] for c in doc["cases"])
            if labels != {"pos": 10, "neg": 10}:
                print(f"[개수] {split}/{name}: {dict(labels)} (10/10 이어야 함)")
                problems += 1
            for c in doc["cases"]:
                if c["id"] in texts:
                    print(f"[id 중복] {c['id']}")
                    problems += 1
                texts[c["id"]] = (split, c["text"])
                hit = forbidden_hits(c["text"], sentences)
                if hit:
                    problems += 1
                    print(f"[금지 문장] {c['id']}" + (f": {c['text']} ~ {hit[0]}" if split == "dev" else ""))
    ids = list(texts)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if jaccard(texts[a][1], texts[b][1]) >= 0.6:
                problems += 1
                print(f"[거의 같은 문항] {a} ~ {b}")
    for name in names:
        md = SYSTEM_COLLECTION / name / "SKILL.md"
        if not md.exists():
            print(f"[스킬 없음] {md}")
            problems += 1
            continue
        hit = forbidden_hits(str(skill_frontmatter(md).get("description", "")), sentences, jac=0.35)
        if hit:
            problems += 1
            print(f"[금지 문장이 description 에] {name}: {hit}")
    print(f"문항 {len(texts)}개, 문제 {problems}건")
    return 1 if problems else 0


def cmd_overlap(_args) -> int:
    """설계 7.5: 고친 description 이 개발 세트 문항의 낱말을 옮겨 왔는지 본다(최장 공통 부분 문자열)."""
    dev = load_cases("dev")
    flagged = 0
    for name in eval_skill_names():
        desc = str(skill_frontmatter(SYSTEM_COLLECTION / name / "SKILL.md").get("description", ""))
        worst = max(((longest_common(desc, c["text"]), c["id"]) for c in dev), key=lambda x: len(x[0]))
        jac = max(jaccard(desc, c["text"], 4) for c in dev)
        mark = "  <-- 확인" if len(worst[0]) >= 10 else ""
        flagged += bool(mark)
        print(f"{name:24s} 최장 공통 {len(worst[0]):2d}자 ({worst[1]}: {worst[0]!r})  4-gram Jaccard 최대 {jac:.2f}{mark}")
    return 1 if flagged else 0


# ---------------------------------------------------------------------------
# 채점기
# ---------------------------------------------------------------------------

def load_env(path: Path) -> dict:
    from dotenv import dotenv_values

    env = dotenv_values(path)
    need = ["PRO_LLM_BASE_URL", "PRO_LLM_API_KEY", "PRO_LLM_MODEL"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise SystemExit(f"{path} 에 {missing} 가 없다")
    return env


class Grader:
    def __init__(self, env: dict, tenant_skills: Path | None = None, model: str | None = None):
        sys.path.insert(0, str(REPO))
        os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:0")
        os.environ.setdefault("SUPABASE_KEY", "eval")
        from deepagents.backends.filesystem import FilesystemBackend
        from deepagents.middleware.filesystem import FilesystemMiddleware
        from deepagents.middleware.skills import SKILLS_SYSTEM_PROMPT, SkillsMiddleware, _list_skills
        from langchain.agents.middleware import TodoListMiddleware
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from langchain_openai import ChatOpenAI

        import core.agents.agent as agent_mod

        self.tmp = Path(tempfile.mkdtemp(prefix="skill-eval-"))
        skills_root = self.tmp / "skills"
        shutil.copytree(GLOBAL_COLLECTION, skills_root / "anthropics-skills")
        shutil.copytree(SYSTEM_COLLECTION, skills_root / "process-gpt-system")
        sources = []
        if tenant_skills:
            shutil.copytree(tenant_skills, skills_root / "eval-tenant" / "local")
            sources.append("/skills/eval-tenant/local")
        sources += ["/skills/anthropics-skills", "/skills/process-gpt-system"]
        self.backend = FilesystemBackend(root_dir=self.tmp, virtual_mode=True)

        merged: dict[str, dict] = {}
        for src in sources:
            for s in _list_skills(self.backend, src):
                merged[s["name"]] = s
        self.skills = list(merged.values())
        self.skill_paths = {s["name"]: s["path"] for s in self.skills}
        mw = SkillsMiddleware(backend=self.backend, sources=sources)
        section = SKILLS_SYSTEM_PROMPT.format(
            skills_locations=mw._format_skills_locations(),
            skills_load_warnings="",
            skills_list=mw._format_skills_list(self.skills),
        )
        self.system = agent_mod.DEFAULT_SYSTEM_PROMPT + "\n\n" + section

        tools = [convert_to_openai_tool(t) for t in FilesystemMiddleware(backend=self.backend).tools]
        tools += [convert_to_openai_tool(t) for t in TodoListMiddleware().tools]
        tools += [convert_to_openai_tool(t) for t in agent_mod.root_service_tools(is_chat=True)]
        for t in json.loads(LITE_TOOLS.read_text(encoding="utf-8"))["tools"]:
            tools.append({"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}})
        seen, self.tools = set(), []
        for t in reversed(tools):  # 이름이 겹치면 마지막 것(create_agent 와 같음)
            if t["function"]["name"] not in seen:
                seen.add(t["function"]["name"])
                self.tools.append(t)
        self.tools.reverse()
        self.model_name = model or env["PRO_LLM_MODEL"]
        self.llm = ChatOpenAI(model=self.model_name, base_url=env["PRO_LLM_BASE_URL"], api_key=env["PRO_LLM_API_KEY"],
                              timeout=180, max_retries=2).bind_tools(self.tools)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, args: dict) -> str:
        try:
            res = self.backend.read(args.get("file_path", ""), int(args.get("offset") or 0), int(args.get("limit") or 2000))
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        for attr in ("content", "text", "output"):
            v = getattr(res, attr, None)
            if isinstance(v, str):
                return v
        return str(res)

    async def grade(self, case: dict) -> dict:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        msgs = [SystemMessage(self.system), HumanMessage(message_of(case))]
        read_skills: list[str] = []
        by_path = {self._norm_path(p): n for n, p in self.skill_paths.items()}
        first_action, steps, started = None, 0, time.monotonic()
        for steps in range(1, MAX_STEPS + 1):
            ai: AIMessage = await self.llm.ainvoke(msgs)
            calls = ai.tool_calls or []
            for c in calls:
                if c["name"] == "read_file":
                    name = by_path.get(self._norm_path((c.get("args") or {}).get("file_path", "")))
                    if name and name not in read_skills:
                        read_skills.append(name)
            others = [c["name"] for c in calls if c["name"] not in EXPLORE]
            if not calls or others:
                first_action = others[0] if others else "text"
                break
            msgs.append(ai)
            for c in calls:
                out = self._read(c.get("args") or {}) if c["name"] == "read_file" else "ok"
                msgs.append(ToolMessage(content=out[:20000], tool_call_id=c["id"], name=c["name"]))
        target = case["skill"]
        read = target in read_skills
        return {
            "id": case["id"], "skill": target, "split": case["split"], "label": case["label"],
            "axes": case.get("axes", []), "read_skills": read_skills, "target_read": read,
            "ok": read if case["label"] == "pos" else not read,
            "first_action": first_action or "step-limit", "steps": steps,
            "seconds": round(time.monotonic() - started, 1),
        }

    @staticmethod
    def _norm_path(p: str) -> str:
        p = str(p or "").strip()
        return re.sub(r"/+", "/", p if p.startswith("/") else "/" + p)


async def run_all(grader: Grader, cases: list[dict], concurrency: int, repeat: int) -> list[list[dict]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(c):
        async with sem:
            # 공용 게이트웨이가 잠깐 끊기는 일이 있다(2026-11-21 holdout 1회차에 수 분) — 길게 물러나며 다시 한다.
            for attempt in range(6):
                try:
                    return await grader.grade(c)
                except Exception as e:  # noqa: BLE001
                    if attempt == 5:
                        return {"id": c["id"], "skill": c["skill"], "split": c["split"], "label": c["label"],
                                "axes": c.get("axes", []), "error": f"{type(e).__name__}: {e}"[:300], "ok": None}
                    await asyncio.sleep(min(5 * 2 ** attempt, 90))

    rounds = []
    for _ in range(repeat):
        rounds.append(await asyncio.gather(*(one(c) for c in cases)))
    return rounds


def summarize(rounds: list[list[dict]], *, show_text: bool, cases_by_id: dict) -> dict:
    # 첫 회차가 오류인 문항은 다음 회차의 판정을 쓴다(점수는 문항당 한 번). 뒤집힘은 오류 없는 회차끼리만 비교한다.
    first = [next((rr[i] for rr in rounds if rr[i].get("ok") is not None), rounds[0][i]) for i in range(len(rounds[0]))]
    by_skill = collections.defaultdict(lambda: {"pos": [0, 0], "neg": [0, 0], "err": 0})
    by_axis = collections.defaultdict(lambda: [0, 0])
    errors = []
    for r in first:
        if r.get("ok") is None:
            by_skill[r["skill"]]["err"] += 1
            errors.append(r)
            continue
        cell = by_skill[r["skill"]][r["label"]]
        cell[0] += int(r["target_read"])
        cell[1] += 1
        for ax in r.get("axes", []):
            k = f"{r['label']}:{ax}"
            by_axis[k][0] += int(r["ok"])
            by_axis[k][1] += 1
    flips = 0
    if len(rounds) > 1:
        for i, r in enumerate(first):
            decisions = {bool(rr[i].get("target_read")) for rr in rounds if rr[i].get("ok") is not None}
            flips += len(decisions) > 1
    lines = [f"{'skill':24s} {'pos read':>10s} {'neg read':>10s}  pass"]
    all_pos, all_neg = [0, 0], [0, 0]
    for name in sorted(by_skill):
        s = by_skill[name]
        pr = s["pos"][0] / s["pos"][1] if s["pos"][1] else 0
        nr = s["neg"][0] / s["neg"][1] if s["neg"][1] else 0
        ok = pr >= 0.90 and nr <= 0.05
        all_pos = [all_pos[0] + s["pos"][0], all_pos[1] + s["pos"][1]]
        all_neg = [all_neg[0] + s["neg"][0], all_neg[1] + s["neg"][1]]
        lines.append(f"{name:24s} {s['pos'][0]:3d}/{s['pos'][1]:<3d}{pr:4.2f} {s['neg'][0]:3d}/{s['neg'][1]:<3d}{nr:4.2f}  {'OK' if ok else 'NO'}"
                     + (f"  (오류 {s['err']})" if s["err"] else ""))
    tp = all_pos[0] / all_pos[1] if all_pos[1] else 0
    tn = all_neg[0] / all_neg[1] if all_neg[1] else 0
    lines.append(f"{'전체':24s} {all_pos[0]:3d}/{all_pos[1]:<3d}{tp:4.2f} {all_neg[0]:3d}/{all_neg[1]:<3d}{tn:4.2f}")
    lines.append("")
    lines.append("변형 축별 통과율(pos=읽음, neg=안 읽음):")
    for k in sorted(by_axis):
        a, n = by_axis[k]
        lines.append(f"  {k:22s} {a:3d}/{n:<3d} {a / n:4.2f}")
    if len(rounds) > 1:
        lines.append(f"\n같은 입력 {len(rounds)}회 결정 뒤집힘: {flips}/{len(first)} = {flips / max(len(first), 1):.3f}")
    if show_text:
        bad = [r for r in first if r.get("ok") is False]
        if bad:
            lines.append("\n틀린 문항(dev):")
            for r in bad:
                c = cases_by_id[r["id"]]
                lines.append(f"  {r['id']} [{r['label']}] read={r['read_skills']} act={r['first_action']} :: {c['text']}"
                             + (f"  {{open: {c['open']}}}" if c.get("open") else ""))
    if errors:
        lines.append("\n오류:")
        for r in errors:
            lines.append(f"  {r['id']} {r['error']}")
    return {"text": "\n".join(lines), "pos_rate": tp, "neg_rate": tn, "flip": flips / max(len(first), 1) if len(rounds) > 1 else None,
            "by_skill": {k: dict(v) for k, v in by_skill.items()}, "by_axis": {k: v for k, v in by_axis.items()}}


def cmd_run(args) -> int:
    skills = [s.strip() for s in args.skills.split(",")] if args.skills else None
    cases = load_cases(args.split, skills)
    if args.ids:
        want = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in want]
    if not cases:
        raise SystemExit("문항이 없다")
    env = load_env(Path(args.env))
    grader = Grader(env, Path(args.tenant_skills) if args.tenant_skills else None, args.model)
    try:
        print(f"모델 {grader.model_name} · 스킬 {len(grader.skills)}개 · 도구 {len(grader.tools)}개 · 시스템 프롬프트 {len(grader.system)}자 · 문항 {len(cases)} × {args.repeat}")
        t0 = time.monotonic()
        rounds = asyncio.run(run_all(grader, cases, args.concurrency, args.repeat))
    finally:
        grader.close()
    show = args.split != "holdout"
    summary = summarize(rounds, show_text=show, cases_by_id={c["id"]: c for c in cases})
    print(summary["text"])
    print(f"\n{time.monotonic() - t0:.0f}초")
    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = {"split": args.split, "model": grader.model_name, "tenant_skills": args.tenant_skills, "repeat": args.repeat,
           "summary": {k: v for k, v in summary.items() if k != "text"},
           "rounds": rounds if show else [[{k: v for k, v in r.items() if k != "read_skills"} | {"read_skills": r.get("read_skills")} for r in rr] for rr in rounds]}
    (RESULTS / f"{stamp}-{args.split}{'-' + args.tag if args.tag else ''}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.split == "holdout":
        runs = json.loads(HOLDOUT_RUNS.read_text(encoding="utf-8")) if HOLDOUT_RUNS.exists() else {"runs": []}
        runs["runs"].append({"at": stamp, "model": grader.model_name, "pos_rate": summary["pos_rate"], "neg_rate": summary["neg_rate"], "tag": args.tag})
        HOLDOUT_RUNS.write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
        if len(runs["runs"]) >= 5:
            print(f"\n[알림] 보류 세트를 {len(runs['runs'])}번 채점했다 — 20% 를 새 문항으로 바꿀 때다(설계 7.1).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="문항 개수·금지 문장·중복·description 금지 문장 검사")
    sub.add_parser("overlap", help="description 과 개발 세트 문항의 겹침")
    r = sub.add_parser("run", help="채점")
    r.add_argument("--split", choices=["dev", "holdout"], default="dev")
    r.add_argument("--skills", help="쉼표로 구분한 스킬 이름(기본 전부)")
    r.add_argument("--ids", help="쉼표로 구분한 문항 id(dev 에서 다시 볼 때)")
    r.add_argument("--repeat", type=int, default=1, help="같은 입력 반복 횟수(뒤집힘 비율)")
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--env", default=str(DEFAULT_ENV), help="PRO_LLM_* 가 든 .env")
    r.add_argument("--model", help="PRO_LLM_MODEL 대신 쓸 모델")
    r.add_argument("--tenant-skills", help="테넌트 스킬 폴더(그 안의 <스킬>/SKILL.md)를 함께 싣는다")
    r.add_argument("--tag", default="", help="결과 파일 이름에 붙일 꼬리표")
    args = p.parse_args()
    return {"check": cmd_check, "overlap": cmd_overlap, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
