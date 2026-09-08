"""publish_skill 이 신규 레포를 만든 뒤 부르는 _auto_register_tenant_skill 동작 테스트.

배경: 사용자가 채팅으로 스킬을 만들고 "저장" 버튼을 누르면 register_skill_as_tenant 가
tenant_skills 에 행을 남긴다. 그 뒤 같은 스킬을 Git 으로 발행하면 이 함수가 다시
insert 를 시도해 tenant_skills_pkey 중복으로 실패했고, 실패 처리가 skills/ 디렉터리를
rmtree 해서 **멀쩡히 등록돼 있던 스킬이 발행 한 번에 사라졌다.**

이미 등록된 스킬이면 insert 를 건너뛰고 파일만 갱신해야 하며, 그래도 DB 단계가 실패하면
기존 파일이 되살아나야 한다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.skills import tools as skill_tools

TENANT = "tenant-auto-reg-test"
SKILL = "booking-review-test"
HTML_URL = "https://example.invalid/owner/booking-review-test"


def _supabase(existing: bool):
    """tenant_skills 조회가 기존 행을 돌려주는지 여부만 다른 supabase 스텁."""
    table = MagicMock()
    select = table.select.return_value
    select.eq.return_value = select
    select.limit.return_value = select
    select.execute.return_value = MagicMock(data=[{"skill_name": SKILL}] if existing else [])

    sb = MagicMock()
    sb.table.return_value = table
    return sb, table


@pytest.fixture
def env(tmp_path, monkeypatch):
    """workspace/skills 를 임시 디렉터리로 격리하고 tenant 컨텍스트를 세팅."""
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    workspace.mkdir()
    skills.mkdir()
    monkeypatch.setenv("SKILLS_DIRS", str(skills))
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", workspace)
    # git provider 조회는 config_id 를 얻는 용도뿐이라 "local" 로 떨어지게 둔다.
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: (_ for _ in ()).throw(ValueError("no provider")))

    token = skill_tools._current_tenant_id.set(TENANT)
    yield workspace, skills
    skill_tools._current_tenant_id.reset(token)


def _checkout(workspace: Path, body: str) -> str:
    """발행 직전의 체크아웃 디렉터리를 만들고 컨테이너 경로를 돌려준다."""
    src = workspace / SKILL
    (src / "references").mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(f"---\nname: {SKILL}\ndescription: d\n---\n\n{body}\n")
    (src / "references" / "checklist.md").write_text(f"checklist {body}")
    return f"/workspace/{SKILL}"


def _dest(skills: Path) -> Path:
    return skills / TENANT / "local" / SKILL


def test_new_skill_is_inserted(env):
    workspace, skills = env
    sb, table = _supabase(existing=False)

    with patch("core.storage.db.get_supabase", return_value=sb):
        result = skill_tools._auto_register_tenant_skill(_checkout(workspace, "v1"), SKILL, HTML_URL)

    assert "테넌트 스킬로 등록 완료" in result, result
    assert "v1" in (_dest(skills) / "SKILL.md").read_text()
    table.insert.assert_called_once()


def test_already_registered_skill_updates_files_without_insert(env):
    """저장 버튼으로 먼저 등록해 둔 스킬을 뒤늦게 발행하는 정상 경로."""
    workspace, skills = env
    sb, table = _supabase(existing=False)
    with patch("core.storage.db.get_supabase", return_value=sb):
        skill_tools._auto_register_tenant_skill(_checkout(workspace, "v1"), SKILL, HTML_URL)

    sb2, table2 = _supabase(existing=True)
    with patch("core.storage.db.get_supabase", return_value=sb2):
        result = skill_tools._auto_register_tenant_skill(_checkout(workspace, "v2"), SKILL, HTML_URL)

    # 중복키로 실패하지 않고, 파일은 새 내용으로 갱신된다.
    assert "WARNING" not in result, result
    assert "갱신" in result, result
    assert "v2" in (_dest(skills) / "SKILL.md").read_text()
    assert "v2" in (_dest(skills) / "references" / "checklist.md").read_text()
    table2.insert.assert_not_called()
    # 백업 디렉터리를 남기지 않는다.
    assert not list(_dest(skills).parent.glob(f"{SKILL}.bak-*"))


def test_db_failure_restores_previous_skill_files(env):
    """DB 단계가 실패해도 이미 등록돼 있던 스킬 파일이 사라지면 안 된다."""
    workspace, skills = env
    sb, _ = _supabase(existing=False)
    with patch("core.storage.db.get_supabase", return_value=sb):
        skill_tools._auto_register_tenant_skill(_checkout(workspace, "v1"), SKILL, HTML_URL)

    with patch("core.storage.db.get_supabase", side_effect=RuntimeError("db down")):
        result = skill_tools._auto_register_tenant_skill(_checkout(workspace, "v2"), SKILL, HTML_URL)

    assert "롤백" in result, result
    assert (_dest(skills) / "SKILL.md").exists(), "기존 스킬이 삭제되면 안 된다"
    assert "v1" in (_dest(skills) / "SKILL.md").read_text()
    assert not list(_dest(skills).parent.glob(f"{SKILL}.bak-*"))
