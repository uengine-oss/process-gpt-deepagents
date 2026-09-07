"""register_skill_as_tenant 의 신규 등록 / 기존 스킬 갱신 동작 테스트.

배경: 딥 에이전트가 기존 스킬을 수정한 뒤 이 도구를 호출하면 "이미 존재합니다" 로
실패했는데, 에이전트가 그것을 성공으로 보고해 수정이 조용히 유실됐다. 기본값은
계속 거부(오탐 방지)하되 overwrite=True 로 명시하면 제자리 갱신되어야 한다.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.skills import tools as skill_tools
from core.skills.upload_utils import get_tenant_skill_dest

TENANT = "tenant-reg-test"
SKILL = "expense-review-test"


def _make_skill_package(tmp_path: Path, body: str) -> Path:
    """<skill>/SKILL.md + refs 파일을 담은 .skill(zip) 을 만들어 경로를 돌려준다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{SKILL}/SKILL.md", f"---\nname: {SKILL}\ndescription: d\n---\n\n{body}\n")
        zf.writestr(f"{SKILL}/references/checklist.md", f"checklist {body}")
    pkg = tmp_path / f"{SKILL}.skill"
    pkg.write_bytes(buf.getvalue())
    return pkg


@pytest.fixture
def env(tmp_path, monkeypatch):
    """WORKSPACE_ROOT 와 SKILLS_DIRS 를 임시 디렉터리로 격리하고 tenant 컨텍스트를 세팅."""
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    workspace.mkdir()
    skills.mkdir()
    monkeypatch.setenv("SKILLS_DIRS", str(skills))
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", workspace)

    token = skill_tools._current_tenant_id.set(TENANT)
    yield workspace
    skill_tools._current_tenant_id.reset(token)


def _invoke(pkg: Path, workspace: Path, **kwargs) -> str:
    """도구를 호출한다. .skill 은 workspace 안에 있어야 _resolve_ws_path 가 찾는다."""
    target = workspace / pkg.name
    target.write_bytes(pkg.read_bytes())
    return skill_tools.register_skill_as_tenant.invoke(
        {"skill_path": f"/workspace/{pkg.name}", **kwargs}
    )


def _dest() -> Path:
    return get_tenant_skill_dest(TENANT, SKILL, config_id="local")


def test_new_skill_is_registered(env, tmp_path):
    with patch("core.storage.db.get_supabase") as gs:
        result = _invoke(_make_skill_package(tmp_path, "v1"), env)

    assert result.startswith("✓"), result
    assert "등록 완료" in result
    assert "v1" in (_dest() / "SKILL.md").read_text()
    # 신규 등록은 tenant_skills 에 insert 되어야 한다.
    gs.assert_called_once()


def test_existing_skill_is_refused_without_overwrite(env, tmp_path):
    with patch("core.storage.db.get_supabase"):
        _invoke(_make_skill_package(tmp_path, "v1"), env)

    with patch("core.storage.db.get_supabase") as gs:
        result = _invoke(_make_skill_package(tmp_path, "v2"), env)

    assert result.startswith("ERROR:"), result
    # 실패 사유가 올바른 다음 행동을 가리켜야 한다(기존 메시지는 "삭제하세요" 였다).
    assert "publish_skill" in result
    assert "overwrite=True" in result
    # 기존 파일은 그대로여야 한다.
    assert "v1" in (_dest() / "SKILL.md").read_text()
    gs.assert_not_called()


def test_existing_skill_is_updated_with_overwrite(env, tmp_path):
    with patch("core.storage.db.get_supabase"):
        _invoke(_make_skill_package(tmp_path, "v1"), env)

    with patch("core.storage.db.get_supabase") as gs:
        result = _invoke(_make_skill_package(tmp_path, "v2"), env, overwrite=True)

    assert result.startswith("✓"), result
    assert "갱신 완료" in result
    assert "v2" in (_dest() / "SKILL.md").read_text()
    assert "v2" in (_dest() / "references" / "checklist.md").read_text()
    # 이미 등록된 스킬이므로 tenant_skills 중복 insert 는 하지 않는다.
    gs.assert_not_called()
    # 백업 디렉터리는 남기지 않는다.
    assert not list(_dest().parent.glob(f"{SKILL}.bak-*"))


def test_overwrite_keeps_old_files_when_move_fails(env, tmp_path):
    with patch("core.storage.db.get_supabase"):
        _invoke(_make_skill_package(tmp_path, "v1"), env)

    import shutil

    calls = {"n": 0}
    orig_move = shutil.move

    def flaky_move(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # 백업 이동은 성공, 새 파일 이동에서 실패
            raise OSError("disk full")
        return orig_move(src, dst)

    with patch("core.storage.db.get_supabase"), patch.object(shutil, "move", flaky_move):
        result = _invoke(_make_skill_package(tmp_path, "v2"), env, overwrite=True)

    assert result.startswith("ERROR:"), result
    # 롤백되어 기존 스킬이 살아 있어야 한다.
    assert "v1" in (_dest() / "SKILL.md").read_text()
