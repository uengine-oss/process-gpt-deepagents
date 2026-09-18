"""frontmatter 가 빠진 스킬을 조용히 넘기지 않는다.

`_scan_collection` 은 SKILL.md 만 있으면 스킬을 목록에 올린다. frontmatter 가
없거나 깨져 description 이 비어도 마찬가지다. 그러면 스킬은 목록에는 보이는데
모델이 고를 근거가 없어 아무 데서도 쓰이지 않는다 — 등록해 둔 사람은 "왜 안
쓰이지" 로만 겪게 된다. `bpmn-process-generation-skill` 이 실제로 그랬다.

그래서 로더가 경고를 남긴다. 이 파일은 그 경고를 지우면 깨진다.
"""

import logging

from core.api.skills_router import _scan_collection


def _skill(root, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_description_이_비면_경고를_남긴다(tmp_path, caplog):
    col = tmp_path / "process-gpt-system"
    _skill(col, "no-frontmatter", "# 제목만 있고 frontmatter 가 없다\n")

    with caplog.at_level(logging.WARNING):
        rows = _scan_collection(str(col))

    # 목록에서 빼지는 않는다 — 있는 것을 숨기면 더 찾기 어렵다.
    assert [r["name"] for r in rows] == ["no-frontmatter"]
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "no-frontmatter" in m and "description" in m for m in messages
    ), messages


def test_frontmatter_가_멀쩡하면_경고하지_않는다(tmp_path, caplog):
    col = tmp_path / "process-gpt-system"
    _skill(
        col,
        "good-skill",
        "---\nname: good-skill\ndescription: 무엇을 하는 스킬인지 적혀 있다.\n---\n\n# 본문\n",
    )

    with caplog.at_level(logging.WARNING):
        rows = _scan_collection(str(col))

    assert rows[0]["description"] == "무엇을 하는 스킬인지 적혀 있다."
    assert not [r for r in caplog.records if "description" in r.getMessage()]


def test_저장소에_번들된_시스템_스킬은_전부_설명이_있다(tmp_path, caplog):
    """실제 번들 스킬에 같은 누락이 없는지 훑는다."""
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "system-skills" / "process-gpt-system"
    if not src.is_dir():
        return
    with caplog.at_level(logging.WARNING):
        rows = _scan_collection(str(src))
    assert rows, "번들 시스템 스킬이 하나도 스캔되지 않았다"
    missing = [r["name"] for r in rows if not (r["description"] or "").strip()]
    assert not missing, f"description 이 빈 번들 스킬: {missing}"
