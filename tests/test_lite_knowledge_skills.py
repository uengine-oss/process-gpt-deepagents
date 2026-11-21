"""Process GPT Lite 의 코드에 박혀 있던 도구 사용법·도메인 지식을 옮긴 시스템 스킬 10개의 형식.

설계: pgpt-pro docs/mobile-review-1121/skill-routing-design.md 4.1·4.2, 9절 결정 4(전부 시스템 스킬).
선택이 잘 되는지는 LLM 채점기(tests/skill_selection_eval)가 재고, 이 파일은 형식만 지킨다.

- frontmatter 가 deepagents 로더를 그대로 통과한다(이름 = 폴더, description 1024자 이하, include_tools 는 공백 구분).
- Lite 도구(이음오피스·ArtCraft)에 기대는 스킬은 본문 첫 줄이 「이 도구들이 목록에 없으면 이 스킬을 쓰지 않는다」다
  — 시스템 스킬은 본판 테넌트에도 보이기 때문이다.
- 본문이 가리킨 references 파일이 있고, references 에 본문이 가리키지 않는 파일이 없다.
- description 에 녹화·기존 시험의 문장(평가 세트의 금지 목록)이 없다.
- 평가 세트가 스킬마다 개발·보류 10+10 씩 있다.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
BUNDLED = REPO / "system-skills" / "process-gpt-system"
EVAL = REPO / "tests" / "skill_selection_eval"

SKILLS = [
    "stock-price-lookup",
    "youtube-public-data",
    "connected-services",
    "hwpx-document",
    "ieumoffice-live-edit",
    "cadcraft-drawing",
    "vectorcraft-graphics",
    "photocraft-editing",
    "soundcraft-audio",
    "filmcraft-video",
]
# 본판에는 없는 Lite 도구에 기대는 스킬 → 그 도구의 이름 접두어
LITE_TOOL_SKILLS = {
    "ieumoffice-live-edit": "office_live_",
    "cadcraft-drawing": "cadcraft_",
    "vectorcraft-graphics": "vectorcraft_",
    "photocraft-editing": "photocraft_",
    "soundcraft-audio": "soundcraft_",
    "filmcraft-video": "filmcraft_",
}
GUARD = "목록에 없으면 이 스킬을 쓰지 않는다"


def _split(name: str) -> tuple[dict, str]:
    text = (BUNDLED / name / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    assert m, f"{name}: frontmatter 가 없다"
    return yaml.safe_load(m.group(1)), m.group(2)


def _eval_module():
    spec = importlib.util.spec_from_file_location("eval_skill_selection", EVAL / "eval_skill_selection.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", SKILLS)
def test_frontmatter_passes_the_deepagents_loader(name, caplog):
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware.skills import MAX_SKILL_DESCRIPTION_LENGTH, _list_skills

    meta, _ = _split(name)
    assert meta["name"] == name
    desc = str(meta.get("description") or "").strip()
    assert desc, "description 이 비었다"
    assert len(desc) <= MAX_SKILL_DESCRIPTION_LENGTH, f"description {len(desc)}자 — 넘는 부분은 모델이 못 본다"
    assert (meta.get("metadata") or {}).get("version"), "metadata.version 이 없다"
    include = (meta.get("metadata") or {}).get("include_tools")
    if include is not None:
        assert isinstance(include, str) and "," not in include, "include_tools 는 공백으로 구분한 문자열이어야 한다"

    backend = FilesystemBackend(root_dir=REPO, virtual_mode=True)
    with caplog.at_level(logging.WARNING):
        loaded = {s["name"]: s for s in _list_skills(backend, "/system-skills/process-gpt-system")}
    assert name in loaded, "deepagents 로더가 이 스킬을 건너뛰었다"
    assert loaded[name]["description"] == desc
    assert not [r for r in caplog.records if f"/{name}/" in r.getMessage()], [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("name,prefix", sorted(LITE_TOOL_SKILLS.items()))
def test_lite_tool_skills_open_with_the_guard_and_bind_only_their_tools(name, prefix):
    meta, body = _split(name)
    first = next(line for line in body.splitlines() if line.strip() and not line.startswith("#"))
    assert GUARD in first, f"본문 첫 줄에 「{GUARD}」가 없다: {first!r}"
    tools = meta["metadata"]["include_tools"].split()
    allowed = ("office_",) if name == "ieumoffice-live-edit" else (prefix,)
    assert tools and all(t.startswith(allowed) for t in tools), tools
    assert "office_live_save" not in tools, "저장은 사용자가 한다 — 스킬이 저장 도구를 열지 않는다"
    # description 은 도구가 숨겨져 있어도 고를 수 있어야 한다(스킬 전용 도구는 읽은 뒤에야 보인다)
    assert GUARD not in str(meta["description"])


def test_connector_tools_named_in_the_skill_are_the_lite_connector_tools():
    lite = {t["name"] for t in json.loads((EVAL / "lite_tools.json").read_text(encoding="utf-8"))["tools"]}
    meta, _ = _split("connected-services")
    named = set(meta["metadata"]["include_tools"].split())
    assert named <= lite, sorted(named - lite)
    yt, _ = _split("youtube-public-data")
    assert set(yt["metadata"]["include_tools"].split()) <= lite


@pytest.mark.parametrize("name", SKILLS)
def test_references_are_linked_both_ways(name):
    _, body = _split(name)
    linked = set(re.findall(r"references/([\w.-]+\.md)", body))
    refs = BUNDLED / name / "references"
    files = {p.name for p in refs.glob("*.md")} if refs.is_dir() else set()
    for ref in files:
        # 다른 references 파일이 가리키는 것도 연결된 것으로 본다(live-edit.md → recolor-line-art.md 등)
        linked |= set(re.findall(r"([\w.-]+\.md)", (refs / ref).read_text(encoding="utf-8"))) & files
    assert linked <= files, f"본문이 가리키는데 없는 파일: {sorted(linked - files)}"
    assert files <= linked, f"아무도 가리키지 않는 references: {sorted(files - linked)}"


def test_descriptions_hold_no_recorded_or_tested_sentence():
    mod = _eval_module()
    sentences = mod.forbidden_sentences()
    for name in SKILLS:
        meta, body = _split(name)
        for where, text in (("description", str(meta["description"])), ("본문", body)):
            assert not mod.forbidden_hits(text, sentences, jac=0.35 if where == "description" else 0.9), (name, where)


def test_the_eval_set_has_ten_and_ten_per_split():
    mod = _eval_module()
    assert set(mod.eval_skill_names()) == set(SKILLS)
    for split in ("dev", "holdout"):
        for name in SKILLS:
            doc = json.loads((EVAL / "cases" / split / f"{name}.json").read_text(encoding="utf-8"))
            labels = [c["label"] for c in doc["cases"]]
            assert labels.count("pos") == 10 and labels.count("neg") == 10, (split, name)
            for c in doc["cases"]:
                assert not mod.forbidden_hits(c["text"], mod.forbidden_sentences()), c["id"]
